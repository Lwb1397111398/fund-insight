# -*- coding: utf-8 -*-
"""把老板点名"没有对口基金、只能取关联度最大替代"的板块落成 owner_locked 映射行。

为什么需要：产品规则 2 说这些映射是有意为之，不能被 agent"修"掉。但内置字典里的板块
在 DB 里根本没有行，`apply_decision` 找不到行就会新插一条 agent 结论，从此盖过内置值。
所以先把这些行落成 `owner_locked=1 / reviewed_by='owner'`，agent 遇到就跳过。

用法：
    DATABASE_URL="sqlite:///<本地镜像库>" python scripts/seed_owner_proxies.py
"""
import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    _db_guard.pin_local_sqlite()
    from src.models.database import FundInfo, SectorFundMapping, SessionLocal
    from src.services.sector_fund_agent import DELIBERATE_PROXIES

    db = SessionLocal()
    try:
        from src.fund.fund_api import fund_api

        for sector, (code, reason) in DELIBERATE_PROXIES.items():
            # 官方名现取，不写死：我抄的名字可能就是下一个错配
            name = (fund_api.verify_fund_fetchable(code).get('api_name') or '').strip()
            row = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector,
                SectorFundMapping.fund_code == code).first()
            print('%-10s -> %s %s   %s' % (sector, code, name, reason))
            if args.dry_run:
                continue
            if not db.query(FundInfo).filter_by(fund_code=code).first():
                db.add(FundInfo(fund_code=code, fund_name=name, sector_type=sector))
            if row is None:
                row = SectorFundMapping(sector_name=sector, fund_code=code,
                                        fund_name=name, is_active=True)
                db.add(row)
            row.reviewed = True
            row.reviewed_by = 'owner'
            row.owner_locked = True
            row.match_source = 'manual'
            row.match_kind = 'proxy'
            row.verify_message = '老板确认的有意代理：' + reason
            row.verified_at = datetime.now()
            row.is_fetchable = True
        db.commit()
        print('[ok] 有意代理已锁定（agent 不会覆盖 owner_locked 行）')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
