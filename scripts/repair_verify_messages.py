# -*- coding: utf-8 -*-
"""修 D1 造成的数据损伤：把被体检结论刷掉的"面向用户理由"从备份里认回来。

背景：`apply_results` 以前无条件写 `verify_message = 体检语句`，
2026-09-21 09:32 那次 `--apply` 把 143 行的用户可见理由刷成
"代码在基金域的品种名与映射名一致（Jaccard …）"，
连老板手定的"无债市标的时取证券/券商 ETF"和 AI 批次的选基解释一起没了。
代码已改（只有本轮真的改结论才写这一列），这里是**数据返修**。

    python scripts/repair_verify_messages.py           # 只报要改什么
    python scripts/repair_verify_messages.py --apply   # 真改
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import _db_guard  # noqa: E402

BACKUP = os.path.join(ROOT, 'data', 'backups', 'pre-sweep-20260921-093219.db')
AUDIT_PREFIX = '代码在基金域的品种名与映射名一致'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    url = _db_guard.pin_local_sqlite(use_mirror_default=True)
    assert url.startswith('sqlite'), url
    if not os.path.exists(BACKUP):
        print('[abort] 备份不在：%s —— 停止，不猜理由' % BACKUP)
        return 2
    # 先把备份固定成一份只读副本：万一要重跑，源头不能被二次污染
    snap = BACKUP.replace('.db', '.snap-%s.db' % datetime.now().strftime('%H%M%S'))
    shutil.copy2(BACKUP, snap)

    from src.models.database import SessionLocal, SectorFundMapping
    old = sqlite3.connect('file:%s?mode=ro' % snap.replace('\\', '/'), uri=True)
    before = {row[0]: (row[1], row[2]) for row in old.execute(
        'select id, sector_name, verify_message from sector_fund_mapping')}
    old.close()

    db = SessionLocal()
    rows = db.query(SectorFundMapping).order_by(SectorFundMapping.id).all()
    restore = []
    for r in rows:
        cur = r.verify_message or ''
        prev = (before.get(r.id) or ('', ''))[1] or ''
        if not cur.startswith(AUDIT_PREFIX):
            continue
        if not prev or prev.startswith(AUDIT_PREFIX):
            continue
        restore.append((r, prev))
    print('[待返修] %d 行（当前是体检语句、备份里有更早的用户可见理由）' % len(restore))
    for r, prev in restore[:8]:
        print('   id %-4s %-10s %-20s ← %s' % (r.id, r.sector_name, r.fund_name, prev[:58]))
    owner = [(r.id, r.sector_name, r.verify_message) for r in rows
             if r.owner_locked or r.reviewed_by == 'owner']
    print('[老板锁定行现状] %s' % owner)
    if not args.apply:
        print('\n[dry-run] 未改库。确认无误加 --apply（副本已存 %s）' % os.path.basename(snap))
        return 0
    for r, prev in restore:
        r.verify_message = prev
    db.commit()
    left = sum(1 for r in db.query(SectorFundMapping).all()
               if (r.verify_message or '').startswith(AUDIT_PREFIX))
    print('[完成] 返修 %d 行；仍以体检语句为理由的剩 %d 行'
          % (len(restore), left))
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
