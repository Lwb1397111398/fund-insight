# -*- coding: utf-8 -*-
"""把**生产现有的板块映射**整表抓下来留前像（回写前的唯一退路）。

为什么单独一个脚本：`push_sector_mappings_to_prod.py` 只做"按板块名定向改/建"，
它拒收的行、以及被改掉的旧值，服务端不会替我们留底。第 8 轮评审因此判 MAJOR-5：
**没有生产前像 = 回写不可逆**（本地有 `--restore-from` 清单，生产什么都没有）。
这个脚本把 GET /api/config/sector-mappings 的原样结果带时间戳落到
`docs/迭代计划/run-2026-09-20/`，回写前先跑一次，事后要退就照它逐行 PUT 回去。

只读，不写任何东西。口令只从环境变量 `ACCESS_PASSWORD` 读。

    ACCESS_PASSWORD=... python scripts/snapshot_prod_mappings.py
    ACCESS_PASSWORD=... python scripts/snapshot_prod_mappings.py --base https://…
"""
import argparse
import io
import json
import os
import sys
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')
DEFAULT_BASE = 'https://fund-insight.onrender.com'
PATH = '/api/config/sector-mappings'


def fetch(base, password, timeout=120):
    req = urllib.request.Request(base.rstrip('/') + PATH, method='GET',
                                 headers={'X-Access-Password': password})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode('utf-8', 'replace') or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.getenv('APP_BASE_URL', DEFAULT_BASE))
    ap.add_argument('--out', default=None, help='默认写进 docs 运行目录并带时间戳')
    args = ap.parse_args()

    password = os.getenv('ACCESS_PASSWORD', '')
    if not password:
        print('[abort] 环境变量 ACCESS_PASSWORD 未设置（口令不进命令行/代码/日志）')
        return 2
    try:
        status, body = fetch(args.base, password)
    except Exception as exc:
        print('[abort] 取不到生产映射：%s' % str(exc)[:200])
        return 3
    if status != 200:
        print('[abort] 生产返回 HTTP %s：%s' % (status, str(body)[:200]))
        return 3
    rows = (body.get('data') or {}).get('mappings') or []
    if not rows:
        # 空表要么是接口口径变了，要么生产真的什么都没有 —— 两种都不能当"前像"存下来
        print('[abort] 生产映射为空（接口可能改了返回结构），不落盘')
        return 4
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = args.out or os.path.join(OUT_DIR, 'prod-mappings-pre-%s.json' % stamp)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump({'taken_at': datetime.now().isoformat(timespec='seconds'),
                   'base': args.base, 'count': len(rows), 'mappings': rows},
                  f, ensure_ascii=False, indent=1)
    locked = sum(1 for r in rows if r.get('owner_locked') or r.get('reviewed_by') == 'owner')
    unreviewed = sum(1 for r in rows if not r.get('reviewed'))
    print('[前像] %d 行已存 %s（老板锁定 %d 行、待审查 %d 行）'
          % (len(rows), path, locked, unreviewed))
    print('[提示] 回写请跑：python scripts/export_repaired_mappings.py 后 '
          'ACCESS_PASSWORD=... python scripts/push_sector_mappings_to_prod.py --limit 5')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
