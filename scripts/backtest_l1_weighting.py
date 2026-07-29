#!/usr/bin/env python
"""
L1 walk-forward 回测（只读）。

三臂：equal / legacy / l1_beta
指标：加权命中率 + 简易方向收益
floor 敏感性：0.2 / 0.4 / 0.6

用法：
  python scripts/backtest_l1_weighting.py
  python scripts/backtest_l1_weighting.py --write-report docs/L1_WEIGHTING_BACKTEST_REPORT.md
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.services import l1_weighting as l1


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        eng = create_engine(url, pool_pre_ping=True)
    else:
        db_path = ROOT / "data" / "fund_insight.db"
        eng = create_engine(f"sqlite:///{db_path}")
    return eng, sessionmaker(bind=eng)()


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    return None


def load_rows(session) -> List[Dict[str, Any]]:
    sql = text(
        """
        SELECT
            p.id,
            p.blogger_id,
            p.fund_code,
            p.prediction_type,
            p.confidence,
            p.prediction_date,
            p.target_date,
            p.verified_at,
            p.is_correct,
            p.is_deleted,
            p.start_nav,
            p.current_nav,
            b.accuracy_rate,
            b.total_predictions,
            b.name AS blogger_name
        FROM predictions p
        JOIN bloggers b ON b.id = p.blogger_id
        WHERE p.is_deleted = false
          AND p.is_correct IS NOT NULL
          AND COALESCE(p.prediction_type, '') <> 'flat'
        ORDER BY COALESCE(p.verified_at, p.target_date, p.prediction_date)
        """
    )
    # SQLite uses 0/1
    try:
        rows = session.execute(sql).mappings().all()
    except Exception:
        sql_sqlite = text(
            """
            SELECT
                p.id,
                p.blogger_id,
                p.fund_code,
                p.prediction_type,
                p.confidence,
                p.prediction_date,
                p.target_date,
                p.verified_at,
                p.is_correct,
                p.is_deleted,
                p.start_nav,
                p.current_nav,
                b.accuracy_rate,
                b.total_predictions,
                b.name AS blogger_name
            FROM predictions p
            JOIN bloggers b ON b.id = p.blogger_id
            WHERE p.is_deleted = 0
              AND p.is_correct IS NOT NULL
              AND COALESCE(p.prediction_type, '') <> 'flat'
            ORDER BY COALESCE(p.verified_at, p.target_date, p.prediction_date)
            """
        )
        rows = session.execute(sql_sqlite).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d["conclusion_date"] = (
            _as_date(d.get("verified_at"))
            or _as_date(d.get("target_date"))
            or _as_date(d.get("prediction_date"))
        )
        d["is_correct"] = bool(d["is_correct"])
        out.append(d)
    return [x for x in out if x["conclusion_date"] is not None]


def split_half(rows: List[Dict[str, Any]]) -> Tuple[List[Dict], List[Dict], date]:
    ordered = sorted(rows, key=lambda x: (x["conclusion_date"], x["id"]))
    if len(ordered) < 4:
        mid = max(1, len(ordered) // 2)
    else:
        mid = len(ordered) // 2
    train, test = ordered[:mid], ordered[mid:]
    cut = test[0]["conclusion_date"] if test else (
        train[-1]["conclusion_date"] if train else date.today()
    )
    return train, test, cut


def train_stats(train: List[Dict]) -> Dict[int, Dict[str, Any]]:
    """按博主聚合 train 窗 c/n，并带 legacy 字段（取 train 末出现的物化分）。"""
    agg: Dict[int, Dict[str, Any]] = {}
    for r in train:
        bid = int(r["blogger_id"])
        st = agg.setdefault(
            bid,
            {
                "c": 0,
                "n": 0,
                "accuracy_rate": float(r.get("accuracy_rate") or 0),
                "total_predictions": int(r.get("total_predictions") or 0),
                "name": r.get("blogger_name"),
            },
        )
        st["n"] += 1
        if r["is_correct"]:
            st["c"] += 1
        # 物化字段随行刷新（近似「当时表上的值」；严格历史快照不可得）
        st["accuracy_rate"] = float(r.get("accuracy_rate") or st["accuracy_rate"])
        st["total_predictions"] = int(r.get("total_predictions") or st["total_predictions"])
    return agg


def directional_return(row: Dict) -> Optional[float]:
    """用 start_nav/current_nav 近似区间收益；方向对齐 up/down。"""
    s, e = row.get("start_nav"), row.get("current_nav")
    if s is None or e is None:
        try:
            s = float(s) if s is not None else None
            e = float(e) if e is not None else None
        except (TypeError, ValueError):
            return None
    try:
        s = float(s)
        e = float(e)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    raw = (e / s) - 1.0
    ptype = (row.get("prediction_type") or "").lower()
    if ptype in ("up", "bullish"):
        return raw
    if ptype in ("down", "bearish"):
        return -raw
    return None


def eval_arm(
    test: List[Dict],
    stats: Dict[int, Dict[str, Any]],
    *,
    arm: str,
    p0: float,
    alpha: float,
    min_n: int,
    floor_ratio: float,
) -> Dict[str, Any]:
    # 先算每个 test 行的 raw weight
    p_hats_for_floor: List[float] = []
    prepared = []
    for r in test:
        bid = int(r["blogger_id"])
        st = stats.get(bid, {"c": 0, "n": 0, "accuracy_rate": 0.0, "total_predictions": 0})
        conf = r.get("confidence")
        if arm == "equal":
            w = 1.0
            p_hat = None
        elif arm == "legacy":
            rel = l1.legacy_reliability_score(st["accuracy_rate"], st.get("n") or st["total_predictions"])
            w, _ = l1.prediction_weight_from_reliability(rel, conf, weight_floor=None)
            p_hat = rel / 100.0
        else:  # l1_beta
            p_hat = l1.beta_hit_rate(st["c"], st["n"], p0=p0, alpha=alpha)
            rel = p_hat * 100
            w, _ = l1.prediction_weight_from_reliability(rel, conf, weight_floor=None)
            p_hats_for_floor.append(p_hat)
        prepared.append((r, w, p_hat))

    floor = 0.0
    floored_n = 0
    if arm == "l1_beta":
        floor = l1.compute_weight_floor(p_hats_for_floor, floor_ratio=floor_ratio)
        new_prep = []
        for r, w, p_hat in prepared:
            rel = (p_hat or l1.DEFAULT_P0) * 100
            w2, fl = l1.prediction_weight_from_reliability(
                rel, r.get("confidence"), weight_floor=floor
            )
            if fl:
                floored_n += 1
            new_prep.append((r, w2, p_hat))
        prepared = new_prep

    tw = sum(w for _, w, _ in prepared) or 1.0
    hit_w = sum(w for r, w, _ in prepared if r["is_correct"])
    hit_eq = sum(1 for r, _, _ in prepared if r["is_correct"]) / max(1, len(prepared))

    rets = []
    w_rets = []
    for r, w, _ in prepared:
        ret = directional_return(r)
        if ret is None:
            continue
        rets.append(ret)
        w_rets.append((w, ret))
    avg_ret = sum(rets) / len(rets) if rets else None
    w_ret = (
        sum(w * rt for w, rt in w_rets) / sum(w for w, _ in w_rets) if w_rets else None
    )

    # 权重集中度：top20% 权重占比
    weights = sorted([w for _, w, _ in prepared], reverse=True)
    k = max(1, int(len(weights) * 0.2))
    top_share = (sum(weights[:k]) / tw) if weights else 0.0

    return {
        "arm": arm,
        "n": len(prepared),
        "weighted_hit_rate": hit_w / tw,
        "equal_hit_rate_on_same": hit_eq,
        "avg_return": avg_ret,
        "weighted_return": w_ret,
        "return_coverage": len(rets) / max(1, len(prepared)),
        "floor": floor,
        "floored_ratio": floored_n / max(1, len(prepared)) if arm == "l1_beta" else 0.0,
        "top20_weight_share": top_share,
    }


def decide(results: Dict[str, Dict], *, return_coverage_ok: bool) -> Dict[str, str]:
    """预注册决策表。"""
    eq = results["equal"]["weighted_hit_rate"]
    leg = results["legacy"]["weighted_hit_rate"]
    l1b = results["l1_beta"]["weighted_hit_rate"]
    # 平局带：±1pp
    band = 0.01
    better_eq = l1b > eq + band
    better_leg = l1b > leg + band
    worse_eq = l1b < eq - band
    worse_leg = l1b < leg - band

    ret_note = "n/a"
    if return_coverage_ok:
        er = results["equal"].get("weighted_return")
        lr = results["l1_beta"].get("weighted_return")
        if er is not None and lr is not None:
            if lr + 1e-12 >= er:
                ret_note = "return_ok"
            else:
                ret_note = "return_worse"

    if worse_eq or worse_leg:
        action = "no_gate_investigate"
        reason = "l1_beta 命中率输给 equal 或 legacy（超 1pp）"
    elif better_eq and better_leg and ret_note != "return_worse":
        action = "gate_on"
        reason = "l1_beta 命中率同时优于 equal 与 legacy，且收益未劣化"
    elif abs(l1b - eq) <= band and abs(l1b - leg) <= band:
        action = "shadow"
        reason = "命中率分不出胜负（±1pp 内）→ shadow 双跑积累后再评"
    elif (better_eq or better_leg) and not (worse_eq or worse_leg):
        # 只赢一臂、不输另一臂
        if ret_note == "return_worse":
            action = "no_gate_investigate"
            reason = "命中有改善但收益劣化"
        else:
            action = "shadow"
            reason = "只相对一臂明显更好，另一臂接近 → shadow"
    else:
        action = "shadow"
        reason = "结果落在灰色区 → shadow"

    return {"action": action, "reason": reason, "ret_note": ret_note}


def render_report(
    *,
    n_total: int,
    n_train: int,
    n_test: int,
    cut: date,
    p0: float,
    base_results: Dict[str, Dict],
    floor_sens: Dict[float, Dict],
    decision: Dict[str, str],
    legacy_note: str,
    db_label: str,
) -> str:
    lines = [
        "# L1 加权 Walk-forward 回测报告",
        "",
        f"- 生成日：{date.today().isoformat()}",
        f"- 数据源：`{db_label}`",
        f"- 切片：时序对半（方案 A）；cut=`{cut.isoformat()}`",
        f"- 样本：全量已结论 {n_total}；train {n_train}；test {n_test}",
        f"- p0={p0}；α 默认 15；min_n=10（权重用全 train c/n+Beta，不因 min_n 丢弃）",
        f"- flag：**未开**（本报告不部署）",
        "",
        "## Legacy 臂语义确认",
        "",
        legacy_note,
        "",
        "## 预注册决策规则",
        "",
        "| 结果 | 动作 |",
        "| --- | --- |",
        "| l1_beta 命中率 > equal **且** > legacy，收益不劣化 | **开闸** |",
        "| l1_beta 输给任一臂（>1pp） | **不开闸**，查因 |",
        "| 分不出胜负（±1pp） | **shadow** 双跑 N 周复评 |",
        "",
        "## 主结果（floor_ratio=0.4）",
        "",
        "| 臂 | n | 加权命中率 | 同集等权命中 | 加权收益 | 收益覆盖 | top20%权重 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in ("equal", "legacy", "l1_beta"):
        r = base_results[arm]
        wr = r["weighted_return"]
        wr_s = f"{wr*100:.2f}%" if wr is not None else "n/a"
        lines.append(
            f"| {arm} | {r['n']} | {r['weighted_hit_rate']*100:.2f}% | "
            f"{r['equal_hit_rate_on_same']*100:.2f}% | {wr_s} | "
            f"{r['return_coverage']*100:.1f}% | {r['top20_weight_share']*100:.1f}% |"
        )
    lines += [
        "",
        f"- l1 floor 绝对值：{base_results['l1_beta']['floor']:.4f}",
        f"- floor 触发率：{base_results['l1_beta']['floored_ratio']*100:.1f}%",
        "",
        "## floor 敏感性（仅 l1_beta）",
        "",
        "| floor_ratio | 加权命中率 | 加权收益 | floor触发率 |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for fr in sorted(floor_sens):
        r = floor_sens[fr]
        wr = r["weighted_return"]
        wr_s = f"{wr*100:.2f}%" if wr is not None else "n/a"
        lines.append(
            f"| {fr} | {r['weighted_hit_rate']*100:.2f}% | {wr_s} | {r['floored_ratio']*100:.1f}% |"
        )

    lines += [
        "",
        "## 决策（按预注册表，禁止现场改口）",
        "",
        f"- **动作**：`{decision['action']}`",
        f"- **理由**：{decision['reason']}",
        f"- 收益附注：{decision['ret_note']}",
        "",
        "## 局限",
        "",
        "- 数据窗短（约数周），对半切统计功效弱；平局/shadow 是预期内结局。",
        "- legacy 臂的 accuracy_rate 取自**当前物化列**在 train 行上的快照近似，非历史逐日重算 verify_score。",
        "- 收益用 start_nav/current_nav，非完整组合回测。",
        "",
        "## 下一步",
        "",
        "- 若 `gate_on`：人工复核后才允许 `ADVICE_L1_HIT_WEIGHTING=1`（本交付默认仍关）。",
        "- 若 `shadow` / `no_gate`：保持 flag 关；可加 shadow 日志或扩样本后再跑。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-report",
        default=str(ROOT / "docs" / "L1_WEIGHTING_BACKTEST_REPORT.md"),
    )
    parser.add_argument("--p0", type=float, default=float(os.getenv("L1_P0", "0.609")))
    parser.add_argument("--alpha", type=float, default=float(os.getenv("L1_ALPHA", "15")))
    args = parser.parse_args()

    eng, session = _connect()
    db_label = "DATABASE_URL" if os.getenv("DATABASE_URL") else "local sqlite"
    try:
        rows = load_rows(session)
    finally:
        session.close()
        eng.dispose()

    # 全局 p0 可用全量命中校准（报告仍默认用设计 0.609，另打印实测）
    if rows:
        empiric_p0 = sum(1 for r in rows if r["is_correct"]) / len(rows)
    else:
        empiric_p0 = args.p0

    train, test, cut = split_half(rows)
    stats = train_stats(train)

    legacy_note = (
        "Step1 **未改** `Blogger.accuracy_rate` 写入语义：仍为 "
        "`total_verify_score/(total_predictions*100)*100`（加权评分，"
        "分母=verify_count>0 且非 flat 且未删）。"
        "API 新增的 `hit_rate` 是只读聚合，**不**回写该列。"
        "因此 legacy 臂 = 现网 EvidenceBuilder flag 关路径"
        "（`accuracy_rate` + `50+(acc-50)*min(1,n/10)`），对比基准真实。"
        f" 全量实证命中率={empiric_p0*100:.2f}%（设计 p0={args.p0}）。"
    )

    base = {}
    for arm in ("equal", "legacy", "l1_beta"):
        base[arm] = eval_arm(
            test,
            stats,
            arm=arm,
            p0=args.p0,
            alpha=args.alpha,
            min_n=10,
            floor_ratio=0.4,
        )

    floor_sens = {}
    for fr in (0.2, 0.4, 0.6):
        floor_sens[fr] = eval_arm(
            test,
            stats,
            arm="l1_beta",
            p0=args.p0,
            alpha=args.alpha,
            min_n=10,
            floor_ratio=fr,
        )

    cov = base["l1_beta"]["return_coverage"]
    decision = decide(base, return_coverage_ok=cov >= 0.30)

    report = render_report(
        n_total=len(rows),
        n_train=len(train),
        n_test=len(test),
        cut=cut,
        p0=args.p0,
        base_results=base,
        floor_sens=floor_sens,
        decision=decision,
        legacy_note=legacy_note,
        db_label=db_label,
    )
    path = Path(args.write_report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[wrote] {path}")


if __name__ == "__main__":
    main()
