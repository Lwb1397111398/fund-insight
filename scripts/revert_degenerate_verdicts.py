# -*- coding: utf-8 -*-
"""找并回滚**退化终点**的验证结论：起点与终点是同一条净值 ⇒ 涨跌幅恒为 0，那不是结论。

判据只看**落库证据**，不看今天的门（第 10 轮评审 M-2 指出前一版两个漏判）：
1. `actual_change == 0` 且 `start_nav == end_nav`（同一条净值的两个字段）；
2. 两端的**实际净值日**（`_real_nav_date` 归一，兼容老数据里 `end_nav_date` 存的是
   "请求的目标日"这个假象）确实是同一天。
以前先从"今天的验证服务怎么判"里取候选，就漏掉了"窗口内一条净值都没有、
起终点都回退到窗口之前同一条"的行 —— 那种行今天的门给的是 `insufficient_points`，
根本进不了候选集，而它的结论同样是 0% 假判错。

为什么不直接用 `rollback_invalid_verifications()`：它默认把所有判不过门槛的已验证结论
一并回溯（实测影响面上千条），其中绝大多数是"当年用真实净值判出、本地镜像现在丢了那段历史"
的行（1208 +4.07%、1669 +1.55%），撤掉等于毁真数据；所以定向传 `only_ids`。

每条被撤的预测都会写 `prediction_change_logs`（含改前快照），
博主统计走 `recalculate_blogger_stats` 重算，不手改计数器。

用法：
    python scripts/revert_degenerate_verdicts.py                 # 只报告
    python scripts/revert_degenerate_verdicts.py --apply         # 真撤（先落改前快照 JSON）
    python scripts/revert_degenerate_verdicts.py --also-report-gate   # 顺带报告新门槛的全量影响面
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db_guard import pin_local_sqlite  # noqa: E402  必须先于任何 ORM 导入

pin_local_sqlite()

from src.models.database import Prediction  # noqa: E402
from src.services.prediction_verify_service import PredictionVerifyService  # noqa: E402

SNAPSHOT_FIELDS = ('fund_code', 'prediction_date', 'target_date', 'status', 'is_correct',
                   'verify_score', 'actual_change', 'start_nav', 'start_nav_date',
                   'end_nav', 'end_nav_date', 'verified_at', 'verify_count')
EPS = 1e-9


def find_degenerate(db, service):
    """从落库证据里找退化结论，分成"能确证"与"看着像但证不了"两堆。"""
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,
        Prediction.is_correct.isnot(None),
        Prediction.start_nav.isnot(None),
        Prediction.end_nav.isnot(None)).all()
    damaged, unsure = [], []
    for p in rows:
        flat_value = abs(p.end_nav - p.start_nav) <= EPS and abs(p.actual_change or 0) <= EPS
        if not flat_value:
            continue
        start_real = service._real_nav_date(p.fund_code, p.start_nav_date or p.prediction_date)
        end_real = service._real_nav_date(p.fund_code, p.end_nav_date or p.target_date)
        if start_real is not None and end_real is not None and start_real == end_real:
            damaged.append(p)
        else:
            # 真·平盘（比如 000801 那天净值就是没动）或本地缺数据无法归一 —— 一律不动
            unsure.append(p)
    return damaged, unsure


def main():
    ap = argparse.ArgumentParser(description='定向回滚退化终点的验证结论')
    ap.add_argument('--apply', action='store_true', help='真正写库（默认只报告）')
    ap.add_argument('--also-report-gate', action='store_true',
                    help='另外报告：新门槛会把多少条历史结论判为"现在不可验"（只读）')
    args = ap.parse_args()

    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        service = PredictionVerifyService(db)
        damaged, unsure = find_degenerate(db, service)
        print('[证据扫描] 涨跌幅恒为 0 的已验证结论：%d 条，其中起点终点确证同一条 %d 条、'
              '证不了（多为真平盘）%d 条' % (len(damaged) + len(unsure), len(damaged), len(unsure)))
        for p in damaged:
            print('   该撤 id=%-5s %s %s→%s 起=%s@%s end=@%s 判为%s'
                  % (p.id, p.prediction_date, p.target_date, p.start_nav, p.start_nav_date,
                     p.end_nav_date, '错' if p.is_correct is False else '对'))
        for p in unsure[:10]:
            print('   不动 id=%-5s %s 涨跌幅 0 但两端日期证不了（start@%s end@%s）'
                  % (p.id, p.fund_code, p.start_nav_date, p.end_nav_date))

        if args.also_report_gate:
            audit = service.rollback_invalid_verifications(dry_run=True)
            by_reason = {}
            for d in audit['data'].get('rollback_details') or []:
                reason = (d.get('data_status') or {}).get('reason') or 'unknown'
                by_reason[reason] = by_reason.get(reason, 0) + 1
            print('\n[只读全量审计] 新门槛判"现在不可验"的历史结论 %d 条：%s（本脚本不撤这些）'
                  % (audit['data'].get('would_rollback'), by_reason))

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
                       'judged_by': 'actual_change==0 且起点终点为同一条净值',
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
