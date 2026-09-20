# -*- coding: utf-8 -*-
"""临时脚本：本地镜像库数据清洗。

做三件事（一次性）：
1. 给"业绩和涨价线"及其预测引用的无映射板块建立有效 ETF 映射
   （先通过 verify_fund_fetchable 抓取验证，通不过就跳过该板块）。
2. 把挂在死基金 003033（净值停在 2020-12）上的待验证预测重挂到新映射，
   并补拉这些基金自 2026-06-01 以来的净值，让到期预测可以验证。
3. 输出处理结果摘要。
"""
import json
import os
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(ROOT, "data", "fund_insight.db").replace("\\", "/")
sys.path.insert(0, ROOT)

from src.core import config  # noqa: F401
from src.models.database import (  # noqa: E402
    Prediction, SectorFundMapping, FundInfo, SessionLocal, init_db,
)
from src.fund.fund_api import fund_api, fund_data_manager  # noqa: E402

# sector -> 候选基金代码（按优先级）
CANDIDATES = {
    "业绩和涨价线": ["515220"],   # 煤炭ETF国泰（涨价/业绩主线代理）
    "有色金属":     ["512400", "159881"],  # 预测已大量使用的南方有色
    "煤炭":         ["515220"],
    "化工":         ["159082", "158006"],
    "钢铁":         ["515210"],
    "红利":         ["510880", "515180"],
}

BACKFILL_START = date(2026, 6, 1)
BACKFILL_END = date(2026, 8, 6)


def pick_valid_fund(candidates):
    """按优先级挑第一个通过抓取验证的基金。"""
    for code in candidates:
        result = fund_api.verify_fund_fetchable(code)
        if result.get("ok"):
            name = result.get("api_name") or ""
            return code, name
        time.sleep(0.3)
    return None, None


def main():
    init_db()
    db = SessionLocal()
    summary = {"mappings_added": {}, "predictions_relinked": 0, "backfilled": {}, "skipped": []}
    try:
        sector_to_fund = {}
        for sector, candidates in CANDIDATES.items():
            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector
            ).first()
            if existing and existing.fund_code != "003033":
                sector_to_fund[sector] = (existing.fund_code, existing.fund_name)
                continue
            code, name = pick_valid_fund(candidates)
            if not code:
                summary["skipped"].append(sector)
                continue
            sector_to_fund[sector] = (code, name or "")

        # 先确保 FundInfo 存在（sector_fund_mapping.fund_code 有外键）
        for sector, (code, name) in sector_to_fund.items():
            if not db.query(FundInfo).filter(FundInfo.fund_code == code).first():
                db.add(FundInfo(fund_code=code, fund_name=name, sector_type=sector))
        db.flush()

        # 再建/改映射
        for sector, (code, name) in sector_to_fund.items():
            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector
            ).first()
            if existing:
                if existing.fund_code != code:
                    existing.fund_code = code
                    existing.fund_name = name
                    existing.reviewed = False
                    summary["mappings_added"][sector] = f"updated -> {code}"
            else:
                db.add(SectorFundMapping(
                    sector_name=sector, fund_code=code, fund_name=name,
                    reviewed=False,
                ))
                summary["mappings_added"][sector] = f"added -> {code}"
        db.flush()

        # 重挂 003033 上未验证预测的基金
        preds = db.query(Prediction).filter(
            Prediction.fund_code == "003033",
            Prediction.is_deleted == False,  # noqa: E712
            Prediction.is_correct.is_(None),
        ).all()
        for p in preds:
            target = sector_to_fund.get(p.sector)
            if not target:
                continue
            p.fund_code, p.fund_name = target[0], target[1] or p.fund_name
            summary["predictions_relinked"] += 1
        db.flush()

        # 补拉新基金的近期净值
        for code in {c for c, _ in sector_to_fund.values()}:
            n = fund_data_manager.backfill_history_range(code, BACKFILL_START, BACKFILL_END, db=db)
            summary["backfilled"][code] = n

        db.commit()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
