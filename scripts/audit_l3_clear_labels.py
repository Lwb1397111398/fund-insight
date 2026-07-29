#!/usr/bin/env python
"""
L3 附录 C 只读：
1) clear 桶方向标签审计（随机 ~50 条，否定/条件句敏感复读）
2) bucket × blogger tier 交叉表（命中率 + 方向收益）

不改生产代码；结果写入 docs/L3_VAGUE_LABEL_ESTIMATE.md 附录 C。
"""
from __future__ import annotations

import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# 与 estimate_l3_vague_labels 同一套桶启发式
CLEAR_BULL = re.compile(
    r"(看多|看涨|看升|上涨|上行|加仓|增持|布局多|做多|突破|新高|乐观|强势|反弹|翻倍|大涨|暴涨|买入|抄底)",
    re.I,
)
CLEAR_BEAR = re.compile(
    r"(看空|看跌|看淡|下跌|下行|减仓|清仓|做空|破位|悲观|走弱|大跌|暴跌|卖出|规避|回避|离场)",
    re.I,
)
VAGUE = re.compile(
    r"(关注|观望|等待|观察|可能|或许|也许|大概|不一定|不确定|谨慎|震荡|波动|分化|"
    r"结构性|择时|再看|看看|留意|跟踪|保持|中性|平衡|灵活|视情况|取决于|有待|"
    r"风险与机遇|机会与风险|多空|博弈|分歧)",
    re.I,
)

# 否定/劝退：方向词前 0–4 字内出现否定 → 方向翻转候选
NEG_PREFIX = re.compile(
    r"(不|别|勿|没|未|难|忌|慎|不要|并非|并非是|并不是|没法|无法|难以|不必|无需|千万别|不要再)"
)
# 条件/反转话术
CONDITIONAL = re.compile(
    r"(如果|若|除非|要是|只有|才(会|能)|否则|万一|就算|即使|哪怕)"
)

DIR_SPAN = re.compile(
    r"((?:不|别|勿|没|未|难|忌|慎|不要|并非|并不是|没法|无法|难以|不必|无需|千万别|不要再){0,2}"
    r".{0,4}?"
    r"(?:看多|看涨|看升|上涨|上行|加仓|增持|做多|突破|新高|乐观|强势|反弹|大涨|暴涨|买入|抄底|"
    r"看空|看跌|看淡|下跌|下行|减仓|清仓|做空|破位|悲观|走弱|大跌|暴跌|卖出|规避|回避|离场))",
    re.I,
)

BULL_TOKENS = (
    "看多", "看涨", "看升", "上涨", "上行", "加仓", "增持", "布局多", "做多",
    "突破", "新高", "乐观", "强势", "反弹", "翻倍", "大涨", "暴涨", "买入", "抄底",
)
BEAR_TOKENS = (
    "看空", "看跌", "看淡", "下跌", "下行", "减仓", "清仓", "做空", "破位",
    "悲观", "走弱", "大跌", "暴跌", "卖出", "规避", "回避", "离场",
)

SAMPLE_N = 50
SEED = 20260729
MIN_N_TIER = 10  # empirical vs prior


def classify_bucket(text: str, ptype: str) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    has_bull = bool(CLEAR_BULL.search(t))
    has_bear = bool(CLEAR_BEAR.search(t))
    p = (ptype or "").lower()
    if p in ("up", "bullish"):
        if has_bull and not has_bear:
            return "clear"
        if has_bear and not has_bull:
            return "weak"
        return "vague_hard"
    if p in ("down", "bearish"):
        if has_bear and not has_bull:
            return "clear"
        if has_bull and not has_bear:
            return "weak"
        return "vague_hard"
    return "other"


def _polarity_of_token(tok: str) -> str:
    low = tok.lower()
    for b in BULL_TOKENS:
        if b in low:
            return "bull"
    for b in BEAR_TOKENS:
        if b in low:
            return "bear"
    return "unk"


def _span_negated(span: str) -> bool:
    # 方向词前半段是否有否定
    for b in list(BULL_TOKENS) + list(BEAR_TOKENS):
        idx = span.lower().find(b.lower()) if b.lower() in span.lower() else span.find(b)
        if idx < 0:
            # try raw
            idx = span.find(b)
        if idx >= 0:
            head = span[:idx]
            if NEG_PREFIX.search(head):
                return True
    return bool(NEG_PREFIX.search(span[: max(1, len(span) // 2)]))


def audit_clear_label(content: str, ptype: str) -> Dict[str, Any]:
    """
    对「桶启发式 clear」样本做否定/条件敏感复读。
    返回 mismatch 及原因。
    """
    t = (content or "").strip()
    p = (ptype or "").lower()
    expected = "bull" if p in ("up", "bullish") else "bear" if p in ("down", "bearish") else "unk"

    spans = [m.group(1) for m in DIR_SPAN.finditer(t)]
    pos_bull = pos_bear = neg_bull = neg_bear = 0
    for sp in spans:
        pol = _polarity_of_token(sp)
        neg = _span_negated(sp)
        if pol == "bull":
            if neg:
                neg_bull += 1
            else:
                pos_bull += 1
        elif pol == "bear":
            if neg:
                neg_bear += 1
            else:
                pos_bear += 1

    # 有效极性：肯定 bull - 否定 bull 的反转贡献
    # 简化：净 bull 分 = pos_bull + neg_bear - neg_bull - pos_bear
    score = pos_bull + neg_bear - neg_bull - pos_bear
    has_cond = bool(CONDITIONAL.search(t))

    reasons = []
    mismatch = False

    if expected == "bull":
        if score < 0:
            mismatch = True
            reasons.append("net_polarity_bear_vs_up")
        if neg_bull > 0 and pos_bull == 0:
            mismatch = True
            reasons.append("only_negated_bull_words")
        if pos_bear > pos_bull:
            mismatch = True
            reasons.append("more_bear_than_bull_tokens")
    elif expected == "bear":
        if score > 0:
            mismatch = True
            reasons.append("net_polarity_bull_vs_down")
        if neg_bear > 0 and pos_bear == 0:
            mismatch = True
            reasons.append("only_negated_bear_words")
        if pos_bull > pos_bear:
            mismatch = True
            reasons.append("more_bull_than_bear_tokens")

    # 条件句 + 单一方向词：提高疑似（不算自动 mismatch，除非已有否定翻转）
    if has_cond and (neg_bull + neg_bear) > 0:
        reasons.append("conditional_with_negation")
        mismatch = True
    elif has_cond:
        reasons.append("conditional_context")

    return {
        "mismatch": mismatch,
        "reasons": reasons or ["ok"],
        "score": score,
        "pos_bull": pos_bull,
        "pos_bear": pos_bear,
        "neg_bull": neg_bull,
        "neg_bear": neg_bear,
        "has_cond": has_cond,
    }


def connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        eng = create_engine(url, pool_pre_ping=True)
        label = "DATABASE_URL"
    else:
        eng = create_engine(f"sqlite:///{ROOT / 'data' / 'fund_insight.db'}")
        label = "local sqlite"
    return eng, sessionmaker(bind=eng)(), label


def fetch(session) -> List[Dict[str, Any]]:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        sql = text(
            """
            SELECT p.id, p.blogger_id, p.prediction_type, p.prediction_content,
                   p.is_correct, p.start_nav, p.current_nav, b.grade, b.name AS blogger_name
            FROM predictions p
            JOIN bloggers b ON b.id = p.blogger_id
            WHERE COALESCE(p.is_deleted, false) = false
              AND lower(COALESCE(p.prediction_type, '')) IN ('up','down','bullish','bearish')
            """
        )
    else:
        sql = text(
            """
            SELECT p.id, p.blogger_id, p.prediction_type, p.prediction_content,
                   p.is_correct, p.start_nav, p.current_nav, b.grade, b.name AS blogger_name
            FROM predictions p
            JOIN bloggers b ON b.id = p.blogger_id
            WHERE COALESCE(p.is_deleted, 0) = 0
              AND lower(COALESCE(p.prediction_type, '')) IN ('up','down','bullish','bearish')
            """
        )
    return [dict(r) for r in session.execute(sql).mappings().all()]


def directional_return(row: Dict[str, Any]) -> Optional[float]:
    try:
        s = float(row["start_nav"]) if row.get("start_nav") is not None else None
        e = float(row["current_nav"]) if row.get("current_nav") is not None else None
    except (TypeError, ValueError):
        return None
    if s is None or e is None or s <= 0:
        return None
    raw = e / s - 1.0
    p = (row.get("prediction_type") or "").lower()
    if p in ("up", "bullish"):
        return raw
    if p in ("down", "bearish"):
        return -raw
    return None


def blogger_tiers(rows: List[Dict[str, Any]]) -> Dict[int, str]:
    """按存活已结论数分 tier：empirical n>=10 / prior 1..9 / neutral 0。"""
    stats: Dict[int, List[bool]] = defaultdict(list)
    for r in rows:
        if r.get("is_correct") is None:
            continue
        stats[int(r["blogger_id"])].append(bool(r["is_correct"]))
    out = {}
    for bid, corrects in stats.items():
        n = len(corrects)
        if n >= MIN_N_TIER:
            out[bid] = "empirical"
        elif n >= 1:
            out[bid] = "prior"
        else:
            out[bid] = "neutral"
    # 无结论博主
    for r in rows:
        bid = int(r["blogger_id"])
        out.setdefault(bid, "neutral")
    return out


def cross_table(
    rows: List[Dict[str, Any]], tiers: Dict[int, str]
) -> List[Dict[str, Any]]:
    cells: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in rows:
        if r.get("is_correct") is None:
            continue
        b = classify_bucket(r.get("prediction_content") or "", r.get("prediction_type") or "")
        if b not in ("clear", "vague_hard", "weak"):
            continue
        t = tiers.get(int(r["blogger_id"]), "neutral")
        cells[(b, t)].append(r)

    out = []
    for (b, t), items in sorted(cells.items()):
        n = len(items)
        c = sum(1 for x in items if bool(x.get("is_correct")))
        rets = [directional_return(x) for x in items]
        ok = [x for x in rets if x is not None]
        out.append(
            {
                "bucket": b,
                "tier": t,
                "n": n,
                "hit_rate": c / n if n else None,
                "avg_return": sum(ok) / len(ok) if ok else None,
                "return_n": len(ok),
            }
        )
    return out


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def main():
    eng, session, db_label = connect()
    try:
        rows = fetch(session)
    finally:
        session.close()
        eng.dispose()

    clear_all = [
        r
        for r in rows
        if classify_bucket(r.get("prediction_content") or "", r.get("prediction_type") or "")
        == "clear"
    ]
    rng = random.Random(SEED)
    sample = list(clear_all)
    rng.shuffle(sample)
    sample = sample[: min(SAMPLE_N, len(sample))]

    audits = []
    mismatch_n = 0
    reason_counter: Dict[str, int] = defaultdict(int)
    for r in sample:
        a = audit_clear_label(r.get("prediction_content") or "", r.get("prediction_type") or "")
        if a["mismatch"]:
            mismatch_n += 1
            for reason in a["reasons"]:
                if reason != "ok":
                    reason_counter[reason] += 1
        audits.append({**a, "id": r["id"], "type": r["prediction_type"], "content": (r.get("prediction_content") or "")[:100], "is_correct": r.get("is_correct")})

    rate = mismatch_n / len(sample) if sample else 0.0

    tiers = blogger_tiers(rows)
    cross = cross_table(rows, tiers)

    # 成分效应：clear 在 empirical vs prior 的命中差
    clear_emp = next((c for c in cross if c["bucket"] == "clear" and c["tier"] == "empirical"), None)
    clear_pri = next((c for c in cross if c["bucket"] == "clear" and c["tier"] == "prior"), None)
    composition_note = "n/a"
    if clear_emp and clear_pri and clear_emp["hit_rate"] is not None and clear_pri["hit_rate"] is not None:
        gap = (clear_emp["hit_rate"] - clear_pri["hit_rate"]) * 100
        composition_note = (
            f"clear×empirical hit={fmt_pct(clear_emp['hit_rate'])} (n={clear_emp['n']}) vs "
            f"clear×prior {fmt_pct(clear_pri['hit_rate'])} (n={clear_pri['n']}), gap={gap:.1f}pp"
        )

    # 预注册判读
    if rate >= 0.15:
        verdict = "label_bug"
        action = "方向标签 bug 立项：修抽取（否定/条件句），P0；同时审基金线选股输入"
    elif rate < 0.15:
        # 成分：若 clear 差主要来自 prior 博主，算成分；否则可作信号
        comp_explains = False
        if clear_emp and clear_pri and clear_emp["n"] >= 15 and clear_pri["n"] >= 10:
            if clear_emp["hit_rate"] is not None and clear_pri["hit_rate"] is not None:
                if clear_emp["hit_rate"] >= 0.58 and clear_pri["hit_rate"] <= 0.52:
                    comp_explains = True
        if comp_explains:
            verdict = "composition_effect"
            action = "成分效应：clear 差主要由低样本博主贡献；记录观察，不修抽取、不移交信号"
        else:
            verdict = "signal_candidate"
            action = (
                "错配率<15% 且交叉表未显示「仅 prior 拖累」→ 写信号备忘录移交基金线"
                "（清晰喊单类或需降权/反指候选）；shadow 期继续观察 weak 桶"
            )
    else:
        verdict = "observe"
        action = "记录观察"

    # 读旧报告，替换/追加附录 C
    doc_path = ROOT / "docs" / "L3_VAGUE_LABEL_ESTIMATE.md"
    old = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    # 去掉旧附录 C
    marker = "\n## 附录 C："
    if marker in old:
        old = old.split(marker)[0].rstrip() + "\n"

    mismatch_samples = [a for a in audits if a["mismatch"]][:8]
    ok_samples = [a for a in audits if not a["mismatch"]][:3]

    cross_lines = [
        "| bucket | tier | n | 命中率 | 方向收益 | 收益n |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for c in cross:
        cross_lines.append(
            f"| {c['bucket']} | {c['tier']} | {c['n']} | {fmt_pct(c['hit_rate'])} | "
            f"{fmt_pct(c['avg_return'])} | {c['return_n']} |"
        )

    reason_lines = ", ".join(f"{k}:{v}" for k, v in sorted(reason_counter.items(), key=lambda x: -x[1])) or "无"

    appendix = f"""
## 附录 C：clear 桶标签审计 + bucket×tier 交叉表（只读）

- 日期：2026-07-30
- 数据源：`{db_label}`
- **不改生产、不 push**
- 复读方法：对 clear 桶随机 {len(sample)} 条（seed={SEED}）做**否定/条件句敏感**规则复读（非金标准人工，可复现）
- 博主 tier：存活已结论 n≥{MIN_N_TIER} → empirical；1–9 → prior；0 → neutral

### C1 预注册判读表

| 结果 | 动作 |
| --- | --- |
| clear 抽样**错配率 ≥15%** | **方向标签 bug 立项**修抽取（否定/条件句），P0 |
| 错配率 **&lt;15%** 且交叉表显示 clear 差主要由 prior 拖累 | 成分效应，记录观察 |
| 错配率 **&lt;15%** 且成分未解释 clear 异常 | **信号备忘录**移交基金线（清晰喊单降权/反指候选） |

### C2 clear 抽样审计

| 项 | 值 |
| --- | ---: |
| clear 全量 | {len(clear_all)} |
| 抽样 | {len(sample)} |
| **错配数** | **{mismatch_n}** |
| **错配率** | **{rate*100:.1f}%** |
| 原因计数 | {reason_lines} |

#### 错配样例（截断）

"""
    for a in mismatch_samples:
        appendix += (
            f"- id={a['id']} type={a['type']} correct={a['is_correct']} "
            f"reasons={a['reasons']}: {a['content']!r}\n"
        )
    if not mismatch_samples:
        appendix += "- （无错配）\n"
    appendix += "\n#### 非错配样例\n\n"
    for a in ok_samples:
        appendix += f"- id={a['id']} type={a['type']}: {a['content']!r}\n"
    if not ok_samples:
        appendix += "- （无）\n"

    appendix += f"""
### C3 bucket × tier 交叉表（仅已结论）

{chr(10).join(cross_lines)}

- 成分读数：{composition_note}

### C4 裁决（按 C1，禁止改口）

- **verdict**：`{verdict}`
- **错配率**：{rate*100:.1f}%
- **action**：{action}

### C5 信号备忘录草稿（仅当 verdict=signal_candidate 时启用）

- 观察：附录 A 中 clear 命中 54.3%/收益为负，vague 60.5%/收益略正。
- 若标签审计未达 bug 线：启发式「清晰方向词」可能对应**滞后喊单/追高**话术，而非更好的可验证预测。
- 基金线候选：**对 clear 桶（或高激动措辞）预测降权**，weak/谨慎措辞观察清单（n 仍小）。
- 需 shadow 期更大样本与人工抽检后再上线权重。
"""
    doc_path.write_text(old.rstrip() + "\n" + appendix, encoding="utf-8")
    summary = f"appendix_C verdict={verdict} mismatch_rate={rate*100:.1f}% n={len(sample)} wrote={doc_path}"
    try:
        print(summary)
    except UnicodeEncodeError:
        print(summary.encode("gbk", errors="replace").decode("gbk"))


if __name__ == "__main__":
    main()
