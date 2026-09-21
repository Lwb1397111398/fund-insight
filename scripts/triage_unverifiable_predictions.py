# -*- coding: utf-8 -*-
"""S7-2 第 0 步：到期未验证预测的**只读**分诊（不写库、不打网络）。

为什么要有这个脚本：§二十 的结论（"80 条卡在数据不足"）是靠抽查猜的，
计划案 v1 因此把"顺延到下一个净值日"当成修法，评审用真数据推翻后才看清
分布完全不同。改判据之前必须先把**每一条**的成因摊开，否则下一次还是猜。

输出三张表：
1. 到期未验证队列逐行诊断（窗口、窗口内净值条数、本地最早/最新净值、
   起点净值日、终点净值日、终态分类）；
2. 已验证但 `start_nav_date == end_nav_date` 的行 —— 起点终点是同一条净值，
   涨跌幅恒为 0，那是**退化结论**，不该计入准确率；
3. 分类合计，必须正好等于队列长度（对不上说明分诊漏了分支）。

终态分类含义：
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
            # 判据口径直接问验证服务（只读、skip_wait=True），免得脚本自己另立一套分类
            svc_reason = None
            if code and row.prediction_date and row.target_date:
                svc_reason = svc._check_fund_data_availability(
                    fund_code=code, nav_start_date=row.prediction_date,
                    window_end=row.target_date, target_date=row.target_date,
                    skip_wait=True).get('reason')
            kind = '%s/%s' % (kind, svc_reason) if svc_reason else kind
            counts[kind] = counts.get(kind, 0) + 1
            report.append({'id': row.id, 'fund_code': code, 'fund_name': row.fund_name,
                           'prediction_date': str(row.prediction_date),
                           'target_date': str(row.target_date),
                           'period': row.prediction_period,
                           'points_in_window': in_window,
                           'local_first': str(dates[0]) if dates else None,
                           'local_last': str(dates[-1]) if dates else None,
                           'start_nav_date': str(start_day) if start_day else None,
                           'end_nav_date': str(end_day) if end_day else None,
                           'judge_reason': svc_reason,
                           'class': kind})

        print('\n%-6s %-8s %-10s %-10s %-5s %-10s %-10s %s'
              % ('id', 'code', '起日', '目标', '点数', '起点净值', '终点净值', '分类/判据'))
        print('-' * 92)
        for r in report:
            print('%-6s %-8s %-10s %-10s %-5s %-10s %-10s %s'
                  % (r['id'], r['fund_code'] or '-', r['prediction_date'] or '-',
                     r['target_date'] or '-', r['points_in_window'],
                     r['start_nav_date'] or '无', r['end_nav_date'] or '无', r['class']))
        print('-' * 92)
        total = sum(counts.values())
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print('  %-20s %d' % (kind, n))
        print('  合计 %d / 队列 %d %s' % (total, len(due),
                                          'OK' if total == len(due) else '!! 对不上，分诊漏分支'))

        degenerate = db.query(Prediction).filter(
            Prediction.is_deleted == False,
            Prediction.is_correct.isnot(None),
            Prediction.start_nav_date.isnot(None),
            Prediction.end_nav_date.isnot(None),
            Prediction.start_nav_date == Prediction.end_nav_date).all()
        print('\n[退化结论] 已验证且 start_nav_date == end_nav_date：%d 条'
              % len(degenerate))
        for p in degenerate[:20]:
            print('   id=%s %s 起点=终点=%s actual_change=%s is_correct=%s score=%s'
                  % (p.id, p.fund_code, p.start_nav_date, p.actual_change,
                     p.is_correct, p.verify_score))

        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as f:
                json.dump({'as_of': str(date.today()), 'queue': len(due), 'counts': counts,
                           'rows': report,
                           'degenerate_verified': [p.id for p in degenerate]},
                          f, ensure_ascii=False, indent=1)
            print('[ok] 明细：%s' % args.json)
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
