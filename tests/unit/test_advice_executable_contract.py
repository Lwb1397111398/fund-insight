"""v2 可执行建议契约：stage3 提示词注入、兜底形状、软闸透传、后端附挂标的。

这一族判据钉的是"改动真实落进链路"：
① stage3 提示词必须带【市场整体状态】块与新动作契约字段（回退旧措辞 ⇒ 红）；
② LLM 失败的兜底 JSON 形状必须含 position/sector_actions（缺键 ⇒ 红）；
③ three_stage 必须把 market_state 原样传到 stage3（漏传 ⇒ 闸在链路上等于没装）；
④ 板块动作的基金代码由后端按映射附挂，映射答不出必须留空（不许硬凑）。
"""
import json

from src.analyzer.llm_analyzer import LLMAnalyzer


class _CapAnalyzer(LLMAnalyzer):
    """跳过父类 LLM 客户端初始化，只捕获提示词、返回预置 JSON。"""

    def __init__(self):
        self.captured = []
        self.reply = "{}"
        self.fail = False

    def _call_llm(self, prompt, **kwargs):
        self.captured.append(prompt)
        if self.fail:
            raise RuntimeError("模拟断网")
        return self.reply


def _market_weak():
    return {
        "stance": "weak",
        "stance_label": "弱势",
        "position_label": "市场弱势，建议减仓为主（持仓不超过三成），不要新开仓买入",
        "enforce": True,
        "index_code": "510300",
        "basis": "沪深300ETF（510300）近5日 -2.1%、近20日 -4.0%，净值截至 2026-10-09",
    }


def test_stage3_prompt_injects_market_block_and_action_contract():
    analyzer = _CapAnalyzer()
    analyzer.reply = json.dumps(
        {
            "advice_type": "hold",
            "advice_content": "大盘弱势，建议减仓",
            "position": {"follow_gate": True, "position_label": "建议减仓", "override_reason": ""},
            "sector_actions": [{"sector": "白酒", "action": "reduce", "confidence": 60, "reason": "若持有白酒建议减半", "horizon": "short"}],
        },
        ensure_ascii=False,
    )
    result = analyzer.generate_advice_stage3(
        {"summary": "v"}, {"near_term_trend": "down", "mid_term_trend": "down", "sector_predictions": {}},
        [], market_state=_market_weak(),
    )
    prompt = analyzer.captured[0]
    # 市场闸事实块进了提示词，且带 stance 与仓位话术
    assert "【市场整体状态】" in prompt
    assert "stance" in prompt and "weak" in prompt
    assert "不要新开仓买入" in prompt  # position_label 原文进上下文
    assert "近5日 -2.1%" in prompt      # basis 数据依据进上下文
    # 新输出契约字段
    for key in ("sector_actions", "follow_gate", "override_reason", "只看不动手", "全部卖出"):
        assert key in prompt
    # "关注"禁令必须写死在提示词里（否则模型又回到含糊话）
    assert '禁止出现"关注"' in prompt
    # 结果原样透传
    assert result["sector_actions"][0]["action"] == "reduce"
    assert result["_stage3_status"] == "ok"


def test_stage3_without_market_state_degrades_to_unknown_block():
    """没接闸的老调用（单测/兜底路径）不许崩：提示词里落 unknown 数据缺失措辞。"""
    analyzer = _CapAnalyzer()
    analyzer.generate_advice_stage3({}, {}, [])
    prompt = analyzer.captured[0]
    assert "【市场整体状态】" in prompt
    assert "unknown" in prompt
    assert "大盘数据缺失" in prompt


def test_stage3_llm_failure_fallback_carries_v2_keys():
    analyzer = _CapAnalyzer()
    analyzer.fail = True
    result = analyzer.generate_advice_stage3({}, {}, [], market_state=_market_weak())
    assert result["_stage3_status"] == "failed"
    assert "sector_actions" in result and result["sector_actions"] == []
    assert result["position"]["follow_gate"] is True
    assert "position_label" in result["position"]


def test_three_stage_passes_market_state_to_stage3():
    seen = {}

    class _Passthrough(LLMAnalyzer):
        def __init__(self):
            pass

        def analyze_viewpoints_stage1(self, viewpoints, use_cache=True):
            return {"summary": "无观点", "_stage_status": "ok"}

        def analyze_predictions_stage2(self, predictions, viewpoint_summary, bloggers,
                                       use_cache=True):
            return {"summary": "ok", "_stage_status": "ok", "near_term_trend": "neutral",
                    "mid_term_trend": "neutral", "sector_predictions": {}}

        def generate_advice_stage3(self, viewpoint_summary, prediction_analysis, bloggers,
                                   market_state=None, correction_note=None, use_cache=True,
                                   sector_states=None):
            seen["market_state"] = market_state
            return {"advice_type": "hold", "advice_content": "建议观望",
                    "market_sentiment": "neutral", "confidence": 50,
                    "_stage3_status": "ok", "_stage_status": "ok"}

    gate = _market_weak()
    _Passthrough().generate_investment_advice_three_stage(
        bloggers=[], predictions=[{"prediction_id": 1}], viewpoints=[], market_state=gate
    )
    assert seen["market_state"] == gate  # 原样传，不许中途换/丢


def test_attach_sector_fund_targets_maps_known_and_blanks_unknown(monkeypatch):
    from src.api.routes import advice as advice_route

    def fake_get_fund_for_sector(sector):
        if sector == "半导体":
            return {"code": "512480", "name": "半导体ETF"}
        return None  # 映射答不出：不许硬凑

    monkeypatch.setattr(
        "src.constants.sector_fund_map.get_fund_for_sector", fake_get_fund_for_sector
    )
    actions = [{"sector": "半导体"}, {"sector": "火星板块"}, {"sector": "  "}]
    out = advice_route._attach_sector_fund_targets(actions)
    assert out[0]["fund_code"] == "512480" and out[0]["fund_name"] == "半导体ETF"
    assert out[1]["fund_code"] == "" and out[1]["fund_name"] == ""
    assert out[2]["fund_code"] == ""
