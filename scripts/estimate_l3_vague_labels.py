#!/usr/bin/env python
"""
L3 只读：估计 up/down 标签中「模糊文本被硬标」占比。
不改抽取/验证代码。

判定标准见 docs/L3_VAGUE_LABEL_ESTIMATE.md（脚本与文档同步）。
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# 明确方向词（命中任一 → 倾向「非模糊」）
CLEAR_BULL = re.compile(
    r"(看多|看涨|看升|上涨|上行|加仓|增持|布局多|做多|突破|新高|乐观|强势|反弹|翻倍|大涨|暴涨|买入|抄底)",
    re.I,
)
CLEAR_BEAR = re.compile(
    r"(看空|看跌|看淡|下跌|下行|减仓|清仓|做空|破位|悲观|走弱|大跌|暴跌|卖出|规避|回避|离场)",
    re.I,
)
# 模糊/不可验话术
VAGUE = re.compile(
    r"(关注|观望|等待|观察|可能|或许|也许|大概|不一定|不确定|谨慎|震荡|波动|分化|"
    r"结构性|择时|再看|看看|留意|跟踪|保持|中性|平衡|灵活|视情况|取决于|有待|"
    r"风险与机遇|机会与风险|多空|博弈|分歧)",
    re.I,
)


def classify_text(text: str, ptype: str) -> str:
    """
    返回：clear / vague_hard / weak / empty
    - clear: 有与标签同向的明确词
    - vague_hard: 无明确方向词，或仅有模糊词，却标了 up/down
    - weak: 有明确词但与标签方向冲突（另计）
    - empty: 无文本
    """
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
            return "weak"  # 文本偏空却标涨
        if has_bull and has_bear:
            return "vague_hard"  # 多空都有、硬选一边
        if has_vague or (not has_bull and not has_bear):
            return "vague_hard"
        return "vague_hard"
    if p in ("down", "bearish"):
        if has_bear and not has_bull:
            return "clear"
        if has_bull and not has_bear:
            return "weak"
        if has_bull and has_bear:
            return "vague_hard"
        if has_vague or (not has_bull and not has_bear):
            return "vague_hard"
        return "vague_hard"
    return "other_type"


def main():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        eng = create_engine(url, pool_pre_ping=True)
        db_label = "DATABASE_URL"
    else:
        eng = create_engine(f"sqlite:///{ROOT / 'data' / 'fund_insight.db'}")
        db_label = "local sqlite"
    Session = sessionmaker(bind=eng)
    s = Session()
    try:
        try:
            rows = s.execute(
                text(
                    """
                    SELECT id, prediction_type, prediction_content, is_correct, is_deleted
                    FROM predictions
                    WHERE COALESCE(is_deleted, false) = false
                      AND lower(COALESCE(prediction_type, '')) IN ('up', 'down', 'bullish', 'bearish')
                    """
                )
            ).mappings().all()
        except Exception:
            rows = s.execute(
                text(
                    """
                    SELECT id, prediction_type, prediction_content, is_correct, is_deleted
                    FROM predictions
                    WHERE COALESCE(is_deleted, 0) = 0
                      AND lower(COALESCE(prediction_type, '')) IN ('up', 'down', 'bullish', 'bearish')
                    """
                )
            ).mappings().all()
    finally:
        s.close()
        eng.dispose()

    counts = Counter()
    verified_vague = 0
    verified_total = 0
    samples_vague = []
    for r in rows:
        label = classify_text(r.get("prediction_content") or "", r.get("prediction_type") or "")
        counts[label] += 1
        if r.get("is_correct") is not None:
            verified_total += 1
            if label == "vague_hard":
                verified_vague += 1
        if label == "vague_hard" and len(samples_vague) < 12:
            samples_vague.append(
                {
                    "id": r["id"],
                    "type": r["prediction_type"],
                    "content": (r.get("prediction_content") or "")[:120],
                    "is_correct": r.get("is_correct"),
                }
            )

    n = len(rows)
    vague_n = counts["vague_hard"]
    pct = (vague_n / n * 100) if n else 0.0
    verified_vague_pct = (verified_vague / verified_total * 100) if verified_total else 0.0

    # 立项阈值：≥10%
    decision = "立项 other 桶改造" if pct >= 10 else "归档不改抽取（占比<10%）"

    c_clear = counts["clear"]
    c_weak = counts["weak"]
    c_empty = counts["empty"]
    report = f"""# L3 模糊硬标占比估计（只读）

- 日期：2026-07-29
- 数据源：`{db_label}`
- 范围：未删除且 prediction_type 为 up/down/bullish/bearish
- **不改**抽取/验证代码

## 判定标准（规则，可复现）

| 标签 | 规则 |
| --- | --- |
| **clear** | 文本含与标签**同向**的明确涨/跌词，且无反向明确词 |
| **vague_hard** | 无明确方向词；或仅有观望/可能/震荡等模糊词；或多空词并存却硬标 up/down |
| **weak** | 有明确词但与标签**反向**（错标信号，另计） |
| **empty** | prediction_content 空 |

明确多：看多/看涨/加仓/突破/…；明确空：看空/看跌/减仓/破位/…
模糊：关注/观望/可能/震荡/分化/择时/中性/博弈/…

## 样本量与结果

| 项 | 值 |
| --- | ---: |
| 总样本 (up/down) | **{n}** |
| clear | {c_clear} ({(c_clear/n*100) if n else 0:.1f}%) |
| **vague_hard** | **{vague_n} ({pct:.1f}%)** |
| weak（反向明确） | {c_weak} ({(c_weak/n*100) if n else 0:.1f}%) |
| empty | {c_empty} |
| 已结论子集 | {verified_total} |
| 已结论中 vague_hard | {verified_vague} ({verified_vague_pct:.1f}%) |

## 决策（预注册：≥10% 立项）

- 模糊硬标占比估计：**{pct:.1f}%**
- **动作：{decision}**

## 样例（vague_hard 截断）

"""
    for srow in samples_vague:
        report += (
            f"- id={srow['id']} type={srow['type']} correct={srow['is_correct']}: "
            f"{srow['content']!r}\n"
        )
    if not samples_vague:
        report += "- （无）\n"
    report += """
## 局限

- 词典启发式，非人工金标准；可能低估「行话暗示方向」、高估「长文含关注二字」。
- 未读原帖全文，只看 `prediction_content`。
- 若立项：加 other/unknown、验证 skip、分母剔除；本报告不实施。
"""
    out = ROOT / "docs" / "L3_VAGUE_LABEL_ESTIMATE.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"[wrote] {out}")


if __name__ == "__main__":
    main()
