# -*- coding: utf-8 -*-
"""只读体检：已判结论的**端点证据**今天还复现得出来吗（净值是否在"当前标的"的序列里）。

为什么需要它（第 16 轮，2026-09-22）：历轮自洽检查都拿"标量 vs 自家台账"比，
两者是同一次验证一起写进去的，所以永远互相吻合 —— 却可能整体挂着**另一个标的**的结论。
触发点是漂移闸门真断网后唯一那条未解释差异（id=2602：存的端点 2.5748@08-10，
而 159857 在 08-06 只有 0.7222）。实测全库 1163 条已判结论里 250 条（21.5%）复现不出来。

**归责口径在第 17 轮被两份独立复评共同纠正过一次**：首版按"这条预测历史上改过
fund_code"归责出"挂错标的 97 条"，其中 95 条的末次验证其实发生在改标之后、用的就是
当前标的。现在的判据是两条同时成立才算（见 `src/services/verdict_evidence.py`）：
① 写下结论时行上挂的是另一个代码（最后一条 `verified` 日志的后像）；
② 那个代码在端点那一天的净值正好等于存的端点值。

判据本身**不在本文件里**，住在 `src/services/verdict_evidence.py`，与前端
"证据已失效"标记、每日跑批报告共用同一份 —— 写第二份迟早与第一份分叉，
这个仓库已经为"两份清单"付过四次账。

用法：
    python scripts/audit_verdict_evidence.py                 # 分桶计数 + 每桶样例
    python scripts/audit_verdict_evidence.py --json out.json
    python scripts/audit_verdict_evidence.py --max-stale 0   # 退码 3 的阈值（默认 0）
本脚本**一行都不写**。三种处置（撤掉重验 / 只标"证据已失效" / 删除）里
删除会永久失去"当初为什么这么判"的审计链，已排除；老板同意"标注 + 能复现的重验"。
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


def audit(db):
    """扫全库已判结论，返回 (条数, 分桶计数, 每桶样例 id, 逐行明细)。"""
    from src.models.database import Prediction
    from src.services.verdict_evidence import (
        EVIDENCE_LABELS, evidence_statuses, has_verdict)

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
        Prediction.end_nav != None,
        Prediction.end_nav_date != None,
    ).all()
    statuses = evidence_statuses(db, rows)

    buckets, samples, detail = Counter(), {}, []
    for p in rows:
        kind = statuses.get(p.id)
        if not kind:
            continue
        buckets[kind] += 1
        samples.setdefault(kind, [])
        if len(samples[kind]) < 8:
            samples[kind].append(p.id)
        detail.append({'id': p.id, 'fund_code': p.fund_code, 'kind': kind,
                       'meaning': EVIDENCE_LABELS.get(kind),
                       'target_date': str(p.target_date),
                       'end_nav_date': str(p.end_nav_date),
                       'stored_end_nav': p.end_nav,
                       'is_correct': p.is_correct, 'verify_score': p.verify_score,
                       'last_verify_date': str(p.last_verify_date)})
    return sum(1 for p in rows if has_verdict(p)), buckets, samples, detail


def accuracy_span(db, stale_ids):
    """把"证据已失效"折算成准确率区间（第 18 轮 MAJOR-3：报数不能靠手算）。

    区间的两端是两个极端假设：**这批结论全部判错** vs **全部判对**。
    真值落在里面，但今天没有人能定到小数点 —— 谁引用准确率，谁就得连区间一起说。
    返回 (已判条数, 现在算出的判对数, 现在百分比, 下界, 上界, 其中失效条数)。

    口径与 `audit()` 同一份（`has_verdict`），不另起一套（第 19 轮 MINOR-7：
    两把尺子今天都为 1110，但"今天恰好相等"不等于可以各写一份）。
    """
    from src.models.database import Prediction
    from src.services.verdict_evidence import has_verdict

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
    ).all()
    rows = [r for r in rows if has_verdict(r)]
    judged = len(rows)
    correct = sum(1 for r in rows if r.is_correct)
    stale = [r for r in rows if r.id in stale_ids]
    stale_correct = sum(1 for r in stale if r.is_correct)
    low = 100.0 * (correct - stale_correct) / max(1, judged)
    high = 100.0 * (correct - stale_correct + len(stale)) / max(1, judged)
    return judged, correct, 100.0 * correct / max(1, judged), low, high, len(stale)


def unmapped_codes(db):
    """已判结论挂着的代码里，**映射表根本没提过**的那些：身份体检对它们没有意见。

    `fund_code_is_servable` 刻意保守（没有映射行就放行），所以"自带代码也过体检"这条
    门只对"映射表提到过的代码"生效（第 19 轮 MAJOR-2）。这里把盲区大小报出来，
    而不是悄悄把它当成 0：这些行今天既不会被改标、也不会被打 ⚠。
    """
    from src.models.database import Prediction, SectorFundMapping

    known = {r[0] for r in db.query(SectorFundMapping.fund_code).distinct().all()}
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
        Prediction.fund_code.isnot(None),
    ).all()
    blind = [r for r in rows if (r.fund_code or '').strip() not in known]
    return len(rows), len(blind), sorted({(r.fund_code or '').strip() for r in blind})[:10]



def main():
    ap = argparse.ArgumentParser(description='已判结论的端点证据体检（只读，不写库）')
    ap.add_argument('--json', help='把逐行明细写成 JSON')
    ap.add_argument('--max-stale', type=int, default=0,
                    help='允许多少条"证据已失效"的结论；超过就退码 3（默认 0）')
    args = ap.parse_args()

    pin_local_sqlite()                       # 必须在任何 ORM import 之前
    from datetime import date
    from src.models.database import SessionLocal

    db = SessionLocal()
    try:
        judged, buckets, samples, detail = audit(db)
        bad = sum(buckets.values())
        print('[体检] 已判结论 %d 条；端点证据与**当前标的**对不上 %d 条（%.1f%%）'
              % (judged, bad, 100.0 * bad / max(1, judged)))
        for kind, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
            print('   %-24s %4d  例如 %s' % (kind, n, samples[kind]))
        judged_n, correct_n, now_pct, low, high, stale_n = accuracy_span(
            db, {d['id'] for d in detail})
        print('\n[准确率只能当区间报] 已判 %d 条、现在落库判对 %d 条 = %.2f%%；'
              '其中 %d 条端点证据今天复现不出来 ⇒ 把这批按"全判错/全判对"两个极端算，'
              '区间 %.2f%% ~ %.2f%%（宽度 %.2f 个百分点）。'
              % (judged_n, correct_n, now_pct, stale_n, low, high, high - low))
        print(' 引用准确率时必须连这个区间一起引用：单报一个小数点就是在假装精度。')
        judged_all, blind_n, blind_codes = unmapped_codes(db)
        print('[体检覆盖面] 已判结论 %d 条里 %d 条挂的代码在 sector_fund_mapping 里'
              '**根本没有行** ⇒ 身份体检对它们没有意见，既不会被判不可服务、'
              '也不会触发改标（门是刻意保守的：没有映射行不等于这只基金有问题）。'
              '例：%s' % (judged_all, blind_n, blind_codes))
        print('\n本脚本不写库。处置已定：不删除（删了就永久失去审计链），'
              '改为读取时派生"证据已失效"标记 + 能复现的定向重验；'
              '每日跑批会报新增条数，见 docs/模块总览/预测验证与准确率统计.md。')
        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as handle:
                json.dump({'as_of': date.today().isoformat(), 'judged': judged,
                           'stale_evidence': bad, 'counts': dict(buckets),
                           'rows': detail}, handle, ensure_ascii=False, indent=1)
            print('[ok] 逐行明细：%s' % args.json)
        if bad > args.max_stale:
            print('[gate] 失效证据 %d 条 > 阈值 %d ⇒ 退码 3。'
                  '注意：只有**显式调用本脚本**的流程会被卡住；'
                  '每日跑批里的 verdict_evidence_audit 是只报告、不阻塞。'
                  % (bad, args.max_stale))
            return 3
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
