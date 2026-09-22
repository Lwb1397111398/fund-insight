# -*- coding: utf-8 -*-
"""一次性只读查询：把"安全的做法"做成最省事的做法。

为什么要有这个文件：2026-09-22 我为了图快，写了条 `python -c "...from src.models.database
import engine..."` 的一次性查询，只设了 `LOCAL_DB_URL` 环境变量而**没调用守卫** ——
`src.models.database` 读的是 `DATABASE_URL`，`.env` 里它是生产 Supabase，于是两条 SELECT
直接打到线上。守卫存在并不能阻止"绕过它"，所以这里给一条绕不过去的捷径：
`scripts/q.py` 默认只读、先 pin 本地镜像、拒绝任何写语句。

只读这件事有**两层**，第二层才是保证：
1. `_assert_read_only()` 的正则 —— 快、报错友好，但它是我对方言的猜测；
2. 引擎级 —— SQLite 用 URI `mode=ro` 打开，PostgreSQL 连上后立刻
   `SET default_transaction_read_only = on` 并把 `SHOW` 回来的值核对一遍。
第 24 轮两份复评各自证明了只有第 1 层会漏：`select ... into t`、`select nextval('s')`
都能穿过正则，只是本地恰好是 SQLite 才没写成。

用法：
    python scripts/q.py "select count(*) from predictions"
    python scripts/q.py --sql "select ..." --limit 20
    python scripts/q.py --production "select ..."      # 真要查生产必须显式说
"""
import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

WRITE_WORDS = re.compile(
    r'\b(insert|update|delete|drop|alter|truncate|create|vacuum|attach|'
    r'pragma\s*=\s*|call|execute|grant|revoke|set|'
    # 第 24 轮两份复评共同抓的缺口：`select ... into t` 在 PostgreSQL 里**就是**建表，
    # `nextval/setval` 会真的推进序列，`copy ... to` 会写文件/表 —— 这些都以 select 开头，
    # 上一版闸门全部放行，靠的只是"本地是 SQLite 恰好语法不通"。
    r'into|nextval|setval|copy|program|dblink|lo_import|lo_export|'
    r'refresh|checkpoint|listen|unlisten|notify|commit|rollback|do)\b', re.I)
# `create or replace` 才是写；`select replace(name,'A','B')` 是读函数。
# 上一版把裸 `replace` 列进黑名单，结果连"看一眼基金名"都要被拒，逼人退回 `python -c`
# （那才是真正绕开守卫的那条路）。
WRITE_WORDS_OR_REPLACE = re.compile(r'\bor\s+replace\b', re.I)


def _strip_literals(sql):
    """把字符串字面量与引号标识符换成占位符再扫关键词。

    不剥就会假阳性：`select * from posts where content like '%delete%'` 里的
    delete 在引号里，是数据不是语句。
    """
    out = re.sub(r"'(?:[^']|'')*'", "'_LIT_'", sql)
    return re.sub(r'"(?:[^"])*"', '"_ID_"', out)


def _assert_read_only(sql):
    """白名单式判"只读"：先剥掉注释与字面量，再以 select/with/explain/values 开头才算。

    用黑名单（"没有 delete 就放行"）是不够的：`select ... ; drop table x` 这种
    多语句串起来就能骗过关键词检查，所以这里同时要求**只有一条语句**且以读关键字开头。
    真正的兜底不在这里，在 `_connect_sqlite_ro` / 生产的 `default_transaction_read_only`
    ——正则挡不住所有方言写法，引擎挡得住。
    """
    stripped = re.sub(r'--[^\n]*', '', sql)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.S).strip().rstrip(';').strip()
    scan = _strip_literals(stripped)
    if ';' in _strip_literals(re.sub(r"'(?:[^']|'')*'", '', stripped)):
        raise ValueError('只允许单条语句（多语句里可能藏着写操作）')
    head = scan.split(None, 1)[0].lower() if scan else ''
    if head not in ('select', 'with', 'explain', 'values'):
        raise ValueError('只读工具，首词必须是 select/with/explain/values，当前是 %r' % head)
    hit = WRITE_WORDS.search(scan)
    if hit:
        raise ValueError('语句里出现写操作关键字 %r：本工具只读' % hit.group(0))
    hit2 = WRITE_WORDS_OR_REPLACE.search(scan)
    if hit2:
        raise ValueError('`create or replace` 是写操作：本工具只读')
    return stripped


def _connect_sqlite_ro(url):
    """以 URI `mode=ro` 打开 SQLite，返回 sqlite3 连接（有 .execute/.close，Cursor 有 keys()）。

    为什么不用 SQLAlchemy：`sqlite:///path` 这个 URL 形状里没有 place-holder 可以传
    `uri=True`，而 pysqlite 的 `check_same_thread` 之类的 connect_args 会**整体替换**
    它自己拼出的 database 参数（`src/models/database.py` 里就是靠这个坑写过注释）。
    直接 sqlite3 反而少一层猜测。
    """
    import sqlite3
    import urllib.parse
    body = url[len('sqlite:///'):] if url.lower().startswith('sqlite:///') else url
    body = body.split('?', 1)[0]
    path = urllib.parse.unquote(body.replace('\\', '/'))
    uri = 'file:' + urllib.parse.quote(path, safe='/:@') + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


def main():
    ap = argparse.ArgumentParser(description='fund-insight 一次性只读查询（默认查本地镜像库）')
    ap.add_argument('sql', nargs='?', help='SQL 语句（select/with/explain）')
    ap.add_argument('--sql', dest='sql_kw', help='同位置参数，方便加引号')
    ap.add_argument('--limit', type=int, default=50, help='最多打印多少行（默认 50）')
    ap.add_argument('--production', action='store_true',
                    help='显式声明查生产（只读）；不填就查本地镜像')
    ap.add_argument('--db', help='额外的 sqlite 文件路径（默认 data/fund_insight.db）')
    args = ap.parse_args()

    sql = (args.sql or args.sql_kw or '').strip()
    if not sql:
        print('用法：python scripts/q.py "select count(*) from predictions"')
        return 2
    try:
        sql = _assert_read_only(sql)
    except ValueError as exc:
        print('[abort] %s' % exc)
        return 4

    if args.production:
        # 真要看线上数字也要走这个入口：读 .env 的连接串，但**仍然只读**
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        if not url.lower().startswith(('postgres', 'postgresql')):
            print('[abort] --production 要求 .env 的 DATABASE_URL 指向 PostgreSQL')
            return 4
        print('[target] 线上生产库（只读）：%s' % url.split('@')[-1])
    else:
        from _db_guard import pin_local_sqlite
        if args.db:
            os.environ['LOCAL_DB_URL'] = args.db
        url = pin_local_sqlite(use_mirror_default=True)
        print('[target] 本地镜像（只读）')

    import sqlalchemy as sa
    is_pg = url.lower().startswith('postgres')
    if is_pg:
        engine = sa.create_engine(url)
        conn = engine.connect()
        # 生产侧同样要引擎级保证：把会话默认事务设成只读，并**读回来确认它真的生效**
        # （第 16 轮教训：一个自称生效却没人验过的桩，比没有桩更糟）。
        conn.exec_driver_sql('SET default_transaction_read_only = on')
        got = conn.exec_driver_sql('SHOW default_transaction_read_only').scalar()
        if str(got).strip().lower() not in ('on', 'true', '1'):
            print('[abort] 生产会话没能进入只读模式（SHOW 回 %r）：拒绝执行' % got)
            conn.close()
            engine.dispose()
            return 4
        print('[guard] 会话已置 default_transaction_read_only=on')
    else:
        # 引擎级只读：SQLite 用 URI `mode=ro` 打开，任何写都会被判
        # "attempt to write a readonly database"。为什么不只靠上面的正则：
        # 正则是我的猜测，mode=ro 是引擎的事实。
        conn = _connect_sqlite_ro(url)
    try:
        result = conn.execute(sa.text(sql)) if is_pg else conn.execute(sql)
        # sqlite3.Cursor 只有 `.description`、没有 `.keys()`（`.keys()` 在 sqlite3.Row 上），
        # SQLAlchemy 的 CursorResult 反过来以 keys() 为主 —— 两边统一成一个列表。
        keys = list(result.keys()) if is_pg else [d[0] for d in (result.description or [])]
        if not keys:
            print('[ok] 执行完成（无结果集）')
            return 0
        print('\t'.join(keys))
        n = 0
        for row in result:
            print('\t'.join('' if v is None else str(v) for v in row))
            n += 1
            if n >= args.limit:
                print('[…] 已截断到 --limit %d' % args.limit)
                break
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
