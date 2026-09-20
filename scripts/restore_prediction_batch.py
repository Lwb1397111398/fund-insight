# -*- coding: utf-8 -*-
"""按 run_id 整批回滚预测改动（S4 的回滚能力交付物）。

为什么需要它：仓库里原先**没有任何代码读取 before_state**，所以"变更日志可回滚"
只是句空话。本脚本把 run_id 那次批量改动还原：
  - 同一预测取该 run 内**最早**一条 before_state 全量回写（多次改动也只退到最初）；
  - 重算受影响博主的统计（准确率是推导值，不重算就会双计/漏计）；
  - 刷新涉及基金的 active_predictions/can_delete；
  - 回滚本身也记一条 change log（action='rollback'），审计链不断。

用法：
    DATABASE_URL="sqlite:///<本地镜像库>" python scripts/restore_prediction_batch.py --run-id rematch-20260921
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402

RESTORE_FIELDS = (
    "fund_code", "fund_name", "sector", "sector_type", "prediction_type",
    "prediction_content", "confidence", "prediction_date", "prediction_period",
    "target_date", "status", "start_nav", "start_nav_date", "current_nav",
    "current_nav_date", "end_nav", "end_nav_date", "actual_change", "is_correct",
    "verify_score", "ai_judgment", "verified_at", "verify_count", "last_verify_date",
    "next_verify_date", "is_expired", "has_active_prediction",
)

# 快照把日期写成了 ISO 字符串，直接塞回 Date/DateTime 列 SQLite 会拒绝
# （"SQLite Date type only accepts Python date objects"），所以回写要按列类型还原。
_DATE_TYPES = ('date',)
_DATETIME_TYPES = ('datetime', 'timestamp')


def coerce_value(column, value):
    if value is None or column is None:
        return value
    from datetime import date, datetime
    type_name = str(column.type).lower()
    if isinstance(value, str):
        if type_name.startswith(_DATETIME_TYPES):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return value
        if type_name.startswith(_DATE_TYPES):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return value
    if isinstance(value, datetime) and type_name.startswith(_DATE_TYPES):
        return value.date()
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    _db_guard.pin_local_sqlite()
    from datetime import datetime

    from src.models.database import (Prediction, PredictionChangeLog, SessionLocal,
                                     snapshot_prediction)
    from src.services.prediction_change_log_service import add_prediction_change_log
    from src.utils.blogger_stats import recalculate_blogger_stats

    db = SessionLocal()
    try:
        logs = db.query(PredictionChangeLog).filter(
            PredictionChangeLog.run_id == args.run_id,
        ).order_by(PredictionChangeLog.prediction_id.asc(),
                   PredictionChangeLog.id.asc()).all()
        if not logs:
            print('[abort] run_id=%s 没有变更日志，什么都不做' % args.run_id)
            return 2

        first_by_prediction = {}
        for log in logs:
            first_by_prediction.setdefault(log.prediction_id, log)
        print('[info] run_id=%s 影响 %d 条预测（日志 %d 条）'
              % (args.run_id, len(first_by_prediction), len(logs)))
        if args.dry_run:
            for pid in list(first_by_prediction)[:20]:
                log = first_by_prediction[pid]
                print('  %s: fund_code %s → 回滚前 %s'
                      % (pid, (log.after_state or {}).get('fund_code'),
                         (log.before_state or {}).get('fund_code')))
            return 0

        restored, missing, bloggers, funds = 0, 0, set(), set()
        for pid, log in first_by_prediction.items():
            prediction = db.query(Prediction).filter(Prediction.id == pid).first()
            if not prediction:
                missing += 1
                continue
            before = dict(log.before_state or {})
            snapshot = snapshot_prediction(prediction)
            columns = Prediction.__table__.columns
            for field in RESTORE_FIELDS:
                if field in before:
                    setattr(prediction, field, coerce_value(columns.get(field), before[field]))
            add_prediction_change_log(
                db, prediction, action='rollback',
                source='restore:%s' % args.run_id[:40],
                before_state=snapshot)
            bloggers.add(prediction.blogger_id)
            funds.update([before.get('fund_code'), snapshot.get('fund_code')])
            restored += 1

        from src.models.database import FundInfo
        from sqlalchemy import func
        for blogger_id in filter(None, bloggers):
            recalculate_blogger_stats(db, blogger_id, commit=False)
        for fund_code in filter(None, funds):
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if not fund:
                continue
            count = db.query(func.count(Prediction.id)).filter(
                Prediction.fund_code == fund_code,
                Prediction.is_deleted == False,   # noqa: E712
            ).scalar() or 0
            fund.active_predictions = count
            fund.can_delete = count == 0
        db.commit()
        print('[ok] 回滚 %d 条预测（%d 条已不存在），重算博主 %d 个、基金 %d 只'
              % (restored, missing, len(bloggers), len(funds)))
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
