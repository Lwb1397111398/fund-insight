# -*- coding: utf-8 -*-
"""`fund_info`（基金列表本身）的身份体检与名称修复。

为什么需要它：本轮迭代一直在修**板块→基金映射**，第 9 轮把同一套判据对准 `fund_info`
才发现问题的另一半在更下游 —— 基金列表里有 **29 行**通不过体检（213 行中）：

- 15 行 `not_a_fund`：存的是**股票名**挂在别人的基金码上
  （000530 冰山冷热 / 000938 紫光股份 / 000970 中科三环 / 001309 德明利 /
   002154 报喜鸟 / 002261 拓维信息 / 002354 天娱数科 / 002413 雷科防务 …）。
  这些码在基金域里各自对应一只真基金（001309 其实是「东方红睿逸定开混合」），
  于是"能抓到净值"完全正常 —— 老板要的"注意是基金而不能是股票"在**这一层**从来没被查过。
- 14 行 `unknown`：`fund_name` 是**空串**，码本身没问题，但体检没法比对名字。
  它们背后挂着 **56 条未删除预测**（006105 33 条、513520 8 条、003033 6 条…），
  前端只能显示空名字，统计页也认不出是哪只基金。

本脚本只做**加性修复**：把空的 `fund_name` 用基金域自证到的品种名补上。
股票名那 15 行**不动**：删行会牵动 `fund_history` 与历史预测口径，属于要老板点头的破坏性操作，
这里只出 CSV 清单（`docs/迭代计划/run-2026-09-20/fund-info-identity-audit.csv`）等处理。

    python scripts/audit_fund_info_identity.py            # 看清单（默认不写）
    python scripts/audit_fund_info_identity.py --apply    # 只补空名字

生产同样有这批脏数据：先在 Render 上跑同一脚本的 dry-run，再 `--apply`。
"""
import argparse
import csv
import io
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='只补空 fund_name，别的一律不写')
    ap.add_argument('--rename-to-official', action='store_true',
                    help='把"股票名挂在别人基金码"的行改成基金域真名（加性改名，不删数据）')
    ap.add_argument('--limit', type=int, default=None, help='只处理前 N 行（先在云上小批试）')
    args = ap.parse_args()

    _db_guard.pin_local_sqlite()
    from src.models.database import SessionLocal, FundInfo
    from src.services.sector_identity_audit import arbitrate_mapping

    db = SessionLocal()
    try:
        rows = db.query(FundInfo).order_by(FundInfo.fund_code).all()
        if args.limit:
            rows = rows[:args.limit]
        print('[体检] fund_info %d 行' % len(rows))
        report, filled, renamed = [], 0, 0
        for r in rows:
            try:
                v = arbitrate_mapping(r.fund_code, r.fund_name or '', '')
            except Exception as exc:
                v = {'verdict': 'probe_error', 'official_name': '', 'reason': str(exc)[:80]}
            verdict = v.get('verdict')
            official = (v.get('official_name') or '').strip()
            if not (r.fund_name or '').strip() and official:
                # 名字为空时体检只能判 unknown（没东西可比），但基金域已经自证到品种名 ——
                # 补名字是纯加性修复，而这批行背后挂着 56 条预测（006105 就占 33 条）：
                # 不补的话前端列表与统计页永远显示一只"无名基金"。
                if args.apply:
                    r.fund_name = official
                    filled += 1
                else:
                    print('   [可补] %s → %s' % (r.fund_code, official))
                continue
            if verdict == 'ok':
                continue
            report.append({'fund_code': r.fund_code, 'stored_name': r.fund_name,
                           'official_name': official, 'verdict': verdict,
                           'reason': (v.get('reason') or '')[:120]})
            if verdict == 'not_a_fund' and official and args.rename_to_official:
                # 改名是加性的（净值与预测都不动），但只在**没有活预测引用**时才做：
                # 名字一改，博主那些"我说的就是这只股票"的历史线索就找不回来了。
                from src.models.database import Prediction
                live = db.query(Prediction).filter(
                    Prediction.fund_code == r.fund_code,
                    Prediction.is_deleted.is_(False)).count()
                if live:
                    print('   [跳过] %s 仍被 %d 条未删除预测引用，先不动'
                          % (r.fund_code, live))
                elif args.apply:
                    r.fund_name = official
                    renamed += 1
                else:
                    print('   [可改名] %s %s → %s'
                          % (r.fund_code, r.fund_name, official))
        if args.apply:
            db.commit()
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, 'fund-info-identity-%s.csv'
                            % datetime.now().strftime('%Y%m%d-%H%M%S'))
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['fund_code', 'stored_name', 'official_name',
                                              'verdict', 'reason'])
            w.writeheader()
            w.writerows(report)
        stocks = [r for r in report if r['verdict'] == 'not_a_fund']
        print('[结果] 通不过体检 %d 行（其中股票名挂基金码 %d 行）；清单 %s'
              % (len(report), len(stocks), path))
        print('[结果] %s空 fund_name 补上 %d 行'
              % ('已' if args.apply else '可补（未写库）：', filled))
        if stocks:
            print('[提示] 股票名那批**没有自动删**：删行会牵动 fund_history 与历史预测口径，'
                  '要老板确认后再处理（可对照 CSV 逐行看）')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
