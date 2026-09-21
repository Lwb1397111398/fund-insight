# -*- coding: utf-8 -*-
"""只读体检：已判结论的**端点证据**今天还成不成立（净值是否在"当前标的"的序列里）。

为什么需要它（第 16 轮，2026-09-22）：历轮自洽检查都拿"标量 vs 自家台账"比，
两者是同一次验证一起写进去的，所以永远互相吻合 —— 却可能整体挂着**另一个标的**的结论。
触发点是漂移闸门真断网后唯一那条未解释差异（id=2602：存的端点 2.5748@08-10，
而 159857 在 08-06 只有 0.7222）。换成"拿当前 `fund_code` 的净值行核一遍"，
实测 1163 条已判结论里 250 条对不上，分三种成因：

| 成因 | 含义 |
| --- | --- |
| `fund_retagged` | 改标（S4b/rematch）之后结论没重算 ⇒ 旧标的的结论挂在新标的上 |
| `nav_revised` | 同一天净值后来被修正/覆盖过（补录、复权、改错码） |
| `nav_row_missing` | 当前标的在那一天根本没有净值行（镜像缺行或基金停更） |

本脚本**一行都不写**（处置方式要老板点头：撤掉重验 / 只标"证据已失效" / 保留），
它只负责把数量与清单摆出来。

用法：
    python scripts/audit_verdict_evidence.py                # 分桶计数 + 每桶样例
    python scripts/audit_verdict_evidence.py --json out.json
    python scripts/audit_verdict_evidence.py --limit 500    # 每桶多列几条 id
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from _db_guard import pin_local_sqlite  # noqa: E402

pin_local_sqlite()

from datetime import date  # noqa: E402

from src.models.database import (  # noqa: E402
    FundHistory, Prediction, PredictionChangeLog, SessionLocal)

FLOAT_TOL = 1e-6


def _same(a, b):
    """浮点末位差不算对不上（导出 JSON 只有 15 位有效数字）。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= FLOAT_TOL * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def classify_verdict(prediction, end_row, fund_code_changed_in_history):
    """给一条"端点证据已不成立"的结论归成因。返回 None 表示证据仍然对得上。"""
    if end_row is not None and _same(end_row.nav, prediction.end_nav):
        return None                          # 净值与日期都对得上 ⇒ 这条结论的输入还在
    if fund_code_changed_in_history:
        return 'fund_retagged'
    if end_row is None:
        return 'nav_row_missing'
    return 'nav_revised'


def audit(db, limit_per_bucket=8):
    """扫全库已判结论，返回 (总数, 分桶计数, 每桶 id 样例, 逐行明细)。"""
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
        Prediction.end_nav != None,
        Prediction.end_nav_date != None,
    ).all()

    # 每条预测是否曾被改过标的：改标类动作的日志里 before/after 的 fund_code 不同
    remapped = set()
    logs = db.query(PredictionChangeLog.prediction_id,
                    PredictionChangeLog.before_state,
                    PredictionChangeLog.after_state).all()
    for pid, before, after in logs:
        if (before or {}).get('fund_code') != (after or {}).get('fund_code'):
            remapped.add(pid)

    buckets, samples, detail = Counter(), {}, []
    for p in rows:
        end_row = db.query(FundHistory).filter(
            FundHistory.fund_code == p.fund_code,
            FundHistory.nav_date == p.end_nav_date).first()
        kind = classify_verdict(p, end_row, p.id in remapped)
        if kind is None:
            continue
        buckets[kind] += 1
        samples.setdefault(kind, [])
        if len(samples[kind]) < limit_per_bucket:
            samples[kind].append(p.id)
        detail.append({'id': p.id, 'fund_code': p.fund_code, 'kind': kind,
                       'target_date': str(p.target_date),
                       'end_nav_date': str(p.end_nav_date),
                       'stored_end_nav': p.end_nav,
                       'actual_end_nav': None if end_row is None else end_row.nav,
                       'is_correct': p.is_correct, 'verify_score': p.verify_score,
                       'last_verify_date': str(p.last_verify_date)})
    return len(rows), buckets, samples, detail


def main():
    ap = argparse.ArgumentParser(description='已判结论的端点证据体检（只读，不写库）')
    ap.add_argument('--json', help='把逐行明细写成 JSON')
    ap.add_argument('--limit', type=int, default=8, help='每个成因桶展示多少条 id')
    args = ap.parse_args()

    db = SessionLocal()
    try:
        total, buckets, samples, detail = audit(db, args.limit)
        bad = sum(buckets.values())
        print('[体检] 已判结论 %d 条；端点证据与**当前标的**对不上 %d 条（%.1f%%）'
              % (total, bad, 100.0 * bad / max(1, total)))
        for kind, n in buckets.most_common():
            print('   %-18s %4d  例如 %s' % (kind, n, samples[kind]))
        print('\n本脚本不写库。三种处置（撤掉重验 / 报表标"证据已失效" / 保留）'
              '都需要老板点头，见 docs/迭代计划/2026-09-21-S4a-v7.4-代码评审修订.md §三十一。')
        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as handle:
                json.dump({'as_of': date.today().isoformat(), 'judged': total,
                           'stale_evidence': bad, 'counts': dict(buckets),
                           'rows': detail}, handle, ensure_ascii=False, indent=1)
            print('[ok] 逐行明细：%s' % args.json)
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
