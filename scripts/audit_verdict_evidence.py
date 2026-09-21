# -*- coding: utf-8 -*-
"""只读体检：已判结论的**端点证据**今天还成不成立（净值是否在"当前标的"的序列里）。

为什么需要它（第 16 轮，2026-09-22）：历轮自洽检查都拿"标量 vs 自家台账"比，
两者是同一次验证一起写进去的，所以永远互相吻合 —— 却可能整体挂着**另一个标的**的结论。
触发点是漂移闸门真断网后唯一那条未解释差异（id=2602：存的端点 2.5748@08-10，
而 159857 在 08-06 只有 0.7222）。

**成因归责曾被我自己写错过一次（第 17 轮两份复评共同指出）**：首版把
"这条预测历史上出现过 fund_code 变更日志"当成"结论挂在旧标的上"的依据，
实测 97 条里 95 条的末次验证**发生在改标之后**、用的就是当前标的
⇒ 真因是"同一标的下净值被就地改写或删行"。所以现在改成**正向核对**：
拿这条行走过的每一个代码去查那一天的净值，只有真的能对上某个旧标的，才敢归到 `old_fund_verdict`。

四个桶：
| 成因 | 含义 |
| --- | --- |
| `old_fund_verdict` | 端点净值只对得上**曾经**的标的 ⇒ 改标后结论没重算（真·挂错） |
| `nav_rewritten` | 同一天有行、数值不同 ⇒ 净值被就地改写/覆盖（`fund_api.update_fund_history`） |
| `nav_row_missing` | 当前标的那一天根本没有行 ⇒ 镜像缺行或基金停更 |
| `unlogged_retag`（附加旗标） | 当前 `fund_code` 与最后一条日志里的不符 ⇒ 改标没写日志，归责看不见 |

用法：
    python scripts/audit_verdict_evidence.py                  # 分桶计数 + 每桶样例
    python scripts/audit_verdict_evidence.py --json out.json
    python scripts/audit_verdict_evidence.py --max-stale 0    # 退码 3 的阈值（默认 0）
本脚本**一行都不写**。三种处置（撤掉重验 / 只标"证据已失效" / 保留）要老板点头。
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

# 与 resync/replay 两个脚本同一条规矩：import 本模块不许碰数据库，
# 钉库动作留在 main()，这样 `scripts.replay_verifications_on_copy` 才能安全 import 下面的纯函数。
FLOAT_TOL = 1e-6


def _same(a, b):
    """浮点末位差不算对不上（导出 JSON 只有 15 位有效数字）。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= FLOAT_TOL * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def verdict_evidence_is_stale(prediction, end_row):
    """纯判据：端点净值与"当前标的、那一天"的净值行对不对得上。

    漂移闸门也用这个函数决定"这条行能不能拿来比对判据"（第 17 轮：
    证据已失效的行如果继续参与比对，会把真回归混在噪声里）。
    """
    if prediction.end_nav is None or prediction.end_nav_date is None:
        return False
    return end_row is None or not _same(end_row.nav, prediction.end_nav)


def classify_stale_verdict(prediction, verified_code, verified_nav, end_row, matched_old_code):
    """给一条证据已失效的结论归成因。

    归责要**两个条件同时成立**才敢说"这条结论挂错了标的"（第 17 轮两份复评各自
    指出我第一版的归责不成立：只看"有没有改过标的的日志"会把"改标后又重验过"的行
    也算进去；只看"数值恰好对得上某个旧代码"则可能纯巧合）：
      ① 写下这条结论时行上挂的是**另一个**代码（最后一条 `verified` 日志的后像）；
      ② 那个代码在端点那一天的净值，正好等于结论里存的端点值。
    只满足①、或只满足②，都归到 `unattributed` 交给人看，不猜。
    """
    code_moved = bool(verified_code and verified_code != prediction.fund_code)
    nav_follows_old_code = verified_nav is not None and _same(verified_nav, prediction.end_nav)
    if code_moved and nav_follows_old_code:
        return 'old_fund_verdict'
    if code_moved or matched_old_code:
        return 'unattributed'
    if end_row is None:
        return 'nav_row_missing'
    return 'nav_rewritten'


def codes_this_row_used(logs, current_code):
    """这条预测**曾经**挂过的所有 fund_code（来自 change log 的前后像）。"""
    codes = set()
    for before, after in logs:
        for state in (before, after):
            code = (state or {}).get('fund_code')
            if code:
                codes.add(code)
    codes.add(current_code)
    return codes


def audit(db):
    """扫全库已判结论，返回 (总数, 分桶计数, 每桶样例 id, 逐行明细)。"""
    from src.models.database import FundHistory, Prediction, PredictionChangeLog

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
        Prediction.end_nav != None,
        Prediction.end_nav_date != None,
    ).all()

    logs_by_pid = {}
    verified_code_by_pid = {}
    # 一次取全量日志（按 id 升序＝写入顺序），既给"走过哪些代码"用，也给
    # "最后一条 verified 日志当时挂的是哪个代码"用 —— 归责以后者为准。
    for pid, action, before, after in db.query(
            PredictionChangeLog.prediction_id, PredictionChangeLog.action,
            PredictionChangeLog.before_state, PredictionChangeLog.after_state).order_by(
            PredictionChangeLog.prediction_id, PredictionChangeLog.id).all():
        logs_by_pid.setdefault(pid, []).append((before, after))
        if action == 'verified':
            verified_code_by_pid[pid] = (after or {}).get('fund_code')

    buckets, samples, detail = Counter(), {}, []
    for p in rows:
        end_row = db.query(FundHistory).filter(
            FundHistory.fund_code == p.fund_code,
            FundHistory.nav_date == p.end_nav_date).first()
        if not verdict_evidence_is_stale(p, end_row):
            continue
        logs = logs_by_pid.get(p.id) or []
        history_codes = codes_this_row_used(logs, p.fund_code)
        # 佐证：这条行走过的哪个代码，那一天的净值正好等于存着的端点净值
        matched_old_code = None
        for code in history_codes:
            if code == p.fund_code:
                continue
            row = db.query(FundHistory).filter(
                FundHistory.fund_code == code,
                FundHistory.nav_date == p.end_nav_date).first()
            if row is not None and _same(row.nav, p.end_nav):
                matched_old_code = code
                break
        verified_code = verified_code_by_pid.get(p.id)
        verified_nav = None
        if verified_code and verified_code != p.fund_code:
            vrow = db.query(FundHistory).filter(
                FundHistory.fund_code == verified_code,
                FundHistory.nav_date == p.end_nav_date).first()
            verified_nav = None if vrow is None else vrow.nav
        last_logged_code = (logs[-1][1] or {}).get('fund_code') if logs else None
        kind = classify_stale_verdict(p, verified_code, verified_nav, end_row,
                                      matched_old_code)
        unlogged_retag = bool(last_logged_code and last_logged_code != p.fund_code)
        buckets[kind] += 1
        if unlogged_retag:
            buckets['(附加)unlogged_retag'] += 1
        samples.setdefault(kind, [])
        if len(samples[kind]) < 8:
            samples[kind].append(p.id)
        detail.append({'id': p.id, 'fund_code': p.fund_code, 'kind': kind,
                       'verified_under_code': verified_code,
                       'verified_code_nav_on_that_day': verified_nav,
                       'matched_old_code': matched_old_code,
                       'unlogged_retag': unlogged_retag,
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
    ap.add_argument('--max-stale', type=int, default=0,
                    help='允许多少条"证据已失效"的结论；超过就退码 3（默认 0）')
    args = ap.parse_args()

    pin_local_sqlite()
    from datetime import date
    from src.models.database import SessionLocal

    db = SessionLocal()
    try:
        total, buckets, samples, detail = audit(db)
        bad = sum(v for k, v in buckets.items() if not k.startswith('('))
        print('[体检] 已判结论 %d 条；端点证据与**当前标的**对不上 %d 条（%.1f%%）'
              % (total, bad, 100.0 * bad / max(1, total)))
        for kind, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
            print('   %-20s %4d  例如 %s' % (kind, n, samples.get(kind, [])))
        print('\n本脚本不写库。三种处置（撤掉重验 / 报表标"证据已失效" / 保留）'
              '都需要老板点头，见 docs/迭代计划/2026-09-21-S4a-v7.4-代码评审修订.md。')
        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as handle:
                json.dump({'as_of': date.today().isoformat(), 'judged': total,
                           'stale_evidence': bad, 'counts': dict(buckets),
                           'rows': detail}, handle, ensure_ascii=False, indent=1)
            print('[ok] 逐行明细：%s' % args.json)
        if bad > args.max_stale:
            print('[gate] 失效证据 %d 条 > 阈值 %d ⇒ 退码 3（这条流水线不该在被忽略的状态下合入）'
                  % (bad, args.max_stale))
            return 3
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
