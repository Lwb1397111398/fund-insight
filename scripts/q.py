# -*- coding: utf-8 -*-
"""一次性只读查询：把"安全的做法"做成最省事的做法。

为什么要有这个文件：2026-09-22 我为了图快，写了条 `python -c "...from src.models.database
import engine..."` 的一次性查询，只设了 `LOCAL_DB_URL` 环境变量而**没调用守卫** ——
`src.models.database` 读的是 `DATABASE_URL`，`.env` 里它是生产 Supabase，于是两条 SELECT
直接打到线上。守卫存在并不能阻止"绕过它"，所以这里给一条绕不过去的捷径：
`scripts/q.py` 默认只读、先 pin 本地镜像、拒绝任何写语句。

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
    r'\b(insert|update|delete|drop|alter|truncate|create|replace|vacuum|attach|'
    r'pragma\s*=\s*|call|execute|grant|revoke|set)\b', re.I)


def _assert_read_only(sql):
    """白名单式判"只读"：先剥掉注释，再以 select/with/pragma-读/explain 开头才算。

    用黑名单（"没有 delete 就放行"）是不够的：`select ... ; drop table x` 这种
    多语句串起来就能骗过关键词检查，所以这里同时要求**只有一条语句**且以读关键字开头。
    """
    stripped = re.sub(r'--[^\n]*', '', sql)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.S).strip().rstrip(';').strip()
    if ';' in stripped:
        raise ValueError('只允许单条语句（多语句里可能藏着写操作）')
    head = stripped.split(None, 1)[0].lower() if stripped else ''
    if head not in ('select', 'with', 'explain', 'values'):
        raise ValueError('只读工具，首词必须是 select/with/explain/values，当前是 %r' % head)
    hit = WRITE_WORDS.search(stripped)
    if hit:
        raise ValueError('语句里出现写操作关键字 %r：本工具只读' % hit.group(0))
    return stripped


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
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        result = conn.execute(sa.text(sql))
        keys = list(result.keys())
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
    engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
