"""预测维护操作：默认只读预览，写入必须由路由显式确认。"""

from collections import defaultdict
from typing import Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import Blogger, FundInfo, Prediction, SectorFundMapping
from src.utils.blogger_stats import recalculate_blogger_stats


class PredictionMaintenanceService:
    def __init__(self, db: Session):
        self.db = db

    def scan_duplicate_groups(self) -> Dict:
        """查找同一博主的精确重复候选，不修改任何预测。"""
        predictions = self.db.query(Prediction).filter(
            Prediction.is_deleted == False,
            Prediction.fund_code.isnot(None),
            Prediction.fund_code != "",
            Prediction.target_date.isnot(None),
        ).order_by(Prediction.id.asc()).all()

        grouped: Dict[tuple, List[Prediction]] = defaultdict(list)
        for prediction in predictions:
            key = (
                prediction.blogger_id,
                prediction.fund_code,
                prediction.prediction_type,
                prediction.target_date,
            )
            grouped[key].append(prediction)

        groups = []
        for key, values in grouped.items():
            if len(values) < 2:
                continue
            groups.append({
                "blogger_id": key[0],
                "fund_code": key[1],
                "prediction_type": key[2],
                "target_date": key[3].isoformat(),
                "prediction_ids": [value.id for value in values],
                "count": len(values),
            })

        return {
            "dry_run": True,
            "duplicate_groups": len(groups),
            "candidate_predictions": sum(group["count"] for group in groups),
            "groups": groups,
        }

    def sync_sector_mappings(self, *, dry_run: bool = True) -> Dict:
        """使用已审核映射预览或同步预测基金关联。"""
        mappings = self.db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True,
            SectorFundMapping.reviewed == True,
        ).order_by(
            SectorFundMapping.updated_at.desc(),
            SectorFundMapping.id.desc(),
        ).all()
        sector_map = {}
        for mapping in mappings:
            sector_map.setdefault(mapping.sector_name, mapping)

        predictions = self.db.query(Prediction).filter(
            Prediction.is_deleted == False,
        ).order_by(Prediction.id.asc()).all()
        candidates = []
        unchanged = 0
        no_mapping = 0
        for prediction in predictions:
            sector = prediction.sector or prediction.sector_type
            mapping = sector_map.get(sector)
            if not mapping:
                no_mapping += 1
                continue
            if prediction.fund_code == mapping.fund_code:
                unchanged += 1
                continue
            was_verified = bool(
                (prediction.verify_count or 0) > 0
                or prediction.status in ("success", "failed", "verified")
                or prediction.is_expired
            )
            candidates.append({
                "prediction": prediction,
                "prediction_id": prediction.id,
                "sector": sector,
                "old_fund_code": prediction.fund_code,
                "old_fund_name": prediction.fund_name,
                "new_fund_code": mapping.fund_code,
                "new_fund_name": mapping.fund_name,
                "reset_verified": was_verified,
            })

        details = [
            {key: value for key, value in candidate.items() if key != "prediction"}
            for candidate in candidates
        ]
        result = {
            "dry_run": dry_run,
            "total_mappings": len(sector_map),
            "would_update": len(candidates),
            "predictions_updated": 0,
            "predictions_unchanged": unchanged,
            "predictions_no_mapping": no_mapping,
            "verified_reset": 0,
            "funds_added": 0,
            "funds_sector_updated": 0,
            "details": details,
        }
        if dry_run or not candidates:
            return result

        affected_bloggers = set()
        affected_funds = set()
        try:
            for candidate in candidates:
                prediction = candidate["prediction"]
                affected_bloggers.add(prediction.blogger_id)
                affected_funds.update(filter(None, [
                    candidate["old_fund_code"],
                    candidate["new_fund_code"],
                ]))
                prediction.fund_code = candidate["new_fund_code"]
                prediction.fund_name = candidate["new_fund_name"]
                if candidate["reset_verified"]:
                    self._reset_verification(prediction)
                    result["verified_reset"] += 1
                result["predictions_updated"] += 1

            self.db.flush()
            for blogger_id in affected_bloggers:
                recalculate_blogger_stats(self.db, blogger_id, commit=False)
            self._refresh_fund_counts(affected_funds)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _reset_verification(prediction: Prediction) -> None:
        prediction.status = "pending"
        prediction.is_expired = False
        prediction.has_active_prediction = True
        prediction.is_correct = None
        prediction.actual_change = None
        prediction.verify_count = 0
        prediction.verify_score = None
        prediction.ai_judgment = None
        prediction.verified_at = None
        prediction.last_verify_date = None
        prediction.start_nav = None
        prediction.start_nav_date = None
        prediction.current_nav = None
        prediction.current_nav_date = None
        prediction.end_nav = None
        prediction.end_nav_date = None

    def _refresh_fund_counts(self, fund_codes) -> None:
        for fund_code in fund_codes:
            fund = self.db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if not fund:
                continue
            count = self.db.query(func.count(Prediction.id)).filter(
                Prediction.fund_code == fund_code,
                Prediction.is_deleted == False,
            ).scalar() or 0
            fund.active_predictions = count
            fund.can_delete = count == 0
