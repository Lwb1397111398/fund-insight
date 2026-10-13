# -*- coding: utf-8 -*-
"""板块自身强弱豁免大盘软闸（2026-10-13 老板点名：大盘弱 ≠ 板块弱）。

老板原话的意思：大盘弱势不代表没有买入空间——某个板块可能马上就要雄起。
落成的规矩（不是一刀切放开，也不是继续一刀切拦死）：
① 豁免只认**净值算出来的事实**：该板块代表基金按与大盘同一套阈值分档，
   stance=strong（近20日 ≥+3% 且近5日仍在涨）才算"自身强势"；range/unknown 不豁免；
② 大盘 weak/very_weak 时，对 strong 板块给 buy/add ⇒ 正常动作，follow_gate=true 照过；
   对没涨出来的板块给 buy/add ⇒ 照旧要 follow_gate=false + 写明理由；
③ sector_states 缺失（老调用方/老证据）⇒ strong=∅，行为与旧版逐字一致（fail-closed）；
④ 板块来源与代表基金：入选证据（预测 sector + 观点看多看空板块）→ get_fund_for_sector
   （与附挂标的同一把尺子）→ fund_history 净值；查不出/点不够 ⇒ unknown；
⑤ 强弱进证据哈希（审计要能还原当时看到什么）、进 stage3 提示词（模型知道才能正常给）、
   附挂回 sector_actions（页面摆出"敢买的依据"）。
"""
from datetime import date, timedelta

import pytest

from src.analyzer.market_state import compute_sector_states, classify_stance
from src.models.database import FundHistory
from src.services.advice_validation import validate_advice_output

# ───────────────────────────── 校验层：豁免语义（核心） ─────────────────────────────


def _advice(actions, advice_type="hold", follow_gate=True, override_reason=""):
    """构造一份除 sector_actions/advice_type/position 外全合规的输出。"""
    return {
        "advice_type": advice_type,
        "advice_content": "大盘弱势三成仓位，红利继续持有",
        "market_sentiment": "neutral",
        "confidence": 60,
        "reasoning": "两位A级博主看多红利",
        "position": {
            "follow_gate": follow_gate,
            "position_label": "三成仓位以内",
            "override_reason": override_reason,
        },
        "sector_actions": actions,
        "suggested_sectors": [],
        "avoid_sectors": [],
        "short_term": {"strategy": "wait", "action": "观望", "risk_level": "medium", "valid_days": 3},
        "mid_term": {"strategy": "position", "action": "布局", "risk_level": "medium", "valid_days": 7},
        "risk_warning": "注意回撤",
        "viewpoint_summary": {"summary": "ok", "_stage_status": "ok"},
        "prediction_analysis": {"summary": "ok", "_stage_status": "ok"},
        "_stage_statuses": {
            "stage1_viewpoints": "ok",
            "stage2_predictions": "ok",
            "stage3_advice": "ok",
        },
    }


def _buy(sector, action="buy"):
    return {"sector": sector, "action": action, "confidence": 70,
            "reason": "板块情绪回暖", "horizon": "short"}


def _evidence(stance, sector_states=None):
    ev = {"predictions": [{"prediction_id": 1}], "market_state": {"stance": stance}}
    if sector_states is not None:
        ev["sector_states"] = sector_states
    return ev


STRONG_HONGLI = {"红利": {"stance": "strong", "r20": 4.2, "fund_code": "510880", "nav_as_of": "2026-10-13"}}
RANGE_HONGLI = {"红利": {"stance": "range", "r20": 0.5, "fund_code": "510880", "nav_as_of": "2026-10-13"}}


def test_weak_market_buy_on_strong_sector_passes_without_override():
    """①+②：大盘弱 + 板块自身 strong（净值事实）⇒ 给买入、声称跟随仓位建议——过闸，不再 gate_conflict。"""
    result = validate_advice_output(
        _advice([_buy("红利")], advice_type="buy"),
        evidence=_evidence("weak", STRONG_HONGLI),
    )
    assert result.ok is True, f"强势板块买入被误拦：{result.code} {result.message}"


def test_weak_market_buy_on_range_sector_still_rejected():
    """②'：range 不是"涨出来的事实"——照旧 gate_conflict（豁免只认 strong，不放水）。"""
    result = validate_advice_output(
        _advice([_buy("红利")]),
        evidence=_evidence("weak", RANGE_HONGLI),
    )
    assert result.ok is False
    assert result.code == "gate_conflict"
    assert "红利" in result.message  # 拒绝理由要点名是哪个板块没依据


def test_weak_market_buy_mixed_sectors_only_unsupported_blocks():
    """②''：strong 板块豁免、range 板块仍拦——豁免不是整份放行。"""
    states = {"红利": {"stance": "strong"}, "半导体": {"stance": "range"}}
    result = validate_advice_output(
        _advice([_buy("红利", "add"), _buy("半导体", "buy")]),
        evidence=_evidence("weak", states),
    )
    assert result.ok is False
    assert result.code == "gate_conflict"
    assert result.details["sectors"] == ["半导体"]


def test_no_sector_states_behaves_exactly_like_old_gate():
    """③：sector_states 缺失（老调用方/旧证据）⇒ 行为与旧版逐字一致，fail-closed。"""
    result = validate_advice_output(
        _advice([_buy("红利")], advice_type="buy"),
        evidence=_evidence("weak", None),
    )
    assert result.ok is False and result.code == "gate_conflict"


def test_unknown_sector_never_exempts():
    """①：映射查不出/净值不够 ⇒ stance=unknown ⇒ 不豁免（"雄起"得有数据撑腰）。"""
    states = {"红利": {"stance": "unknown", "basis": "净值不足"}}
    result = validate_advice_output(
        _advice([_buy("红利")]),
        evidence=_evidence("weak", states),
    )
    assert result.ok is False and result.code == "gate_conflict"


def test_buy_type_with_supported_sector_passes_even_flag():
    """②'''：advice_type=buy 只要有 strong 板块的 buyish 撑腰就放行；
    只押没涨的板块时整体 buy 照样拦。"""
    ok = validate_advice_output(
        _advice([_buy("红利")], advice_type="buy"),
        evidence=_evidence("weak", STRONG_HONGLI),
    )
    assert ok.ok is True
    bad = validate_advice_output(
        _advice([{"sector": "红利", "action": "hold", "confidence": 60, "reason": "持有", "horizon": "mid"}],
                advice_type="buy"),
        evidence=_evidence("weak", STRONG_HONGLI),
    )
    assert bad.ok is False and bad.code == "gate_conflict"


# ───────────────────────── market_state：compute_sector_states ─────────────────────────


@pytest.fixture
def mapped_funds(monkeypatch):
    """板块→基金映射换成可控桩（compute_sector_states 函数体内 import，打模块属性）。"""
    table = {"红利": {"code": "512890", "name": "红利低波ETF"}, "医药": {"code": "512010", "name": "医药ETF"}}

    def fake(sector):
        return table.get(sector)

    monkeypatch.setattr("src.constants.sector_fund_map.get_fund_for_sector", fake)
    return table


def _seed_navs(db, code, name, closes):
    base = date(2026, 9, 1)
    for i, v in enumerate(closes):
        db.add(FundHistory(fund_code=code, fund_name=name,
                           nav_date=base + timedelta(days=i), nav=v))
    db.commit()


def test_sector_strong_when_navs_rise(test_db, mapped_funds):
    """与大盘同一把尺子：涨出来的板块判 strong，basis 带实体（基金名/代码/涨幅/净值日）。"""
    rising = [1.0] * 8 + [1.0 * (1 + 0.008 * i) for i in range(1, 13)]  # 后12天连涨 ≈ +9.6%
    _seed_navs(test_db, "512890", "红利低波ETF", rising)
    states = compute_sector_states(test_db, ["红利"])
    st = states["红利"]
    assert st["stance"] == "strong"
    assert st["fund_code"] == "512890"
    assert st["r20"] is not None and st["r20"] > 3.0
    assert "红利低波ETF" in st["basis"] and "净值截至" in st["basis"]


def test_sector_unknown_without_mapping(test_db, mapped_funds):
    """映射答不出的板块 ⇒ unknown 并说明原因（不硬凑，也不拦死别的板块）。"""
    states = compute_sector_states(test_db, ["火星板块"])
    st = states["火星板块"]
    assert st["stance"] == "unknown"
    assert "映射答不出" in st["basis"]


def test_sector_unknown_with_thin_navs(test_db, mapped_funds):
    """净值点不足 MIN_ROWS ⇒ unknown（判不出就不豁免，与大盘闸同规矩）。"""
    _seed_navs(test_db, "512010", "医药ETF", [1.0, 1.01, 1.02])
    states = compute_sector_states(test_db, ["医药"])
    assert states["医药"]["stance"] == "unknown"


def test_sector_states_dedup_and_cap(test_db, mapped_funds, monkeypatch):
    """重复板块去重、上限截断（防无界查询）；异常的单板块记 unknown 不崩整链。"""
    calls = []

    def counting(sector):
        calls.append(sector)
        if sector == "炸了":
            raise RuntimeError("模拟映射炸了")
        return {"code": "512890", "name": "红利低波ETF"}

    monkeypatch.setattr("src.constants.sector_fund_map.get_fund_for_sector", counting)
    _seed_navs(test_db, "512890", "红利低波ETF", [1.0] * 10)
    states = compute_sector_states(test_db, ["红利", "红利", "  ", "炸了"])
    assert "红利" in states and "炸了" in states
    assert states["炸了"]["stance"] == "unknown"
    assert calls.count("红利") == 1  # 去重


def test_compute_sector_states_threshold_is_shared_with_market():
    """一把尺子钉死：板块分档与大盘 classify_stance 同一函数（不复制阈值）。"""
    assert classify_stance(1.0, 4.0, -1.0) == "strong"
    assert classify_stance(-1.0, -4.0, -9.0) == "weak"


# ───────────────────── 证据包：sector_states 进哈希（审计还原） ─────────────────────

def test_hash_changes_with_sector_states(test_db):
    """板块强弱变了（哪怕博主/预测/观点/大盘全没变）⇒ evidence_hash 必须变：
    豁免判定依赖它，哈希不记就等于审计还原不了"当时看到了什么"。"""
    from src.services.advice_evidence import AdviceEvidenceBuilder, EvidencePack

    builder = AdviceEvidenceBuilder(test_db)
    pack_a = EvidencePack(as_of_date=date(2026, 10, 13),
                          sector_states={"红利": {"stance": "strong", "r20": 4.2}})
    pack_b = EvidencePack(as_of_date=date(2026, 10, 13),
                          sector_states={"红利": {"stance": "range", "r20": 0.2}})
    pack_none = EvidencePack(as_of_date=date(2026, 10, 13))
    ha, hb, hn = (builder._hash_pack(p) for p in (pack_a, pack_b, pack_none))
    assert ha != hb  # 板块档位变 ⇒ 哈希变
    assert ha != hn and hb != hn  # 记没记进哈希也要可辨


def test_pack_builds_sector_states_from_selected_sectors(test_db, monkeypatch):
    """证据包的板块来源：入选预测的 sector + 观点的看多看空板块，都进 sector_states。"""
    from src.services.advice_evidence import AdviceEvidenceBuilder

    seen = {}

    def fake(db, sectors):
        seen["sectors"] = list(sectors)
        return {s: {"stance": "range"} for s in set(sectors)}

    monkeypatch.setattr("src.services.advice_evidence.compute_sector_states", fake)
    blogger = _blogger(test_db)
    _pred(test_db, blogger, date(2026, 10, 20), sector="白酒")
    _viewpoint(test_db, "作者甲", "医药回暖")
    pack = AdviceEvidenceBuilder(test_db).build(as_of=date(2026, 10, 13))
    assert "白酒" in seen["sectors"]  # 预测的板块
    assert pack.sector_states  # 进包（值来自桩）


def _blogger(db, name="板块证据博主", accuracy=80, total=10, correct=8):
    """最小可用博主行（预测外键需要；字段与 test_advice_evidence 样板一致）。"""
    from src.models.database import Blogger
    b = Blogger(name=name, platform="wechat", accuracy_rate=accuracy,
                total_predictions=total, correct_predictions=correct, grade="A")
    db.add(b)
    db.flush()
    return b


def _pred(db, blogger, target, sector="白酒"):
    from src.models.database import Post, Prediction
    post = Post(blogger_id=blogger.id, title="e", content="e",
                post_date=target - timedelta(days=3))
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id, sector=sector,
                   prediction_type="up", prediction_content=f"{sector}涨",
                   confidence=70, prediction_date=post.post_date,
                   target_date=target, status="pending", is_correct=None,
                   is_deleted=False, is_expired=False)
    db.add(p)
    db.commit()
    return p


def _viewpoint(db, author, summary, day=None):
    from src.models.database import Viewpoint
    v = Viewpoint(author=author, summary=summary, content=summary,
                  viewpoint_date=day or date(2026, 10, 13),
                  sectors_bullish=["医药"], is_deleted=False,
                  reasoning="r", is_summary=True)
    db.add(v)
    db.commit()
    return v


# ───────────────────────── stage3 提示词：板块事实块 ─────────────────────────

def test_stage3_prompt_injects_sector_facts_block():
    """模型得先看见"哪些板块自己在涨"，才敢在弱势大盘下正常给买入（规则5同步改写）。"""
    from src.analyzer.llm_analyzer import LLMAnalyzer

    class _Cap(LLMAnalyzer):
        def __init__(self):
            self.captured = []

        def _call_llm(self, prompt, **kwargs):
            self.captured.append(prompt)
            return "{}"

    cap = _Cap()
    cap.generate_advice_stage3(
        {}, {}, [],
        market_state={"stance": "weak", "position_label": "弱势减仓", "basis": "510300 近20日 -4%"},
        sector_states={"红利": {"stance": "strong", "stance_label": "强势",
                                "r20": 4.2, "nav_as_of": "2026-10-13"}},
    )
    prompt = cap.captured[0]
    # 断言钉**数据块本体**（带括注的标题行）：规则5的措辞里也引用块名，光断"【各板块自身强弱】"
    # 会被那句规则文本顶穿（第一版这样判过，M121 因此变 no-op——改判括注行才咬得住）。
    assert "【各板块自身强弱】（系统按各板块代表基金净值算出的事实" in prompt
    assert '"板块": "红利"' in prompt and "强势" in prompt
    assert "不算逆着大盘" in prompt  # 规则5改写：strong 板块可以正常给买入

    cap2 = _Cap()
    cap2.generate_advice_stage3({}, {}, [], market_state={"stance": "weak"})
    p2 = cap2.captured[0]
    # 没有板块数据 ⇒ 不塞事实 JSON 块（规则5里引用这个块名的一句仍在，是措辞不是数据）
    assert '"板块": ' not in p2
    assert "【各板块自身强弱】（系统按各板块代表基金净值算出的事实" not in p2


def test_three_stage_passes_sector_states_to_stage3():
    """透传不许半路丢：three_stage 的 sector_states 原样进 stage3。"""
    from src.analyzer.llm_analyzer import LLMAnalyzer

    seen = {}

    class _P(LLMAnalyzer):
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
            seen["sector_states"] = sector_states
            return {"advice_type": "hold", "advice_content": "建议观望",
                    "market_sentiment": "neutral", "confidence": 50,
                    "_stage3_status": "ok", "_stage_status": "ok"}

    states = {"红利": {"stance": "strong"}}
    _P().generate_investment_advice_three_stage(
        bloggers=[], predictions=[{"prediction_id": 1}], viewpoints=[],
        sector_states=states,
    )
    assert seen["sector_states"] == states  # 原样传，中途不许换/丢


# ───────────────────────── 路由：透传 + 附挂 ─────────────────────────

def test_route_forwards_sector_states_to_analyzer(test_db, monkeypatch):
    """证据里的 sector_states 必须原样送到三阶段调用（漏传 ⇒ 闸在链路上等于没装）。"""
    from src.api.routes import advice as advice_route
    from src.services.advice_service import AdviceService

    states = {"红利": {"stance": "strong", "r20": 4.2}}
    evidence = {
        "bloggers": [], "predictions": [{"prediction_id": 1, "sector": "红利"}],
        "viewpoints": [], "funds": [], "market_state": {}, "sector_states": states,
        "evidence_hash": "eh", "meta": {},
    }
    calls = []

    class _Fake:
        def generate_investment_advice_three_stage(self, bloggers, predictions,
                                                   viewpoints=None, market_state=None,
                                                   correction_note=None, use_cache=True,
                                                   sector_states=None):
            calls.append(sector_states)
            return {
                "advice_type": "hold", "advice_content": "观望为主",
                "market_sentiment": "neutral", "confidence": 50,
                "position": {"follow_gate": True, "position_label": "三成仓", "override_reason": ""},
                "sector_actions": [{"sector": "红利", "action": "hold", "confidence": 60,
                                    "reason": "持有", "horizon": "mid"}],
                "viewpoint_summary": {"summary": "ok", "_stage_status": "ok"},
                "prediction_analysis": {"summary": "ok", "_stage_status": "ok"},
                "_stage_statuses": {"stage1_viewpoints": "ok", "stage2_predictions": "ok",
                                    "stage3_advice": "ok"},
            }

    monkeypatch.setattr(AdviceService, "get_data_for_advice", lambda self: evidence)
    monkeypatch.setattr(advice_route, "get_analyzer", lambda *a, **k: _Fake())
    monkeypatch.setattr(advice_route, "current_as_of", lambda: date(2026, 3, 15))

    resp = advice_route.generate_advice(
        request=advice_route.GenerateAdviceRequest(force=False), db=test_db)
    assert resp["success"] is True
    assert calls == [states]


def test_attach_sector_targets_carries_own_state():
    """附挂标的时把板块自身强弱一并挂上（页面"敢买的依据"就是这份字段）。"""
    from src.api.routes.advice import _attach_sector_fund_targets

    states = {"红利": {"stance": "strong", "stance_label": "强势", "r20": 4.2,
                       "nav_as_of": "2026-10-13", "fund_code": "510880"}}
    actions = [{"sector": "红利"}, {"sector": "火星"}]
    out = _attach_sector_fund_targets(actions, states)
    assert out[0]["sector_stance"] == "strong"
    assert out[0]["sector_stance_label"] == "强势"
    assert out[0]["sector_r20"] == 4.2
    assert "sector_stance" not in out[1]  # 没有数据的板块不硬塞


def test_advice_page_renders_sector_state():
    """前端：板块操作行必须把自身强弱摆出来（豁免是大盘弱时最容易引疑问的动作）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    assert "a.sector_stance_label" in html
    assert "'sector-state-' + a.sector_stance" in html
