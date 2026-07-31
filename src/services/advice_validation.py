"""
投资建议 P1：输出 Schema 与业务校验

规则：
- 证据不足 → 不落库
- 坏 JSON / 缺必填 / 非法枚举 → 不落库
- 任一 LLM 阶段失败（_stage_status=failed）→ 不落库
- 引用必须属于本次证据集（若提供 prediction_id）
- 本模块只校验，不写库
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


ADVICE_TYPES = frozenset({"buy", "sell", "hold", "watch"})
# 统一对外口径：bullish/bearish/neutral；兼容模型偶发 greedy/fearful
MARKET_SENTIMENTS = frozenset({"bullish", "bearish", "neutral", "greedy", "fearful"})
SENTIMENT_NORMALIZE = {
    "greedy": "bullish",
    "fearful": "bearish",
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
}
SHORT_STRATEGIES = frozenset({"momentum", "watch", "wait"})
MID_STRATEGIES = frozenset({"position", "reduce", "wait"})
RISK_LEVELS = frozenset({"high", "medium", "low"})


@dataclass
class ValidationResult:
    ok: bool
    code: str = "ok"
    message: str = ""
    normalized: Optional[Dict[str, Any]] = None
    details: Dict[str, Any] = field(default_factory=dict)


def validate_evidence_for_advice(data: Dict[str, Any]) -> ValidationResult:
    """
    证据充分性。
    允许「仅有预测」或「预测+观点」；两者都空则拒绝。
    不要求必须有观点（观点是低权辅助）。
    """
    predictions = data.get("predictions") or []
    viewpoints = data.get("viewpoints") or []
    meta = data.get("meta") or {}

    if meta.get("insufficient_evidence") or (not predictions and not viewpoints):
        return ValidationResult(
            ok=False,
            code="insufficient_evidence",
            message="当前无有效预测与观点，无法形成可审计建议",
            details={
                "prediction_count": len(predictions),
                "viewpoint_count": len(viewpoints),
            },
        )

    # 只有观点、没有任何当前预测：证据偏弱，拒绝正式建议
    if not predictions:
        return ValidationResult(
            ok=False,
            code="no_actionable_predictions",
            message="缺少当前可行动预测，拒绝生成正式投资建议",
            details={"viewpoint_count": len(viewpoints)},
        )

    return ValidationResult(
        ok=True,
        code="ok",
        message="证据充足",
        details={
            "prediction_count": len(predictions),
            "viewpoint_count": len(viewpoints),
            "conflict_count": len(data.get("conflicts") or []),
            "exclusion_count": len(data.get("exclusions") or []),
        },
    )


def _as_int_confidence(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 100:
        return None
    return n


def _validate_term_block(
    block: Any, *, kind: str, strategies: frozenset
) -> Optional[str]:
    if block is None:
        return None  # 可选
    if not isinstance(block, dict):
        return f"{kind} 必须是对象"
    strategy = block.get("strategy")
    if strategy is not None and strategy not in strategies:
        return f"{kind}.strategy 非法: {strategy}"
    risk = block.get("risk_level")
    if risk is not None and risk not in RISK_LEVELS:
        return f"{kind}.risk_level 非法: {risk}"
    if "valid_days" in block and block["valid_days"] is not None:
        try:
            vd = int(block["valid_days"])
            if vd < 0 or vd > 365:
                return f"{kind}.valid_days 超出范围"
        except (TypeError, ValueError):
            return f"{kind}.valid_days 必须是整数"
    return None


REQUIRED_STAGE_KEYS = (
    "stage1_viewpoints",
    "stage2_predictions",
    "stage3_advice",
)


def detect_stage_failures(advice: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    从三阶段输出中收集失败阶段。

    fail-closed：缺少 _stage_statuses 或任一阶段标记缺失/非 ok|failed
    一律视为失败（避免旧缓存/截断输出被静默放行）。
    """
    failures: List[Dict[str, str]] = []

    statuses = advice.get("_stage_statuses")
    if not isinstance(statuses, dict):
        return [
            {
                "stage": "pipeline",
                "reason": "missing_stage_statuses",
            }
        ]

    for name in REQUIRED_STAGE_KEYS:
        st = statuses.get(name)
        if st is None:
            failures.append({"stage": name, "reason": "missing_stage_marker"})
        elif st == "failed":
            failures.append({"stage": name, "reason": "explicit_failed"})
        elif st != "ok":
            failures.append({"stage": name, "reason": f"unknown_status:{st}"})

    # 兼容软文案回退（即使 statuses 写成 ok）
    vp = advice.get("viewpoint_summary") or {}
    if isinstance(vp, dict) and vp.get("summary") == "观点分析失败":
        if not any(f["stage"] == "stage1_viewpoints" for f in failures):
            failures.append({"stage": "stage1_viewpoints", "reason": "soft_fallback"})

    pa = advice.get("prediction_analysis") or {}
    if isinstance(pa, dict) and pa.get("summary") == "预测分析失败":
        if not any(f["stage"] == "stage2_predictions" for f in failures):
            failures.append({"stage": "stage2_predictions", "reason": "soft_fallback"})

    if advice.get("_stage3_status") == "failed" or (
        advice.get("_stage_status") == "failed"
        and not any(f["stage"] == "stage3_advice" for f in failures)
    ):
        # 若总状态 failed 但 stage3 未单列，补一条
        if not any(f["stage"] == "stage3_advice" for f in failures):
            if advice.get("_stage3_status") == "failed" or "stage3" in str(
                advice.get("_stage_reason") or ""
            ):
                failures.append(
                    {
                        "stage": "stage3_advice",
                        "reason": advice.get("_stage_reason") or "stage3_failed",
                    }
                )

    return failures


# 缓存键版本：升级 prompt/模型策略时递增，迫使重算
ADVICE_PROMPT_VERSION = "advice.three_stage.v1"


def build_advice_cache_key(
    evidence_hash: str,
    *,
    prompt_version: str = ADVICE_PROMPT_VERSION,
    model_version: str = "",
) -> str:
    """
    P2 生成缓存键 = evidence_hash + prompt 版本 + 模型版本。
    任一变化必须重算。
    """
    eh = (evidence_hash or "").strip()
    pv = (prompt_version or "").strip() or "unknown_prompt"
    mv = (model_version or "").strip() or "unknown_model"
    return f"{eh}|{pv}|{mv}"


def advice_cache_digest(cache_key: str) -> str:
    """
    缓存键的定长存储摘要（32 字符），用于写入 investment_advice.data_hash。

    data_hash 列是 VARCHAR(32)，而 cache_key = evidence_hash(64位SHA256)
    + prompt 版本 + 模型版本，整体约 100 字符，直接入库会触发
    StringDataRightTruncation。故入库与其命中比较统一改用本 MD5 摘要；
    可读的 cache_key 仍用于接口返回与日志展示。
    """
    return hashlib.md5((cache_key or "").encode("utf-8")).hexdigest()


def validate_advice_output(
    advice: Any,
    evidence: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """
    校验模型输出；通过时返回 normalized 可落库字典。
    枚举/结构只约束**新写入**；读路径不调用本函数。
    """
    if not isinstance(advice, dict):
        return ValidationResult(
            ok=False,
            code="invalid_json_shape",
            message="建议输出不是 JSON 对象",
        )

    # 阶段失败或标记缺失：fail-closed，不落库
    stage_failures = detect_stage_failures(advice)
    if stage_failures:
        return ValidationResult(
            ok=False,
            code="stage_failed",
            message="三阶段分析存在失败或阶段标记缺失，拒绝落库不完整建议",
            details={"stage_failures": stage_failures},
        )

    advice_type = advice.get("advice_type")
    if advice_type not in ADVICE_TYPES:
        return ValidationResult(
            ok=False,
            code="invalid_advice_type",
            message=f"advice_type 非法: {advice_type}",
        )

    content = advice.get("advice_content")
    if not isinstance(content, str) or not content.strip():
        return ValidationResult(
            ok=False,
            code="missing_advice_content",
            message="advice_content 缺失或为空",
        )

    raw_sentiment = advice.get("market_sentiment")
    if raw_sentiment not in MARKET_SENTIMENTS:
        return ValidationResult(
            ok=False,
            code="invalid_market_sentiment",
            message=f"market_sentiment 非法: {raw_sentiment}",
        )
    sentiment = SENTIMENT_NORMALIZE[raw_sentiment]

    confidence = _as_int_confidence(advice.get("confidence"))
    if confidence is None:
        return ValidationResult(
            ok=False,
            code="invalid_confidence",
            message="confidence 必须是 0-100 的整数",
        )

    reasoning = advice.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        return ValidationResult(
            ok=False,
            code="invalid_reasoning",
            message="reasoning 必须是字符串",
        )

    for key in ("suggested_sectors", "avoid_sectors"):
        val = advice.get(key)
        if val is not None and not isinstance(val, list):
            return ValidationResult(
                ok=False,
                code=f"invalid_{key}",
                message=f"{key} 必须是数组",
            )

    err = _validate_term_block(
        advice.get("short_term"), kind="short_term", strategies=SHORT_STRATEGIES
    )
    if err:
        return ValidationResult(ok=False, code="invalid_short_term", message=err)

    err = _validate_term_block(
        advice.get("mid_term"), kind="mid_term", strategies=MID_STRATEGIES
    )
    if err:
        return ValidationResult(ok=False, code="invalid_mid_term", message=err)

    # 引用校验：若输出带 prediction_id，必须属于证据集
    evidence = evidence or {}
    allowed_pred_ids: Set[int] = set()
    for p in evidence.get("predictions") or []:
        pid = p.get("prediction_id")
        if pid is not None:
            try:
                allowed_pred_ids.add(int(pid))
            except (TypeError, ValueError):
                pass

    ref_preds = advice.get("referenced_prediction_ids")
    if ref_preds is not None:
        if not isinstance(ref_preds, list):
            return ValidationResult(
                ok=False,
                code="invalid_references",
                message="referenced_prediction_ids 必须是数组",
            )
        bad = []
        for x in ref_preds:
            try:
                xid = int(x)
            except (TypeError, ValueError):
                bad.append(x)
                continue
            if allowed_pred_ids and xid not in allowed_pred_ids:
                bad.append(xid)
        if bad:
            return ValidationResult(
                ok=False,
                code="reference_not_in_evidence",
                message="引用了不在本次证据集中的预测",
                details={"bad_ids": bad},
            )

    normalized = {
        "advice_type": advice_type,
        "advice_content": content.strip(),
        "market_sentiment": sentiment,
        "confidence": confidence,
        "reasoning": (reasoning or "").strip() or None,
        "risk_warning": advice.get("risk_warning"),
        "suggested_sectors": list(advice.get("suggested_sectors") or [])[:5],
        "avoid_sectors": list(advice.get("avoid_sectors") or [])[:5],
        "short_term": advice.get("short_term") or {},
        "mid_term": advice.get("mid_term") or {},
        "avoid_reasoning": advice.get("avoid_reasoning") or "",
        "viewpoint_summary": advice.get("viewpoint_summary"),
        "prediction_analysis": advice.get("prediction_analysis"),
    }
    return ValidationResult(
        ok=True,
        code="ok",
        message="输出校验通过",
        normalized=normalized,
    )
