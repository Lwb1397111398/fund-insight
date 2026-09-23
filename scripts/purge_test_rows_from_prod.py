# -*- coding: utf-8 -*-
"""清理**我的测试会话误写进生产库**的假数据（默认只预检，真删要确认词，删前逐行备份）。

发生了什么（2026-09-23，北京时间 10:17 与 10:37 两批）：`tests/conftest.py` 里我在
"钉 `DATABASE_URL` 到临时 SQLite"那行**之前**导入了一次 `src.utils.…`，而 `src/__init__.py`
会拉起 `src.core.config` ⇒ 应用 engine 绑上了 `.env` 里的生产 Supabase。
后果：几条夹具数据落在了线上（只读预检实测，均在 `>= 2026-09-23 10:00`）：
    bloggers        8 行，名字全是 `T-基金列表体检博主`（id 334-341）
    posts           8 行，挂在上面那 8 个博主下
    system_config   1 行，`config_key = nav_backfill_proof:TEST_BF`
    fund_info       1 行，`159877 / 占位基金`（id 302，updated_at 今天）
这些行会进博主榜与统计 ⇒ 老板界面上的数被我的测试污染了。conftest 的顺序已经修好，
并加了一条"钉库之前不许导入 `src`"的断言（见 `tests/conftest.py`）。

为什么单独写一个脚本而不是手敲 DELETE：这仓库为"批量删除"反复付过账 ⇒
一律 先备份被删的行、逐行 receipt、`--restore-from` 能按原样插回去。

为什么谓词按**名字/键**而不是按时间：时间窗能圈进来的可能是老板真数据。
名字/键是夹具里写死的字符串，匹配不到真博主。（时间只用来打印"我看到了什么"。）

用法：
    python scripts/purge_test_rows_from_prod.py --production              # 只读预检 + 出计划
    python scripts/purge_test_rows_from_prod.py --production --apply --confirm CLEAN-TEST-ROWS
    python scripts/purge_test_rows_from_prod.py --production --restore-from backup/purge-test-rows-*.json --confirm RESTORE-TEST-ROWS
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

CONFIRM_TOKEN = 'CLEAN-TEST-ROWS'
RESTORE_CONFIRM_TOKEN = 'RESTORE-TEST-ROWS'
# 夹具里写死的字符串：只按这些匹配，绝不按时间窗删。
TEST_BLOGGER_NAME = 'T-基金列表体检博主'
TEST_CONFIG_KEY = 'nav_backfill_proof:TEST_BF'
TEST_FUND = ('159877', '占位基金')
SINCE = '2026-09-23 10:00'


def _reject_local(url):
    """这个脚本的靶子必须是远程库：本地镜像没有这些行，误跑只会报"干净"给人假安心。"""
    if str(url).lower().startswith('sqlite'):
        print('[abort] 当前连的是 SQLite（%s）—— 本脚本只处理**生产**误写，'
              '要查镜像请直接用 scripts/q.py。' % url)
        raise SystemExit(4)


def plan(db):
    """按名字/键取出待删的行（含外键顺序：先 posts，再 bloggers）。"""
    import sqlalchemy as sa

    from src.models.database import Blogger, FundInfo, Prediction, Post, SectorFundMapping, SystemConfig

    bloggers = db.query(Blogger).filter(Blogger.name == TEST_BLOGGER_NAME).all()
    ids = [b.id for b in bloggers]
    posts = db.query(Post).filter(Post.blogger_id.in_(ids)).all() if ids else []
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == TEST_CONFIG_KEY).all()
    code, name = TEST_FUND
    funds = db.query(FundInfo).filter(FundInfo.fund_code == code, FundInfo.fund_name == name).all()
    # 这只基金有没有被真数据引用？被引用就不能删（宁可留着一条名字古怪的档案）
    refs = (db.query(sa.func.count()).select_from(Prediction)
            .filter(Prediction.fund_code == code).scalar() or 0)
    refs += (db.query(sa.func.count()).select_from(SectorFundMapping)
             .filter(SectorFundMapping.fund_code == code).scalar() or 0)
    return {'bloggers': bloggers, 'posts': posts, 'config': cfg, 'funds': funds, 'fund_refs': refs}


def _row(obj):
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(type(obj))
    cols = [c.key for c in mapper.columns]
    d = {c: getattr(obj, c) for c in cols}
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return {'__table__': mapper.local_table.name, **d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--production', action='store_true', required=True,
                    help='显式声明靶子是生产库（缺了就拒跑）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', default='')
    ap.add_argument('--restore-from', default='')
    args = ap.parse_args()

    if args.restore_from:
        return restore(args)

    from src.models.database import SessionLocal, engine
    _reject_local(engine.url)
    db = SessionLocal()
    try:
        p = plan(db)
        n_posts, n_blog = len(p['posts']), len(p['bloggers'])
        print('[target] %s' % str(engine.url).split('@')[-1])
        print('[预检] 按夹具名/键匹配：posts %d 行 → bloggers %d 行 → system_config %d 行 → fund_info %d 行'
              % (n_posts, n_blog, len(p['config']), len(p['funds'])))
        print('[预检] 时间核对：这些行的 created_at 是否都落在 %s 之后 —— '
              '见下面逐行清单，有一条不是就说明谓词圈到了真数据，立即停手' % SINCE)
        rows = ([_row(x) for x in p['posts']] + [_row(x) for x in p['bloggers']]
                + [_row(x) for x in p['config']] + [_row(x) for x in p['funds']])
        for r in rows:
            when = r.get('created_at') or r.get('updated_at')
            print('   - %-16s id=%-6s %s  %s' % (r['__table__'], r.get('id'),
                                                 (r.get('name') or r.get('config_key') or r.get('fund_name') or '')[:24], when))
        if p['fund_refs']:
            print('[abort] `%s` 仍被 %d 行引用 ⇒ 不删这一行（宁可留着名字古怪的档案）'
                  % (TEST_FUND[0], p['fund_refs']))
            funds, rows = [], [r for r in rows if r['__table__'] != 'fund_info']
        else:
            funds = p['funds']
        if not args.apply:
            print('（dry-run，未改动任何数据。真删：--apply --confirm %s）' % CONFIRM_TOKEN)
            return 0
        if args.confirm != CONFIRM_TOKEN:
            print('[abort] 真删生产数据必须带 --confirm %s' % CONFIRM_TOKEN)
            return 3
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        os.makedirs(os.path.join(ROOT, 'backup'), exist_ok=True)
        path = os.path.join(ROOT, 'backup', 'purge-test-rows-%s.json' % stamp)
        with io.open(path, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print('[备份] 已落盘 %s（%d 行）' % (path, len(rows)))
        deleted = 0
        for obj in p['posts'] + p['bloggers'] + p['config'] + funds:
            tbl, oid = type(obj).__tablename__, obj.id
            db.delete(obj)
            db.flush()
            print('   [已删] %s id=%s' % (tbl, oid))
            deleted += 1
        db.commit()
        after = plan(db)
        left = sum(len(after[k]) for k in ('posts', 'bloggers', 'config', 'funds'))
        print('[回执] 删除 %d 行；再跑一次预检仍匹配到 %d 行（应为 0）' % (deleted, left))
        print('[回执] 还原：python scripts/purge_test_rows_from_prod.py --production '
              '--restore-from %s --confirm %s' % (path, RESTORE_CONFIRM_TOKEN))
        return 0 if left == 0 else 5
    finally:
        db.close()


def restore(args):
    if args.confirm != RESTORE_CONFIRM_TOKEN:
        print('[abort] 往生产插回数据必须带 --confirm %s' % RESTORE_CONFIRM_TOKEN)
        return 3
    from src.models.database import SessionLocal, engine
    _reject_local(engine.url)
    with io.open(args.restore_from, encoding='utf-8') as fh:
        rows = json.load(fh)
    db = SessionLocal()
    try:
        from sqlalchemy import text
        for r in rows:
            tbl = r.pop('__table__')
            cols = list(r.keys())
            sql = 'insert into %s (%s) values (%s)' % (
                tbl, ', '.join(cols), ', '.join(':' + c for c in cols))
            db.execute(text(sql), {c: r[c] for c in cols})
            print('   [已插回] %s id=%s' % (tbl, r.get('id')))
        db.commit()
        print('[回执] 插回 %d 行（按备份原样，含 id）' % len(rows))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
