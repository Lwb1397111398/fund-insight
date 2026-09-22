# -*- coding: utf-8 -*-
"""一次性只读查询：把"安全的做法"做成最省事的做法。

为什么要有这个文件：2026-09-22 我为了图快，写了条 `python -c "...from src.models.database
import engine..."` 的一次性查询，只设了 `LOCAL_DB_URL` 环境变量而**没调用守卫** ——
`src.models.database` 读的是 `DATABASE_URL`，`.env` 里它是生产 Supabase，于是两条 SELECT
直接打到线上。守卫存在并不能阻止"绕过它"，所以这里给一条绕不过去的捷径：
`scripts/q.py` 默认只读、先 pin 本地镜像、拒绝任何写语句。

只读这件事有**两层**，第二层才是保证：
1. `_assert_read_only()` 的正则 —— 快、报错友好，但它是我对方言的猜测；
2. 引擎级 —— SQLite 用 URI `mode=ro` 打开；PostgreSQL 建引擎时带
   `isolation_level='READ ONLY'`（方言在**建连接时**就把会话特征设好，不等第一条语句），
   然后 `_pg_read_only_probe()` **真试一次写**（临时表），只有它报
   "read-only transaction" 才继续执行。
第 24 轮两份复评各自证明了只有第 1 层会漏：`select ... into t`、`select nextval('s')`
都能穿过正则，只是本地恰好是 SQLite 才没写成。
第 25 轮又把第 2 层的旧写法判为**恒真核对**：`SET default_transaction_read_only` 与业务
查询落在同一条事务里（SQLAlchemy 首条语句才 autobegin），该 GUC 只管"之后的事务"，
而 `SHOW` 读回的正是我刚设进去的会话值 ⇒ 永远不会报"没生效"。所以要探针，不要自报。

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
# 第 25 轮 A 又举出五条穿过上一版关键词表的：这些"函数调用"形式在 PG 上有真实副作用，
# 而且 `set_config` 连语法都长得像读。闸门只是第一层（真正的保证在引擎级只读上），
# 但报错友好度也重要 —— 让人看见"这条本来要被拒"，而不是执行完才发现变了。
WRITE_FUNCS = re.compile(
    r'\b(pg_advisory_lock|pg_advisory_unlock|pg_advisory_xact_lock|set_config|'
    r'pg_switch_wal|pg_reload_conf|pg_read_file|pg_read_binary_file|pg_ls_dir|'
    r'lo_get|lo_put|pg_terminate_backend|pg_cancel_backend|dblink_exec)\s*\(', re.I)
# `create or replace` 才是写；`select replace(name,'A','B')` 是读函数。
WRITE_WORDS_OR_REPLACE = re.compile(r'\bor\s+replace\b', re.I)

# 一次从左到右的分词：字符串/引号标识符与注释**互相包含**时谁先出现谁生效。
# 分成两步做会出错：先剥注释 ⇒ `like '%--%'` 被拦腰截断（第 25 轮 B 实测）；
# 先剥字面量 ⇒ 注释里一个英文撇号就把后半截当字符串吃掉。
_TOKENS = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"|--[^\n]*|/\*.*?\*/", re.S)


def _mask_literals(sql):
    """把字面量与注释换成占位符，供扫描用（执行时仍用原句）。"""
    return _TOKENS.sub(lambda m: "'_X_'" if m.group(0)[0] in "'\"" else ' ', sql)


def _assert_read_only(sql):
    """白名单式判"只读"：首词必须 select/with/explain/values、只允许一条语句、
    扫不到写关键词/写函数。真正的兜底不在这里，在 `_connect_sqlite_ro` /
    PG 的 `isolation_level='READ ONLY'` + `_pg_read_only_probe` —— 正则挡不住所有方言写法，
    引擎挡得住。
    """
    scan = _mask_literals(sql)
    if ';' in scan.strip().rstrip(';'):
        raise ValueError('只允许单条语句（多语句里可能藏着写操作）')
    head = scan.split(None, 1)[0].lower() if scan.strip() else ''
    if head not in ('select', 'with', 'explain', 'values'):
        raise ValueError('只读工具，首词必须是 select/with/explain/values，当前是 %r' % head)
    hit = WRITE_WORDS.search(scan)
    if hit:
        raise ValueError('语句里出现写操作关键字 %r：本工具只读' % hit.group(0))
    func = WRITE_FUNCS.search(scan)
    if func:
        raise ValueError('语句里调用有副作用的函数 %r：本工具只读' % func.group(1))
    if WRITE_WORDS_OR_REPLACE.search(scan):
        raise ValueError('`create or replace` 是写操作：本工具只读')
    return sql.strip().rstrip(';').strip()


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


def _pg_read_only_probe(conn):
    """**证明**这条连接写不了东西，而不是读一个我刚设进去的变量。

    第 25 轮两份复评共同判这条为 MAJOR：上一版发的是
    `SET default_transaction_read_only = on` + `SHOW default_transaction_read_only`，
    而 B 用事件监听复现出 `SET`、`SHOW`、业务查询全在**同一条事务**里
    （SQLAlchemy 首条语句才 autobegin），那个 GUC 只管"之后的事务"，
    `SHOW` 读回的又正是自己刚设的会话值 ⇒ 恒真，永远不会报"没生效"。
    现在改成：方言在建连时就把会话特征设成 READ ONLY（`isolation_level='READ ONLY'`），
    然后**真试一次写**（临时表），只有它报"read-only transaction"才算数。
    返回 None 表示只读成立，返回字符串表示原因（调用方必须中止）。
    """
    conn.exec_driver_sql('SAVEPOINT q_guard')
    try:
        conn.exec_driver_sql('CREATE TEMP TABLE _q_guard(x int)')
    except Exception as exc:
        msg = str(exc).lower()
        conn.exec_driver_sql('ROLLBACK TO SAVEPOINT q_guard')
        if 'read-only' in msg or 'read only' in msg:
            return None
        return '只读探针报错但不是只读错：%s' % str(exc)[:160]
    conn.exec_driver_sql('ROLLBACK TO SAVEPOINT q_guard')
    return '探针建临时表竟然成功 ⇒ 这条连接并不是只读，拒绝执行'


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
        # 第 25 轮 A/B 都点到：`--db` 指到别的文件时还印"本地镜像（只读）"，
        # 那正是第 23 轮那条"拿错库报数"的形态。回显真实路径。
        print('[target] 只读 SQLite：%s' % (url.split(':///', 1)[-1] or url))

    import sqlalchemy as sa
    is_pg = url.lower().startswith('postgres')
    if is_pg:
        # 只读由**方言在建连接时**设进会话特征（等价于
        # `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`），
        # 所以之后每一条事务都是只读 —— 不再依赖"我先 SET 再查"这种事务内动作。
        engine = sa.create_engine(url, isolation_level='READ ONLY')
        conn = engine.connect()
        why = _pg_read_only_probe(conn)
        if why:
            print('[abort] %s' % why)
            conn.close()
            engine.dispose()
            return 4
        print('[guard] 探针写临时表被数据库拒绝 ⇒ 这条连接确实只读')
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
