#!/usr/bin/env python
"""
L3 只读：
1) up/down 模糊硬标占比
2) 已验证结论 vague/clear 分组命中率（+可选收益）裁决

判定与附录 B 预注册表见 docs/L3_VAGUE_LABEL_ESTIMATE.md
不改抽取/验证；不部署。
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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


def classify_text(text: str, ptype: str) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    has_bull = bool(CLEAR_BULL.search(t))
    has_bear = bool(CLEAR_BEAR.search(t))
    has_vague = bool(VAGUE.search(t))
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
    return "other_type"


def connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        eng = create_engine(url, pool_pre_ping=True)
        return eng, sessionmaker(bind=eng)(), "DATABASE_URL"
    eng = create_engine(f"sqlite:///{ROOT / 'data' / 'fund_insight.db'}")
    return eng, sessionmaker(bind=eng)(), "local sqlite"


def fetch_rows(session) -> List[Dict[str, Any]]:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        sql = text(
            """
            SELECT id, prediction_type, prediction_content, is_correct, is_deleted,
                   start_nav, current_nav
            FROM predictions
            WHERE COALESCE(is_deleted, false) = false
              AND lower(COALESCE(prediction_type, '')) IN ('up', 'down', 'bullish', 'bearish')
            """
        )
    else:
        sql = text(
            """
            SELECT id, prediction_type, prediction_content, is_correct, is_deleted,
                   start_nav, current_nav
            FROM predictions
            WHERE COALESCE(is_deleted, 0) = 0
              AND lower(COALESCE(prediction_type, '')) IN ('up', 'down', 'bullish', 'bearish')
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
    raw = (e / s) - 1.0
    p = (row.get("prediction_type") or "").lower()
    if p in ("up", "bullish"):
        return raw
    if p in ("down", "bearish"):
        return -raw
    return None


def bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """仅 is_correct 非空。"""
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        if r.get("is_correct") is None:
            continue
        label = classify_text(r.get("prediction_content") or "", r.get("prediction_type") or "")
        # 裁决主对比：clear vs vague_hard；weak/empty 单列
        buckets[label].append(r)

    out = {}
    for name, items in buckets.items():
        n = len(items)
        correct = sum(1 for x in items if bool(x.get("is_correct")))
        rets = [directional_return(x) for x in items]
        rets_ok = [x for x in rets if x is not None]
        out[name] = {
            "n": n,
            "correct": correct,
            "hit_rate": (correct / n) if n else None,
            "avg_return": (sum(rets_ok) / len(rets_ok)) if rets_ok else None,
            "return_coverage": (len(rets_ok) / n) if n else 0.0,
        }
    return out


def adjudicate(stats: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """
    附录 B 预注册（写死后再看数）：
    - vague 命中率落在 45%–55%（≈抛硬币）且 clear − vague ≥ 10pp → 噪声实锤，全套 other
    - |clear − vague| < 10pp → 两桶接近，归档标签卫生
    - 其他灰色：记录，不自动全套改造（人工看）
    """
    clear = stats.get("clear") or {}
    vague = stats.get("vague_hard") or {}
    ch, vh = clear.get("hit_rate"), vague.get("hit_rate")
    cn, vn = clear.get("n") or 0, vague.get("n") or 0
    if ch is None or vh is None or cn < 10 or vn < 10:
        return {
            "verdict": "insufficient_n",
            "action": "样本不足，不自动立项；保持观察",
            "detail": f"clear_n={cn} vague_n={vn}",
        }
    diff_pp = (ch - vh) * 100
    vague_coin = 0.45 <= vh <= 0.55
    if vague_coin and diff_pp >= 10:
        return {
            "verdict": "noise_confirmed",
            "action": "立项 other 桶全套改造（抽取 other + 验证 skip + 命中率仅 clear + 覆盖率指标；历史不回填仅前向；shadow 计数器清零切 post_other）",
            "detail": f"vague_hit={vh*100:.1f}%~50%, clear_hit={ch*100:.1f}%, gap={diff_pp:.1f}pp",
        }
    if abs(diff_pp) < 10:
        return {
            "verdict": "similar_buckets",
            "action": "归档为标签卫生问题，不动命中率分母",
            "detail": f"clear_hit={ch*100:.1f}%, vague_hit={vh*100:.1f}%, abs_gap={abs(diff_pp):.1f}pp<10",
        }
    # clear 高但 vague 不是硬币，或 vague 更差很多但不在 50 带
    if diff_pp >= 10:
        return {
            "verdict": "clear_better_vague_not_coin",
            "action": "灰色区：clear 显著更好但 vague 非~50%；建议仍做 other 前向隔离，但需人工确认（不自动等同噪声实锤）",
            "detail": f"clear_hit={ch*100:.1f}%, vague_hit={vh*100:.1f}%, gap={diff_pp:.1f}pp",
        }
    return {
        "verdict": "vague_better_or_mixed",
        "action": "灰色区：vague 不差于 clear；归档卫生优先，慎重改分母",
        "detail": f"clear_hit={ch*100:.1f}%, vague_hit={vh*100:.1f}%, gap={diff_pp:.1f}pp",
    }


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def main():
    eng, session, db_label = connect()
    try:
        rows = fetch_rows(session)
    finally:
        session.close()
        eng.dispose()

    counts = Counter()
    for r in rows:
        counts[classify_text(r.get("prediction_content") or "", r.get("prediction_type") or "")] += 1

    n = len(rows)
    vague_n = counts["vague_hard"]
    pct = (vague_n / n * 100) if n else 0.0
    decision_share = "立项 other 桶改造" if pct >= 10 else "归档不改抽取（占比<10%）"

    verified = [r for r in rows if r.get("is_correct") is not None]
    stats = bucket_stats(rows)
    adj = adjudicate(stats)

    # 粗反推：若 vague=50% 且总体 h，clear 理论值
    overall_h = (
        sum(1 for r in verified if bool(r.get("is_correct"))) / len(verified)
        if verified
        else None
    )
    v_share = (stats.get("vague_hard", {}).get("n") or 0) / len(verified) if verified else 0
    c_share = (stats.get("clear", {}).get("n") or 0) / len(verified) if verified else 0
    implied = None
    if overall_h is not None and c_share > 0.05:
        # overall ≈ v_share*0.5 + c_share*X + other residual ignore
        # 仅用 clear+vague 归一
        vs = stats.get("vague_hard", {}).get("n") or 0
        cs = stats.get("clear", {}).get("n") or 0
        if cs + vs > 0 and overall_h is not None:
            # 用实际两桶加权反推无意义；改用顾问公式示意
            implied = (overall_h - v_share * 0.5) / c_share if c_share else None

    samples_vague = []
    for r in rows:
        if classify_text(r.get("prediction_content") or "", r.get("prediction_type") or "") != "vague_hard":
            continue
        if len(samples_vague) >= 12:
            break
        samples_vague.append(r)

    def row_bucket(name: str) -> str:
        s = stats.get(name) or {"n": 0, "correct": 0, "hit_rate": None, "avg_return": None, "return_coverage": 0}
        return (
            f"| {name} | {s['n']} | {s['correct']} | {fmt_pct(s['hit_rate'])} | "
            f"{fmt_pct(s['avg_return'])} | {fmt_pct(s['return_coverage'])} |"
        )

    report = f"""# L3 模糊硬标占比估计（只读）

- 日期：2026-07-29
- 数据源：`{db_label}`
- 范围：未删除且 prediction_type 为 up/down/bullish/bearish
- **不改**抽取/验证代码；**不部署**本轮新功能

## 判定标准（规则，可复现）

| 标签 | 规则 |
| --- | --- |
| **clear** | 文本含与标签**同向**的明确涨/跌词，且无反向明确词 |
| **vague_hard** | 无明确方向词；或仅有观望/可能/震荡等模糊词；或多空词并存却硬标 up/down |
| **weak** | 有明确词但与标签**反向**（错标信号，另计） |
| **empty** | prediction_content 空 |

明确多：看多/看涨/加仓/突破/…；明确空：看空/看跌/减仓/破位/…
模糊：关注/观望/可能/震荡/分化/择时/中性/博弈/…

## 样本量与结果（全量 up/down）

| 项 | 值 |
| --- | ---: |
| 总样本 (up/down) | **{n}** |
| clear | {counts['clear']} ({(counts['clear']/n*100) if n else 0:.1f}%) |
| **vague_hard** | **{vague_n} ({pct:.1f}%)** |
| weak（反向明确） | {counts['weak']} ({(counts['weak']/n*100) if n else 0:.1f}%) |
| empty | {counts['empty']} |
| 已结论子集 | {len(verified)} |

## 决策 A（占比门槛：≥10% 才讨论改造）

- 模糊硬标占比估计：**{pct:.1f}%**
- **动作（占比门）：{decision_share}**
- 最终是否全套改分母，以 **附录 A 分组裁决 + 附录 B 预注册表** 为准。

## 样例（vague_hard 截断）

"""
    for r in samples_vague:
        report += (
            f"- id={r['id']} type={r['prediction_type']} correct={r.get('is_correct')}: "
            f"{(r.get('prediction_content') or '')[:120]!r}\n"
        )
    if not samples_vague:
        report += "- （无）\n"

    report += f"""
## 局限

- 词典启发式，非人工金标准；可能低估「向上/低吸」等未入库行话、高估「长文含关注」。
- 未读原帖全文，只看 `prediction_content`。
- 附录 A/B 为改造前裁决；本文件不实施抽取变更。

---

## 附录 A：已验证结论 vague / clear 分组裁决（只读）

- 分母：`is_correct IS NOT NULL` 且未删且 up/down（与上表同一启发式）
- 收益：有 start_nav/current_nav 时，up 用区间收益、down 用负区间收益

| 桶 | n | 正确数 | 命中率 | 平均方向收益 | 收益覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: |
{row_bucket("clear")}
{row_bucket("vague_hard")}
{row_bucket("weak")}
{row_bucket("empty")}

- 已结论总体命中率：{fmt_pct(overall_h)}（n={len(verified)}）
- 已结论中 vague 占比：{v_share*100:.1f}%；clear 占比：{c_share*100:.1f}%
- 若假设 vague 为纯噪声 50%、用总体反推 clear 理论命中（示意，非证明）：{fmt_pct(implied) if implied is not None else "n/a"}

### 附录 A 判读结果（严格执行附录 B，禁止改口）

- **verdict**：`{adj["verdict"]}`
- **detail**：{adj["detail"]}
- **action**：{adj["action"]}

---

## 附录 B：预注册判读表（先写规则再跑数）

| 查询结果 | 结论 | 动作 |
| --- | --- | --- |
| vague 命中率 ∈ **[45%, 55%]** 且 **clear − vague ≥ 10pp** | 噪声实锤 | **全套改造**：抽取加 other 类；验证 skip other；命中率**只算 clear**；新增**覆盖率**（clear/全部可标）；历史**不回填**仅前向；L1 shadow **清零重计**并标 `post_other` |
| **|clear - vague| < 10pp** | LLM 在模糊文本里读出了相近信号 | **归档标签卫生**，不动分母 |
| clear 高 >=10pp 但 vague 不在硬币带 | 灰色 | 可前向隔离 other，**不**自动等同噪声实锤，需人工确认 |
| 任一带 n<10 | 样本不足 | 不自动立项 |

本次跑数落入：`{adj["verdict"]}`。

### 与 L1 shadow 的交互（插旗）

- 改造会改变「可验证结论」定义；**禁止**把 pre-other 与 post-other 结论混进同一 +150 复评计数。
- 代码侧：`L1_SHADOW_DATA_ERA=pre_other|post_other`，other 上线日写 `L3_OTHER_CUTOVER_AT` 并重置 `L1_SHADOW_BASELINE_VERIFIED` / `L1_SHADOW_STARTED_AT`。
- 详见 `src/services/l1_shadow.reeval_status` 返回的 `data_era` / `era_note`。
"""
    out = ROOT / "docs" / "L3_VAGUE_LABEL_ESTIMATE.md"
    out.write_text(report, encoding="utf-8")
    # Windows 控制台可能是 gbk：只打摘要
    summary = (
        f"verdict={adj['verdict']} detail={adj['detail']} "
        f"action={adj['action'][:80]} wrote={out}"
    )
    print(summary.encode("utf-8", errors="replace").decode("utf-8"))
    try:
        print(summary)
    except UnicodeEncodeError:
        print(summary.encode("gbk", errors="replace").decode("gbk"))


if __name__ == "__main__":
    main()
