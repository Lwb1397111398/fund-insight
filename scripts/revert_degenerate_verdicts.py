# -*- coding: utf-8 -*-
"""回滚**退化终点**的验证结论：起点与终点是同一条净值 ⇒ 涨跌幅恒为 0，那不是结论。

为什么要单独一个脚本，而不是直接用 `rollback_invalid_verifications()`：
那个方法默认把所有判不过门槛的已验证结论一并回溯，实测影响面是上千条，其中绝大多数
是"**当年用真实净值判出、本地镜像现在丢了那段历史**"（1208 +4.07%、1669 +1.55% 就是这种），
撤掉它们等于毁掉真结论。所以这里只用它筛出的 `same_nav_endpoint` 子集，再加两条
硬判据确认"这条结论本身没有信息量"才撤：
1. `actual_change == 0` 且 `start_nav == end_nav`；
2. 存的 `end_nav_date` 反查到的**实际**净值日 == `start_nav_date`（即终点用的就是起点那条）。

每条被撤的预测都会写 `prediction_change_logs`（action=verification_rollback，含改前快照），
博主统计走 `recalculate_blogger_stats` 重算，不手改计数器。

用法：
    python scripts/revert_degenerate_verdicts.py                # 只报告（含新门槛总影响面）
    python scripts/revert_degenerate_verdicts.py --apply        # 真撤（先落改前快照 JSON）
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db_guard import pin_local_sqlite  # noqa: E402  必须先于任何 ORM 导入

pin_local_sqlite()

from datetime import datetime  # noqa: E402

from src.models.database import Prediction  # noqa: E402
from src.services.prediction_verify_service import PredictionVerifyService  # noqa: E402

SNAPSHOT_FIELDS = ('fund_code', 'prediction_date', 'target_date', 'status', 'is_correct',
                   'verify_score', 'actual_change', 'start_nav', 'start_nav_date',
                   'end_nav', 'end_nav_date', 'verified_at', 'verify_count')


def _is_informative(p) -> bool:
    """结论本身有没有信息量：涨跌幅非 0、或终点用的确实是另一天的净值。"""
    if p.start_nav is None or p.end_nav is None:
        return True                     # 取不到两端就不下判断，保守保留
    if abs(p.end_nav - p.start_nav) > 1e-9 or (p.actual_change or 0) != 0:
        return True
    return False


def _damage_set(db, service, details):
    """把 `same_nav_endpoint` 的候选分成"该撤"与"只是本地缺历史、不能撤"。"""
    damaged, kept = [], []
    for d in details:
        if (d.get('data_status') or {}).get('reason') != 'same_nav_endpoint':
            continue
        p = db.query(Prediction).filter(Prediction.id == d['prediction_id']).first()
        if p is None or _is_informative(p):
            kept.append((d['prediction_id'], p))
            continue
        real_end = service._real_nav_date(p.fund_code, p.end_nav_date or p.target_date)
        if real_end is None or real_end != p.start_nav_date:
            kept.append((d['prediction_id'], p))     # 终点用的不是起点那条，撤了没道理
            continue
        damaged.append(p)
    return damaged, kept


def main():
    ap = argparse.ArgumentParser(description='定向回滚退化终点的验证结论')
    ap.add_argument('--apply', action='store_true', help='真正写库（默认只报告）')
    args = ap.parse_args()

    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        service = PredictionVerifyService(db)
        audit = service.rollback_invalid_verifications(dry_run=True)
        details = audit['data'].get('rollback_details') or []
        by_reason = {}
        for d in details:
            reason = (d.get('data_status') or {}).get('reason') or 'unknown'
            by_reason[reason] = by_reason.get(reason, 0) + 1
        print('[新门槛影响面] 已验证 %d 条中，按今天的判据有 %d 条会判"现在不可验"：%s'
              % (audit['data'].get('total_checked'), audit['data'].get('would_rollback'),
                 by_reason))
        print('   （除退化终点外一律不动：它们当时确有净值可判）')

        damaged, kept = _damage_set(db, service, details)
        print('\n[退化终点] 候选 %d 条：该撤 %d 条、不能撤 %d 条'
              % (len(kept) + len(damaged), len(damaged), len(kept)))
        for p in damaged:
            print('   该撤 id=%-5s %s 起=%s@%s end=%s@%s change=%.4f correct=%s'
                  % (p.id, p.fund_code, p.start_nav, p.start_nav_date,
                     p.end_nav, p.end_nav_date, p.actual_change or 0, p.is_correct))
        for pid, p in kept:
            if p is None:
                continue
            print('   保留 id=%-5s %s 涨跌幅=%.4f（本地丢了那段历史，结论本身有依据）'
                  % (pid, p.fund_code, p.actual_change or 0))

        if not damaged:
            print('\n没有需要撤的结论。')
            return 0
        if not args.apply:
            print('\n未写库。确认后用 --apply 执行。')
            return 0

        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'docs', '迭代计划', 'run-' + datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(out_dir, exist_ok=True)
        snap_path = os.path.join(out_dir, 'degenerate-verdicts-before-%s.json'
                                 % datetime.now().strftime('%H%M%S'))
        with io.open(snap_path, 'w', encoding='utf-8') as f:
            json.dump({'created_at': datetime.now().isoformat(timespec='seconds'),
                       'reason': 'same_nav_endpoint',
                       'rows': [{'id': p.id,
                                 'fields': {k: str(getattr(p, k)) for k in SNAPSHOT_FIELDS
                                            if getattr(p, k) is not None}} for p in damaged]},
                      f, ensure_ascii=False, indent=1)
        print('\n[ok] 改前快照：%s（%d 行）' % (snap_path, len(damaged)))

        result = service.rollback_invalid_verifications(
            dry_run=False, only_ids=tuple(p.id for p in damaged))
        print('[applied] %s' % result['message'])
        for p in damaged:
            db.expire_all()
            row = db.query(Prediction).filter(Prediction.id == p.id).first()
            print('   复核 id=%s：status=%s is_correct=%s end_nav_date=%s change=%s'
                  % (row.id, row.status, row.is_correct, row.end_nav_date, row.actual_change))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
