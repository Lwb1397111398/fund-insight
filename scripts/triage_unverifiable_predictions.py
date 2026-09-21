# -*- coding: utf-8 -*-
"""S7-2 第 0 步：到期未验证预测的**只读**分诊（不写库、不打网络）。

为什么要有这个脚本：§二十 的结论（"80 条卡在数据不足"）是靠抽查猜的，
计划案 v1 因此把"顺延到下一个净值日"当成修法，评审用真数据推翻后才看清
分布完全不同。改判据之前必须先把**每一条**的成因摊开，否则下一次还是猜。

输出两张表：
1. 到期未验证队列逐行诊断（窗口、窗口内净值条数、本地最早/最新净值、
   起点净值日、终点净值日、终态分类）；
2. 分类合计 + 脚本日历口径与验证服务 reason 的不一致清单（不一致不代表错，
   但必须看得见，否则两套口径会悄悄分家）。

终态分类含义（`判据分类` 那一列取自**验证服务的 reason**，是唯一口径；
下面这套日历口径的 `local_class` 只写进 JSON 供对照，两者不一致会在末尾报出来）：
- `verifiable_now`     窗口内起点/终点是两条不同净值，立刻可验；
- `waiting_target_nav` 目标日就是今天或净值还没发布，等一次净值即可（会自行消化）；
- `degenerate_gap`     终点净值日 == 起点净值日（目标日休市且之前只有那一条），
                        验了也只会得到 0% 的假结论 —— 这才是 S7-2 要归因的那类；
- `missing_history`    目标日及之前**一条净值都没有**，真·缺历史；
- `no_fund_code`       预测没有基金代码，先补映射再谈验证。

用法：
    python scripts/triage_unverifiable_predictions.py            # 全量
    python scripts/triage_unverifiable_predictions.py --limit 40
    python scripts/triage_unverifiable_predictions.py --json out.json

已验证结论里有没有 0% 假结论，请用 `scripts/revert_degenerate_verdicts.py` 判 ——
它按"净值值 + 日期"双锚定，能覆盖老口径下 `end_nav_date` 存成请求日的行；
本脚本以前那份 `start_nav_date == end_nav_date` 的自检对老行天生漏判（第 11 轮 MINOR-8），
所以这里不再放第二套结论。
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db_guard import pin_local_sqlite  # noqa: E402  必须先于任何 ORM 导入

pin_local_sqlite()

from datetime import date, timedelta  # noqa: E402

from src.models.database import FundHistory, Prediction  # noqa: E402
from src.services.prediction_lifecycle import filter_due_for_verify  # noqa: E402


def _nav_dates(db, fund_code):
    return sorted({r[0] for r in db.query(FundHistory.nav_date).filter(
        FundHistory.fund_code == fund_code).all() if r[0] is not None})


def _le(dates, day):
    """严格 `<= day` 的最近一条 —— 与 get_nav_by_date 的 DB 路径同口径（禁止未来数据）。"""
    earlier = [d for d in dates if d <= day]
    return earlier[-1] if earlier else None


KNOWN_CLASSES = {
    # 验证服务的 reason
    'exact_target', 'weekend_previous', 'waited_previous', 'waiting_target_nav',
    'insufficient_points', 'no_history', 'no_source_history', 'same_nav_endpoint',
    'end_nav_too_old', 'endpoint_lag_unproven',
    # 脚本自己的日历口径（只在服务给不出 reason 时兜底出现）
    'verifiable_now', 'missing_history', 'degenerate_gap', 'no_fund_code',
}

# 日历口径 → 允许出现的 reason 集合。命名差不是矛盾，方向相反才是。
CONSISTENT_WITH = {
    # 'verifiable_now' 只数"窗口里点数够不够"，端点是否落在目标日它不看：
    # 第 16 轮 BLOCKER-2 新增的 `endpoint_lag_unproven`（点数够、但端点早于目标日且
    # 拿不到"那几天确实休市"的证据）正是这种"日历说够、判据说不能落死"的组合。
    'verifiable_now': {'exact_target', 'weekend_previous', 'waited_previous',
                       'waiting_target_nav', 'endpoint_lag_unproven'},
    'degenerate_gap': {'same_nav_endpoint', 'insufficient_points', 'no_source_history',
                       'no_history', 'end_nav_too_old'},
    'missing_history': {'insufficient_points', 'no_history', 'no_source_history'},
    'no_fund_code': {'insufficient_points', 'no_history'},
    'waiting_target_nav': {'waiting_target_nav', 'insufficient_points',
                           'weekend_previous', 'waited_previous', 'exact_target'},
}


def classify(row, start_day, end_day, in_window):
    if not row.fund_code:
        return 'no_fund_code'
    if end_day is None:
        return 'missing_history'
    if start_day is not None and start_day == end_day:
        return 'degenerate_gap'
    if row.target_date and row.target_date >= date.today() - timedelta(days=2) and not in_window:
        return 'waiting_target_nav'
    if in_window >= 2:
        return 'verifiable_now'
    return 'missing_history'


def main():
    ap = argparse.ArgumentParser(description='到期未验证预测只读分诊')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--json', help='把逐行诊断写成 JSON 便于复核')
    args = ap.parse_args()

    from src.models.database import SessionLocal
    from src.services.prediction_verify_service import PredictionVerifyService
    db = SessionLocal()
    try:
        svc = PredictionVerifyService(db)
        due = filter_due_for_verify(db)
        print('[队列] 到期未验证 %d 条' % len(due))
        calendar_cache = {}
        report = []
        counts = {}
        for row in (due[:args.limit] if args.limit else due):
            code = row.fund_code or ''
            if code not in calendar_cache:
                calendar_cache[code] = _nav_dates(db, code) if code else []
            dates = calendar_cache[code]
            start_day = _le(dates, row.prediction_date) if row.prediction_date else None
            end_day = _le(dates, row.target_date) if row.target_date else None
            in_window = len([d for d in dates
                             if row.prediction_date <= d <= row.target_date]) if (
                row.prediction_date and row.target_date) else 0
            kind = classify(row, start_day, end_day, in_window)
            # 分类口径以验证服务为准（避免脚本另立一套）；脚本自己算的那套只在
            # 服务无结论可用时兜底，两者优先级写死在这里。
            svc_reason = None
            if code and row.prediction_date and row.target_date:
                svc_reason = svc._check_fund_data_availability(
                    fund_code=code, nav_start_date=row.prediction_date,
                    window_end=row.target_date, target_date=row.target_date,
                    # 分诊要的是"结构性归因"，所以强制跳过等待期：同一条预测
                    # 在 Cron 里可能显示 `waiting_target_nav`（再等等就有），
                    # 在这里显示 `endpoint_lag_unproven`（拿不到证据就别判）。
                    # 两边标签不同是设计，不是矛盾。
                    skip_wait=True).get('reason')
            label = svc_reason or ('no_fund_code' if not code else kind)
            counts[label] = counts.get(label, 0) + 1
            report.append({'id': row.id, 'fund_code': code, 'fund_name': row.fund_name,
                           'prediction_date': str(row.prediction_date),
                           'target_date': str(row.target_date),
                           'period': row.prediction_period,
                           'points_in_window': in_window,
                           'local_first': str(dates[0]) if dates else None,
                           'local_last': str(dates[-1]) if dates else None,
                           'start_nav_date': str(start_day) if start_day else None,
                           'end_nav_date': str(end_day) if end_day else None,
                           'local_class': kind,
                           'class': label})

        print('\n%-6s %-8s %-10s %-10s %-5s %-10s %-10s %s'
              % ('id', 'code', '起日', '目标', '点数', '起点净值', '终点净值', '判据分类'))
        print('-' * 92)
        for r in report:
            print('%-6s %-8s %-10s %-10s %-5s %-10s %-10s %s'
                  % (r['id'], r['fund_code'] or '-', r['prediction_date'] or '-',
                     r['target_date'] or '-', r['points_in_window'],
                     r['start_nav_date'] or '无', r['end_nav_date'] or '无', r['class']))
        print('-' * 92)
        total = sum(counts.values())
        scope = len(report)
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print('  %-24s %d' % (label, n))
        # 自检只报**真能失败**的两件事（第 13 轮 MAJOR-3：上一版留着一条恒等式
        # —— 循环里 counts 与 report 各 +1 一次，永远相等，不是检查）：
        # ① 出现了没登记过的 reason（判据新增分类时必须同步图例）；
        # ② 日历口径与验证服务 reason 互相矛盾的行（命名差不算，见 CONSISTENT_WITH）。
        unknown = sorted(k for k in counts if k not in KNOWN_CLASSES)
        print('  分类计数 %d / 逐行 %d（仅信息，不做断言）' % (total, scope))
        if unknown:
            print('  !! 未登记的判据分类：%s（脚本图例需要同步）' % unknown)
        disagree = [r for r in report
                    if r['class'] not in CONSISTENT_WITH.get(r['local_class'], set())]
        print('  与日历口径矛盾的行：%d 条 %s'
              % (len(disagree), [(d['id'], d['local_class'], d['class']) for d in disagree[:6]]))

        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as f:
                json.dump({'as_of': str(date.today()), 'queue': len(due), 'counts': counts,
                           'rows': report,
                           'contradictions': [(d['id'], d['local_class'], d['class'])
                                              for d in disagree]},
                          f, ensure_ascii=False, indent=1)
            print('[ok] 明细：%s' % args.json)
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
