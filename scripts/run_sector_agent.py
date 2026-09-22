# -*- coding: utf-8 -*-
"""跑板块→基金 agent，把证据链落成 JSON/Markdown，便于人工复核。

用法：
    DATABASE_URL="sqlite:///<本地镜像库>" python scripts/run_sector_agent.py --sectors 半导体,债券,SpaceX
    python scripts/run_sector_agent.py --from-db --limit 20 --out docs/迭代计划/run-2026-09-20/agent-probe.json
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402  必须先把连接串钉到本地 SQLite


def collect_sectors(args):
    if args.sectors:
        return [s.strip() for s in args.sectors.split(',') if s.strip()]
    from src.models.database import SessionLocal, Prediction, SectorFundMapping
    db = SessionLocal()
    try:
        sectors = [row[0] for row in db.query(Prediction.sector).filter(
            Prediction.sector.isnot(None), Prediction.is_deleted == False  # noqa: E712
        ).group_by(Prediction.sector).order_by(
            # 按预测条数降序：先修影响面最大的板块
            __import__('sqlalchemy').func.count(Prediction.id).desc()
        ).limit(args.limit).all()]
        if args.only_pending:
            reviewed = {m.sector_name for m in db.query(SectorFundMapping).filter(
                SectorFundMapping.reviewed == True).all()}  # noqa: E712
            sectors = [s for s in sectors if s not in reviewed]
        return sectors
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sectors', default='')
    ap.add_argument('--from-db', action='store_true')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--only-pending', action='store_true')
    ap.add_argument('--no-llm', action='store_true', help='只走确定性路径，验证降级行为')
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'agent_probe.json'))
    args = ap.parse_args()

    _db_guard.pin_local_sqlite(use_mirror_default=True)
    from src.services.sector_fund_agent import resolve_sector_fund

    sectors = collect_sectors(args) or []
    results = []
    for sector in sectors:
        decision = resolve_sector_fund(sector, allow_llm=not args.no_llm)
        results.append(decision.to_dict())
        chosen = decision.chosen
        print('%-14s %-11s conf=%.2f kind=%-6s -> %s %s (%d ms, %d 轮)'
              % (sector, decision.status, decision.confidence,
                 (chosen.t3_proxy and 'proxy') or 'direct' if chosen else '-',
                 chosen.code if chosen else '-', chosen.display_name if chosen else '-',
                 decision.elapsed_ms, decision.rounds))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print('\n证据链已写入 %s' % args.out)


if __name__ == '__main__':
    main()
