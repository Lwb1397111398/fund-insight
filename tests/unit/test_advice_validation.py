"""P1：投资建议输出校验与拒绝落库测试。"""
from datetime import date, timedelta

from src.models.database import Blogger, InvestmentAdvice, Post, Prediction
from src.services.advice_service import AdviceService
from src.services.advice_validation import (
    ADVICE_PROMPT_VERSION,
    advice_cache_digest,
    build_advice_cache_key,
    detect_stage_failures,
    validate_advice_output,
    validate_evidence_for_advice,
)
from src.analyzer.llm_analyzer import LLMAnalyzer


def _valid_advice(**overrides):
    base = {
        "advice_type": "hold",
        "advice_content": "短期观望，中期继续持有白酒",
        "market_sentiment": "neutral",
        "confidence": 62,
        "reasoning": "高权预测偏中性",
        "position": {
            "follow_gate": True,
            "position_label": "维持现有仓位，不加不减",
            "override_reason": "",
        },
        "sector_actions": [
            {
                "sector": "白酒",
                "action": "hold",
                "confidence": 60,
                "reason": "两位A级博主中性偏多",
                "horizon": "mid",
            }
        ],
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


def _evidence(**market_state):
    """构造带市场闸状态的证据集（默认无 stance ⇒ 闸不生效）。"""
    return {"predictions": [{"prediction_id": 1}], "market_state": market_state or {}}


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

        def analyze_viewpoints_stage1(self, viewpoints, use_cache=True):
            return {"summary": "无观点", "market_sentiment": "neutral", "_stage_status": "ok"}

        def generate_advice_stage3(self, viewpoint_summary, prediction_analysis, bloggers,
                                   market_state=None, correction_note=None, use_cache=True,
                                   sector_states=None):
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


def test_advice_cache_digest_fits_varchar32():
    """data_hash 列为 VARCHAR(32)：完整 cache_key（64位证据哈希+版本+模型）
    约百字符，直接入库会 StringDataRightTruncation；摘要必须定长 32 且稳定。"""
    evidence_hash = "482d07cbf030ff5904f1e69af662b574d85d470af56bccb6f6907dc1708c4821"
    cache_key = build_advice_cache_key(
        evidence_hash,
        prompt_version="advice.three_stage.v1",
        model_version="glm-5-2-260617",
    )
    # 完整 cache_key 确实超长（说明为什么不能直接入库）
    assert len(cache_key) > 32

    digest = advice_cache_digest(cache_key)
    assert len(digest) == 32  # 恰好塞进 VARCHAR(32)
    # 确定性：同样的键得到同样摘要，命中比较才成立
    assert digest == advice_cache_digest(cache_key)
    # 键变化 → 摘要变化（证据/版本/模型任一改动即重算）
    other = build_advice_cache_key(
        "另一证据", prompt_version="advice.three_stage.v1", model_version="glm-5-2-260617"
    )
    assert advice_cache_digest(other) != digest


# ───────────────────────── v2 契约：板块动作、软闸一致性、措辞纪律 ─────────────────────────


def _buy_advice(**overrides):
    """弱势里敢喊买入的一份建议；用 overrides 摆 follow_gate/override_reason。"""
    base = _valid_advice(
        advice_type="buy",
        advice_content="大盘虽有回撤，但三位A级博主一致看涨，建议买入半导体",
        position={"follow_gate": False, "position_label": "可小仓位买入半导体", "override_reason": "三位A级博主近两周半导体全部判对"},
        sector_actions=[
            {
                "sector": "半导体",
                "action": "buy",
                "confidence": 70,
                "reason": "三位A级博主一致看涨",
                "horizon": "short",
            }
        ],
        suggested_sectors=["半导体"],
    )
    base.update(overrides)
    return base


def test_prompt_version_bumped_to_v2():
    """契约换了（position/sector_actions/禁"关注"）⇒ 版本必须升，旧缓存不许再命中。"""
    assert ADVICE_PROMPT_VERSION == "advice.three_stage.v2"


def test_missing_sector_actions_rejected():
    advice = _valid_advice()
    advice.pop("sector_actions")
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "missing_sector_actions"


def test_empty_sector_actions_rejected():
    result = validate_advice_output(_valid_advice(sector_actions=[]))
    assert result.ok is False
    assert result.code == "missing_sector_actions"


def test_invalid_action_enum_rejected():
    advice = _valid_advice(
        sector_actions=[{"sector": "白酒", "action": "holdin", "confidence": 60}]
    )
    result = validate_advice_output(advice)
    assert result.ok is False
    assert result.code == "invalid_sector_action"


def test_action_label_and_market_stance_normalized():
    result = validate_advice_output(_valid_advice(), evidence=_evidence(stance="range"))
    assert result.ok is True
    item = result.normalized["sector_actions"][0]
    assert item["action_label"] == "继续持有"  # 中文动作标签由后端统一挂，不靠模型自觉
    assert result.normalized["market_stance"] == "range"


def test_ambiguous_wording_rejected():
    """老病根："关注"说不清买卖 ⇒ v2 明令拒绝。"""
    result = validate_advice_output(
        _valid_advice(advice_content="短期观望，中期关注白酒")
    )
    assert result.ok is False
    assert result.code == "ambiguous_wording"


def test_content_without_action_word_rejected():
    result = validate_advice_output(
        _valid_advice(advice_content="市场情绪中性，博主意见分歧较大")
    )
    assert result.ok is False
    assert result.code == "missing_action_word"


def test_weak_gate_buy_claiming_follow_rejected():
    """弱势 + 声称跟随系统闸 + 还喊买入 ⇒ 自相矛盾，拒。"""
    advice = _buy_advice(
        position={"follow_gate": True, "position_label": "维持现有仓位不加仓", "override_reason": ""}
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="weak"))
    assert result.ok is False
    assert result.code == "gate_conflict"


def test_weak_gate_buy_without_override_reason_rejected():
    """弱势逆市看多但不给理由 ⇒ 拒（软闸：允许 override，不许没说法）。"""
    advice = _buy_advice(
        position={"follow_gate": False, "position_label": "可小仓位买入半导体", "override_reason": ""}
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="very_weak"))
    assert result.ok is False
    assert result.code == "missing_override_reason"


def test_weak_gate_buy_with_override_reason_ok():
    result = validate_advice_output(_buy_advice(), evidence=_evidence(stance="weak"))
    assert result.ok is True
    assert result.normalized["position"]["override_reason"]
    assert result.normalized["market_stance"] == "weak"


def test_weak_gate_sellish_actions_follow_gate_ok():
    """弱势下只给减仓/清仓/回避 + follow_gate=true ⇒ 正常通过，且这是最常见形状。"""
    advice = _valid_advice(
        advice_type="sell",
        advice_content="大盘弱势，建议清仓白酒、回避半导体",
        position={"follow_gate": True, "position_label": "建议减仓，白酒全部卖出", "override_reason": ""},
        sector_actions=[
            {"sector": "白酒", "action": "exit", "confidence": 75, "reason": "若持有白酒，建议全部卖出", "horizon": "short"},
            {"sector": "半导体", "action": "avoid", "confidence": 65, "reason": "近5日博主转空", "horizon": "mid"},
        ],
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="weak"))
    assert result.ok is True


def test_unknown_stance_never_enforces():
    """数据缺失 ⇒ 闸失效：buyish 且 follow_gate=true 也不再拦。"""
    advice = _buy_advice(
        position={"follow_gate": True, "position_label": "大盘数据缺失，暂无法给出仓位建议", "override_reason": ""}
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="unknown"))
    assert result.ok is True


def test_strong_stance_buy_follow_gate_ok():
    advice = _buy_advice(
        position={"follow_gate": True, "position_label": "市场强势，可正常买入", "override_reason": ""}
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="strong"))
    assert result.ok is True


def test_advice_type_buy_triggers_gate_even_without_buyish_action():
    """sector_actions 里没有 buy，但 advice_type=buy 也是买入指令 ⇒ 弱势照样要理由。"""
    advice = _valid_advice(
        advice_type="buy",
        advice_content="建议买入证券板块",
        position={"follow_gate": False, "position_label": "可买入证券", "override_reason": ""},
        sector_actions=[
            {"sector": "证券", "action": "watch", "confidence": 60, "reason": "先看一天", "horizon": "short"}
        ],
    )
    result = validate_advice_output(advice, evidence=_evidence(stance="weak"))
    assert result.ok is False
    assert result.code == "missing_override_reason"
