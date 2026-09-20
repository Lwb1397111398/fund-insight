"""Create prediction audit records inside the caller's transaction."""

from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.models.database import Prediction, PredictionChangeLog


SNAPSHOT_FIELDS = (
    "fund_code",
    "fund_name",
    "sector",
    "sector_type",
    "prediction_type",
    "prediction_content",
    "confidence",
    "prediction_date",
    "prediction_period",
    "target_date",
    "status",
    "start_nav",
    "start_nav_date",
    "current_nav",
    "current_nav_date",
    "end_nav",
    "end_nav_date",
    "actual_change",
    "is_correct",
    "verify_score",
    "ai_judgment",
    "verified_at",
    "verify_count",
    "last_verify_date",
    "next_verify_date",
    "is_expired",
    "has_active_prediction",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
    "restore_before",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def snapshot_prediction(prediction: Prediction) -> Dict[str, Any]:
    return {
        field: _json_value(getattr(prediction, field))
        for field in SNAPSHOT_FIELDS
    }


def add_prediction_change_log(
    db: Session,
    prediction: Prediction,
    *,
    action: str,
    source: str,
    before_state: Dict[str, Any],
    run_id: Optional[str] = None,
) -> Optional[PredictionChangeLog]:
    """记录一条预测变更。字段无变化时返回 None（不写空日志）。

    `run_id` 用于批量改动的整批回滚：表上没有 batch 列，只能靠它把一次跑批的
    所有行串起来（见 scripts/restore_prediction_batch.py）。
    """
    after_state = snapshot_prediction(prediction)
    changed_fields = [
        field for field in SNAPSHOT_FIELDS
        if before_state.get(field) != after_state.get(field)
    ]
    if not changed_fields:
        return None

    log = PredictionChangeLog(
        prediction_id=prediction.id,
        action=action,
        source=source,
        changed_fields=changed_fields,
        before_state=before_state,
        after_state=after_state,
        run_id=run_id,
    )
    db.add(log)
    return log
