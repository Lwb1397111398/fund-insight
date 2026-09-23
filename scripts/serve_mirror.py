# -*- coding: utf-8 -*-
"""把页面跑在**本地镜像库**上做浏览器核验（`.env` 指生产时唯一安全的起法）。

为什么要有：`python -m src` 会读 `.env` 里的 `DATABASE_URL`，而那玩意现在指向生产 Supabase。
要在真实浏览器里看"页面报的数对不对"，就必须先把库钉到 `data/fund_insight.db`
（和 `scripts/q.py` 同一个道理：把安全做法做成最省事的做法）。

用法：
    python scripts/serve_mirror.py                  # 默认 127.0.0.1:8012
    python scripts/serve_mirror.py --port 8012

口令：默认现造一个一次性口令并打印出来（只绑 127.0.0.1，且镜像库可整份还原）。
要沿用 `.env` 里的真口令请自己显式设 `ACCESS_PASSWORD` —— 别把它抄进任何文档或提交。
"""
import argparse
import io
import os
import secrets
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from _db_guard import pin_local_sqlite  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8012)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    if args.host not in ('127.0.0.1', 'localhost'):
        print('[abort] 这个入口只许绑本机回环：它跑的是镜像库 + 临时口令。')
        return 4
    pin_local_sqlite(use_mirror_default=True)
    url = os.environ['DATABASE_URL']
    if not url.lower().startswith('sqlite'):
        print('[abort] 钉完之后仍不是 SQLite：%s' % url.split('@')[-1])
        return 4
    ephemeral = not os.environ.get('ACCESS_PASSWORD')
    if ephemeral:
        os.environ['ACCESS_PASSWORD'] = secrets.token_urlsafe(12)
    print('库：%s' % url)
    print('口令：%s' % ('（本次现造，见下面一行）' if ephemeral else '（沿用环境里已有的）'))
    if ephemeral:
        print('ACCESS_PASSWORD=%s' % os.environ['ACCESS_PASSWORD'])
    print('http://%s:%d/  （Ctrl+C 停）' % (args.host, args.port))
    import uvicorn
    uvicorn.run('src.api.main:app', host=args.host, port=args.port, log_level='warning')
    return 0


if __name__ == '__main__':
    sys.exit(main())
