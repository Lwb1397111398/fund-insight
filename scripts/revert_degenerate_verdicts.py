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


def _nav_row_date_for(db, fund_code, day, want_nav):
    """找"`<= day` 且净值正好等于 `want_nav`"的那一天；找不到返回 None。

    为什么不直接用"≤day 的最近一条"：那是**推测**它用了哪天，证明不了那天真有行；
    而老数据的 `end_nav_date` 存的是请求的目标日（周末），按日期比必然对不上。
    按"值 + 日期"双锚定还顺带挡住了改标行 —— 两端净值来自另一只基金时，
    在这只基金的净值序列里根本找不到，只能算"证不了"。（第 11 轮 MINOR-9）
    """
    from src.models.database import FundHistory
    if fund_code is None or day is None or want_nav is None:
        return None
    rows = db.query(FundHistory.nav_date, FundHistory.nav).filter(
        FundHistory.fund_code == fund_code,
        FundHistory.nav_date <= day).all()
    hits = [r[0] for r in rows if r[1] is not None and abs(float(r[1]) - float(want_nav)) <= EPS]
    return max(hits) if hits else None


def find_degenerate(db, service):
    """从**落库证据**里找退化结论，分成"能确证"与"看着像但证不了"两堆。"""
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,
        Prediction.is_correct.isnot(None),
        Prediction.start_nav.isnot(None),
        Prediction.end_nav.isnot(None)).all()
    damaged, unsure = [], []
    for p in rows:
        if abs(p.end_nav - p.start_nav) > EPS or abs(p.actual_change or 0) > EPS:
            continue
        start_day = p.start_nav_date or p.prediction_date
        end_day = p.end_nav_date or p.target_date
        start_real = _nav_row_date_for(db, p.fund_code, start_day, p.start_nav)
        end_real = _nav_row_date_for(db, p.fund_code, end_day, p.end_nav)
        if not (start_real is not None and end_real is not None and start_real == end_real):
            unsure.append(p)
            continue
        # 再加一条硬证据：起点那天之后、终点请求日之前**没有任何净值行**。
        # 少了这条，"07-09 与 07-10 净值恰好都是 1.3014"的真平盘会被误判成退化
        # —— 那是两个不同的行，只是数值相同。
        from src.models.database import FundHistory
        later = db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == p.fund_code,
            FundHistory.nav_date > start_real,
            FundHistory.nav_date <= end_day).first()
        (unsure if later is not None else damaged).append(p)
    return damaged, unsure


def find_look_ahead(db):
    """已验证结论里，终点净值日**晚于目标日**的那些（S5 交叉回归 BLOCKER-2）。

    它们是 S7-1"休市顺延"那版留下的：当时终点取了目标日之后第一个有净值的交易日，
    正好违反本仓库写死的"验证窗口固定截止到 target_date、禁止使用目标日之后的行情"。
    代码路径已在 S7-2 撤回，但**已经落库的结论一行都没撤**，仍在算博主准确率。
    注意 `end_nav_date` 在 S7-2 之前存的是"请求的目标日"而不是实际日，所以这一类
    只能靠 `end_nav_date > target_date` 这个存下来的值去抓（老数据里它等于目标日时
    看不出问题，凡是存成晚于目标日的，一定是当时真用了之后的净值）。
    """
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,
        Prediction.is_correct.isnot(None),
        Prediction.end_nav_date.isnot(None),
        Prediction.target_date.isnot(None)).all()
    return [p for p in rows
            if _as_date(p.end_nav_date) > _as_date(p.target_date)]


def _as_date(value):
    from datetime import datetime
    if isinstance(value, datetime):
        return value.date()
    return value


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
        look_ahead = find_look_ahead(db)
        print('\n[未来函数结论] 终点净值日晚于目标日（S7-1 遗留，违反防未来函数策略）：%d 条'
              % len(look_ahead))
        for p in look_ahead[:12]:
            print('   id=%-5s %s 目标%s 终点%s 涨跌幅=%.2f%% 判%s'
                  % (p.id, p.fund_code, p.target_date, p.end_nav_date,
                     p.actual_change or 0, '对' if p.is_correct else '错'))
        damaged_ids = {p.id for p in damaged}
        combined = [p for p in damaged] + [p for p in look_ahead if p.id not in damaged_ids]
        print('\n[该撤] 退化终点 %d 条 + 未来函数 %d 条 = %d 条（去重后）'
              % (len(damaged), len(look_ahead), len(combined)))
        print('[证据扫描] 涨跌幅恒为 0 的已验证结论：%d 条，其中起点终点确证同一条 %d 条、'
              '证不了（多为真平盘）%d 条' % (len(damaged) + len(unsure), len(damaged), len(unsure)))
        for p in combined:
            print('   该撤 id=%-5s %s %s→%s 起=%s@%s end=%s@%s 判为%s'
                  % (p.id, p.fund_code, p.prediction_date, p.target_date, p.start_nav,
                     p.start_nav_date, p.end_nav, p.end_nav_date,
                     '错' if p.is_correct is False else '对'))
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

        if not combined:
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
                       'judged_by': '退化终点（起点=终点同一条净值）或未来函数（终点晚于目标日）',
                       'rows': [{'id': p.id,
                                 'fields': {k: str(getattr(p, k)) for k in SNAPSHOT_FIELDS
                                            if getattr(p, k) is not None}} for p in combined]},
                      f, ensure_ascii=False, indent=1)
        print('\n[ok] 改前快照：%s（%d 行）' % (snap_path, len(damaged)))

        # 不复用 `rollback_invalid_verifications`：那个方法的语义是"今天的数据已不支撑
        # 这条结论"，而这一批的问题是"结论当初就是用错的/未来的数据判出来的"——
        # 实测 94 条里它只肯撤 1 条（其余今天仍可验，于是被算成"保留"）。
        from src.services.prediction_change_log_service import (
            add_prediction_change_log, snapshot_prediction)
        from src.services.prediction_verify_service import clear_verification_fields
        from src.utils.blogger_stats import recalculate_blogger_stats

        run_id = 'revert-bad-verdicts-%s' % datetime.now().strftime('%Y%m%d-%H%M%S')
        bloggers = set()
        for p in combined:
            before = snapshot_prediction(p)
            clear_verification_fields(p)
            add_prediction_change_log(db, p, action='verification_rollback',
                                      source='maintenance', before_state=before,
                                      run_id=run_id)
            bloggers.add(p.blogger_id)
        for blogger_id in bloggers:
            if blogger_id:
                recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()
        print('[applied] 已撤 %d 条，回到待验证队列（run_id=%s；整批还原：'
              'python scripts/restore_prediction_batch.py --run-id %s）'
              % (len(combined), run_id, run_id))
        db.expire_all()          # 一次就够：循环里 expire 会把整表反复作废（MINOR-10）
        for p in combined[:6]:
            row = db.query(Prediction).filter(Prediction.id == p.id).first()
            print('   复核 id=%s：status=%s is_correct=%s end_nav_date=%s change=%s'
                  % (row.id, row.status, row.is_correct, row.end_nav_date, row.actual_change))
        yes = db.query(Prediction).filter(Prediction.is_deleted == False,
                                          Prediction.is_correct == True).count()
        no = db.query(Prediction).filter(Prediction.is_deleted == False,
                                         Prediction.is_correct == False).count()
        print('[核对] 撤后全库 is_correct=True %d、False %d；这些行会在下一轮按正确窗口重判'
              % (yes, no))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
