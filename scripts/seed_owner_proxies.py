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
    # 第 20 轮 MAJOR-1：这个脚本直写 `reviewed_by='owner' + owner_locked=True`，
    # 而那是全仓唯一一条**不经 owner_confirm** 的豁免来源（豁免清单是代码字面量
    # `sector_fund_agent.DELIBERATE_PROXIES`）。AGENTS.md 写"只能由显式确认换来"，
    # 少这一句就还是假话。真写必须带 --owner-confirm。
    ap.add_argument('--owner-confirm', metavar='TOKEN',
                    help='真写必须等于 SEED-PROXY：这次操作会给行加上老板署名与体检豁免')
    args = ap.parse_args()
    if not args.dry_run and args.owner_confirm != 'SEED-PROXY':
        print('[abort] 这个脚本会给行盖"老板已确认 + 体检豁免"，真写必须 '
              '--owner-confirm SEED-PROXY（第 20 轮 MAJOR-1：豁免只能由显式确认换来，'
              '脚本也不例外）。只看计划请加 --dry-run。')
        return 4

    _db_guard.pin_local_sqlite(use_mirror_default=True)
    from src.models.database import FundInfo, SectorFundMapping, SessionLocal
    from src.services.sector_fund_agent import DELIBERATE_PROXIES

    db = SessionLocal()
    try:
        from src.fund.fund_api import fund_api

        skipped = []
        for sector, (code, reason) in DELIBERATE_PROXIES.items():
            # 官方名现取，不写死：我抄的名字可能就是下一个错配
            probe = fund_api.verify_fund_fetchable(code)
            name = (probe.get('api_name') or '').strip()
            row = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector,
                SectorFundMapping.fund_code == code).first()
            # 第 38 轮 A 席 MAJOR-3：以前只取名字、**从不读探针的结论**，于是
            # "查无此码"也照样落 `is_fetchable=True` + 空基金名 + 一行幻影 `fund_info`。
            # 第 39 轮两份同点：只挡 `ok=False` 是补了一半 —— 探针自己就有"ok 但没拿到名字"
            # 这一格（`fund_api.py` 的文案"可抓取净值数据（接口未返回名称）"，注释还写明
            # "场内 ETF 常常拿不到实时名称"，而这张名单几乎全是 ETF）。空名行在服务端 HTTP 写
            # 路径上是被明确拒收的（`fund_name_wiped` / `row_has_no_fund_name`），
            # 因为名字一空，体检从此判"unknown＝不指控"。
            why = None
            if not probe.get('ok'):
                why = '探针说取不到（%s）' % (probe.get('message') or 'ok=False')
            elif not name:
                why = '探针说可抓但**没拿到官方名**（空名行会让体检失明）'
            if why:
                skipped.append('%s→%s（%s）' % (sector, code, why))
                print('%-10s -> %s   [跳过：%s] %s' % (sector, code, why, reason))
                continue
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
        if skipped:
            print('[回执] %d 条被跳过（探针说取不到，没盖豁免章、没造档案行）：' % len(skipped))
            for line in skipped:
                print('   ' + line)
            print('[回执] 这些行需要人工确认：要么换成还在的标的，要么从豁免名单里去掉')
            return 5
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
