# -*- coding: utf-8 -*-
"""把**生产现有的板块映射**整表抓下来留前像（回写前的唯一退路）。

为什么单独一个脚本：`push_sector_mappings_to_prod.py` 只做"按板块名定向改/建"，
它拒收的行、以及被改掉的旧值，服务端不会替我们留底。第 8 轮评审因此判 MAJOR-5：
**没有生产前像 = 回写不可逆**（本地有 `--restore-from` 清单，生产什么都没有）。
这个脚本把 GET /api/config/sector-mappings 的原样结果带时间戳落到
`docs/迭代计划/run-2026-09-20/`。

**它只是一份"看得懂的回执"，不是完整前像**（第 9 轮 MAJOR-3）：那个 GET 目前不返回
`owner_locked / reviewed_by / confidence / evidence / match_source / verified_at`，
所以拿它"逐行 PUT 回去"既补不回免疫状态，`update_mapping` 还会把碰到的每一行
盖成 `reviewed + owner_locked + reviewed_by='owner'`，等于用恢复动作伪造老板意志。
真要回滚生产映射，用 `--apply` 前服务端本库的备份（Render 侧快照）或
`audit-import` 明确写回审计字段，别把这个文件当退路。

只读，不写任何东西。口令只从环境变量 `ACCESS_PASSWORD` 读。

    ACCESS_PASSWORD=... python scripts/snapshot_prod_mappings.py
    ACCESS_PASSWORD=... python scripts/snapshot_prod_mappings.py --base https://…

**`--via-db` 才是完整前像**：绕过接口、直接只读 SELECT 整表，`owner_locked /
reviewed_by / confidence / evidence / match_source / verified_at` 一列不落。
接口那条路留给"不能直连数据库"的场合，并会在回执里明说自己不完整。
生产侧连上后立刻 `SET default_transaction_read_only=on` 并读回来核对（与 `scripts/q.py`
同一套两层保证）；不带 `--production` 时一律走 `_db_guard` 钉本地镜像 + SQLite `mode=ro`。

    python scripts/snapshot_prod_mappings.py --via-db --out E:/tmp/prod-pre.json   # 生产
"""
import argparse
import io
import json
import os
import sys
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')
DEFAULT_BASE = 'https://fund-insight.onrender.com'
PATH = '/api/config/sector-mappings'
TABLE = 'sector_fund_mapping'


def fetch(base, password, timeout=120):
    req = urllib.request.Request(base.rstrip('/') + PATH, method='GET',
                                 headers={'X-Access-Password': password})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode('utf-8', 'replace') or '{}')


def fetch_via_db(production=False):
    """只读整表。返回 (列名列表, 行字典列表)。任何写路径在两层上都会被拒。"""
    if production:
        import sqlalchemy as sa
        from dotenv import load_dotenv
        from q import _pg_read_only_probe        # 同一个只读机制，别抄第二份
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        if not url.lower().startswith(('postgres', 'postgresql')):
            raise RuntimeError('--via-db --production 要求 .env 的 DATABASE_URL 指向 PostgreSQL')
        engine = sa.create_engine(url, execution_options={'postgresql_readonly': True})
        with engine.connect() as conn:
            why = _pg_read_only_probe(conn)      # 真试一次写，被拒才算只读成立
            if why:
                raise RuntimeError(why)
            res = conn.execute(sa.text('select * from %s order by sector_name' % TABLE))
            keys = list(res.keys())
            rows = [dict(zip(keys, r)) for r in res]
        engine.dispose()
        return keys, rows

    from _db_guard import pin_local_sqlite
    from q import _connect_sqlite_ro
    url = pin_local_sqlite(use_mirror_default=True)
    conn = _connect_sqlite_ro(url)
    try:
        cur = conn.execute('select * from %s order by sector_name' % TABLE)
        keys = [d[0] for d in cur.description]
        rows = [dict(zip(keys, r)) for r in cur]
    finally:
        conn.close()
    return keys, rows



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.getenv('APP_BASE_URL', DEFAULT_BASE))
    ap.add_argument('--out', default=None, help='默认写进 docs 运行目录并带时间戳')
    ap.add_argument('--via-db', action='store_true',
                    help='绕过接口、只读直连数据库抓整表（唯一能拿到审计列的路径）')
    ap.add_argument('--production', action='store_true',
                    help='与 --via-db 连用：连 .env 指向的生产库（仍只读）；不配则钉本地镜像')
    args = ap.parse_args()

    password = os.getenv('ACCESS_PASSWORD', '')
    if not password and not args.via_db:
        print('[abort] 环境变量 ACCESS_PASSWORD 未设置（口令不进命令行/代码/日志）')
        return 2
    via_db = args.via_db
    if via_db:
        try:
            keys, rows = fetch_via_db(args.production)
        except Exception as exc:
            print('[abort] 只读取整表失败：%s' % str(exc)[:220])
            return 3
        base = '数据库直连（只读）' if args.production else '本地镜像（只读，mode=ro）'
    else:
        try:
            status, body = fetch(args.base, password)
        except Exception as exc:
            print('[abort] 取不到生产映射：%s' % str(exc)[:200])
            return 3
        if status != 200:
            print('[abort] 生产返回 HTTP %s：%s' % (status, str(body)[:200]))
            return 3
        keys = []
        rows = (body.get('data') or {}).get('mappings') or []
        base = args.base
    if not rows:
        # 空表要么是接口口径变了，要么生产真的什么都没有 —— 两种都不能当"前像"存下来
        print('[abort] 映射为空（接口可能改了返回结构），不落盘')
        return 4
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = args.out or os.path.join(OUT_DIR, 'prod-mappings-pre-%s.json' % stamp)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump({'taken_at': datetime.now().isoformat(timespec='seconds'),
                   'source': base, 'via_db': via_db, 'count': len(rows),
                   'columns': keys or sorted(rows[0].keys()), 'mappings': rows},
                  f, ensure_ascii=False, indent=1, default=str)
    locked = sum(1 for r in rows if r.get('owner_locked') or r.get('reviewed_by') == 'owner')
    unreviewed = sum(1 for r in rows if not r.get('reviewed'))
    missing = [c for c in ('owner_locked', 'reviewed_by', 'evidence')
               if rows and rows[0].get(c) is None and c not in rows[0]]
    if missing:
        # GET 不带这些列时 `locked` 恒为 0：报出来会被当成"生产没人锁定"的假证据
        print('[前像] %d 行已存 %s（待审查 %d 行）；缺少列 %s ⇒ 这份文件不足以逐行回滚'
              % (len(rows), path, unreviewed, missing))
        print('[提示] 要完整前像请加 --via-db（只读直连数据库，审计列也在）')
        return 0
    print('[前像] %d 行 × %d 列已存 %s（老板锁定 %d 行、待审查 %d 行）'
          % (len(rows), len(rows[0]), path, locked, unreviewed))
    print('[提示] 回写请跑：python scripts/export_repaired_mappings.py 后 '
          'ACCESS_PASSWORD=... python scripts/push_sector_mappings_to_prod.py --limit 5')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
