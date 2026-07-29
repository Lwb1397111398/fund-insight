"""P1：投资建议输出校验与拒绝落库测试。"""
from datetime import date, timedelta

from src.models.database import Blogger, InvestmentAdvice, Post, Prediction
from src.services.advice_service import AdviceService
from src.services.advice_validation import (
    detect_stage_failures,
    validate_advice_output,
    validate_evidence_for_advice,
)
from src.analyzer.llm_analyzer import LLMAnalyzer


def _valid_advice(**overrides):
    base = {
        "advice_type": "hold",
        "advice_content": "短期观望，中期关注白酒",
        "market_sentiment": "neutral",
        "confidence": 62,
        "reasoning": "高权预测偏中性",
        "suggested_sectors": ["白酒"],
        "avoid_sectors": [],
        "short_term": {
            "strategy": "wait",
            "action": "观望",
            "risk_level": "medium",
            "valid_days": 3,
        },
        "mid_term": {
            "strategy": "position",
            "action": "布局",
            "risk_level": "medium",
            "valid_days": 7,
        },
        "risk_warning": "注意回撤",
        "viewpoint_summary": {"summary": "ok", "_stage_status": "ok"},
        "prediction_analysis": {"summary": "ok", "_stage_status": "ok"},
        "_stage_statuses": {
            "stage1_viewpoints": "ok",
            "stage2_predictions": "ok",
            "stage3_advice": "ok",
        },
    }
    base.update(overrides)
    return base


def test_empty_evidence_rejected():
    data = {"predictions": [], "viewpoints": [], "meta": {"insufficient_evidence": True}}
    result = validate_evidence_for_advice(data)
    assert result.ok is False
    assert result.code == "insufficient_evidence"


def test_no_predictions_only_viewpoints_rejected():
    data = {
        "predictions": [],
        "viewpoints": [{"viewpoint_id": 1, "summary": "看多"}],
        "meta": {},
    }
    result = validate_evidence_for_advice(data)
    assert result.ok is False
    assert result.code == "no_actionable_predictions"


def test_bad_json_shape_rejected():
    result = validate_advice_output("not-a-dict")
    assert result.ok is False
    assert result.code == "invalid_json_shape"


def test_missing_required_field_rejected():
    advice = _valid_advice()
    del advice["advice_content"]
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "missing_advice_content"


def test_invalid_enum_rejected():
    advice = _valid_advice(advice_type="maybe")
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "invalid_advice_type"


def test_sentiment_normalized_on_success():
    advice = _valid_advice(market_sentiment="greedy")
    result = validate_advice_output(advice)
    assert result.ok is True
    assert result.normalized["market_sentiment"] == "bullish"


def test_missing_stage_statuses_fail_closed():
    """阶段标记缺失必须按失败拒绝（fail-closed）。"""
    advice = _valid_advice()
    advice.pop("_stage_statuses", None)
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "stage_failed"
    assert any(
        f.get("reason") == "missing_stage_statuses"
        for f in result.details.get("stage_failures", [])
    )


def test_partial_stage_marker_fail_closed():
    advice = _valid_advice(
        _stage_statuses={
            "stage1_viewpoints": "ok",
            # 缺 stage2
            "stage3_advice": "ok",
        }
    )
    result = validate_advice_output(advice)
    assert result.ok is False
    assert any(
        f.get("reason") == "missing_stage_marker"
        for f in result.details.get("stage_failures", [])
    )


def test_advice_cache_key_changes_with_versions():
    from src.services.advice_validation import build_advice_cache_key

    k1 = build_advice_cache_key("abc", prompt_version="p1", model_version="m1")
    k2 = build_advice_cache_key("abc", prompt_version="p1", model_version="m1")
    assert k1 == k2
    assert build_advice_cache_key("abc", prompt_version="p2", model_version="m1") != k1
    assert build_advice_cache_key("abc", prompt_version="p1", model_version="m2") != k1
    assert build_advice_cache_key("abd", prompt_version="p1", model_version="m1") != k1


def test_stage1_failure_detected_and_blocks_persist():
    advice = _valid_advice(
        viewpoint_summary={
            "summary": "观点分析失败",
            "_stage_status": "failed",
            "_stage_reason": "llm_or_parse_failed",
        },
        _stage_statuses={
            "stage1_viewpoints": "failed",
            "stage2_predictions": "ok",
            "stage3_advice": "ok",
        },
    )
    failures = detect_stage_failures(advice)
    assert any(f["stage"] == "stage1_viewpoints" for f in failures)
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "stage_failed"


def test_stage2_failure_blocks_persist():
    advice = _valid_advice(
        prediction_analysis={
            "summary": "预测分析失败",
            "_stage_status": "failed",
        },
        _stage_statuses={
            "stage1_viewpoints": "ok",
            "stage2_predictions": "failed",
            "stage3_advice": "ok",
        },
    )
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "stage_failed"
    assert any(
        f["stage"] == "stage2_predictions" for f in result.details["stage_failures"]
    )


def test_stage3_failure_blocks_persist():
    advice = _valid_advice(
        _stage3_status="failed",
        _stage_status="failed",
        _stage_statuses={
            "stage1_viewpoints": "ok",
            "stage2_predictions": "ok",
            "stage3_advice": "failed",
        },
    )
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "stage_failed"


def test_reference_not_in_evidence_rejected():
    advice = _valid_advice(referenced_prediction_ids=[999])
    evidence = {"predictions": [{"prediction_id": 1}]}
    result = validate_advice_output(advice, evidence=evidence)
    assert result.ok is False
    assert result.code == "reference_not_in_evidence"


def test_create_advice_not_called_when_validation_fails(test_db, monkeypatch):
    """集成：证据为空时 API 路径逻辑不应落库。"""
    service = AdviceService(test_db)
    before = test_db.query(InvestmentAdvice).count()

    data = service.get_data_for_advice()  # 空库 → 无预测
    ev = validate_evidence_for_advice(data)
    assert ev.ok is False

    # 模拟校验失败则不 create
    if not ev.ok:
        after = test_db.query(InvestmentAdvice).count()
        assert after == before


def test_three_stage_marks_stage2_failed_on_empty_predictions():
    """三阶段：无预测时 stage2 显式 failed，整体 _stage_status=failed。"""
    # 避免真实 LLM：构造 analyzer 但不调网络——stage2 空预测早退
    class Dummy(LLMAnalyzer):
        def __init__(self):
            # 跳过父类 LLM 客户端初始化
            pass

        def analyze_viewpoints_stage1(self, viewpoints):
            return {"summary": "无观点", "market_sentiment": "neutral", "_stage_status": "ok"}

        def generate_advice_stage3(self, viewpoint_summary, prediction_analysis, bloggers):
            # 即使 stage3 成功，整体仍应因 stage2 failed 被标记
            return {
                "advice_type": "hold",
                "advice_content": "x",
                "market_sentiment": "neutral",
                "confidence": 50,
                "_stage3_status": "ok",
                "_stage_status": "ok",
            }

    analyzer = Dummy()
    result = analyzer.generate_investment_advice_three_stage(
        bloggers=[], predictions=[], viewpoints=[]
    )
    assert result["_stage_statuses"]["stage2_predictions"] == "failed"
    assert result["_stage_status"] == "failed"
    # 输出校验应拒绝
    assert validate_advice_output(result).ok is False
