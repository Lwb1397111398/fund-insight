# -*- coding: utf-8 -*-
"""「生成今日投资建议」按北京日复用的路由判据（2026-10-13，老板点名修的）。

老板报的现象：今天点生成，屏上显示的还是昨天的结果。当场复现（同参数打生产）
拿到的回执是 `code=gate_conflict`——当天那条被质量闸拒了、没落库，
页面还挂着昨天的旧建议，看起来就像"生成=给昨天的"。本文件钉住修法后的语义：

① 当天（北京日）已经生成过 ⇒ 非 force 点击直接返回当天那条，一次 LLM 都不调；
② 昨天的记录哪怕 data_hash 与今天算出的完全相同 ⇒ 也不许复用（旧的证据哈希缓存
   跨天命中/当天复点误重算，都被这一条挡回）；当天没有 ⇒ 生成；
③ force（「重新生成」按钮）⇒ 当天已有也重算；
④ 新行的 advice_date 写北京日（current_as_of 是唯一时钟；容器 UTC 钟差一天的
   那一族，第 88 轮清了两处，这里是第三处）；
⑤ 契约违规（软闸/措辞）当场重试一次、重试必须带拒绝理由（correction_note）；
   结构性失败（阶段 failed）不重试——重调只会拿回同一份被拒输出；
⑥ 同日多条取 created_at 最新（重新生成后复用要能拿到最后那条）；
⑦ 前端：旧建议必须显式警示"不是今天的结果"、生成失败原因持久上页面。
"""
from datetime import date, datetime

import pytest

from src.models.database import InvestmentAdvice
from src.services.advice_service import AdviceService
from src.services.advice_validation import (
    ADVICE_PROMPT_VERSION,
    advice_cache_digest,
    build_advice_cache_key,
)
from src.api.routes import advice as advice_route

FIXED_TODAY = date(2026, 3, 15)  # 故意不等于真实北京日：问错钟的写法当场露馅


def _evidence_hash_stub() -> str:
    return "eh-" + "0" * 58


def _ok_output(**overrides):
    """一份能过 validate_advice_output 的 stage3 输出（stance 缺省 ⇒ 软闸不生效）。"""
    base = {
        "advice_type": "hold",
        "advice_content": "大盘震荡，建议观望为主，持有白酒",
        "market_sentiment": "neutral",
        "confidence": 62,
        "reasoning": "两位A级博主中性偏多",
        "position": {
            "follow_gate": True,
            "position_label": "维持现有仓位",
            "override_reason": "",
        },
        "sector_actions": [
            {"sector": "白酒", "action": "hold", "confidence": 60,
             "reason": "A级博主中性偏多", "horizon": "mid"}
        ],
        "suggested_sectors": ["白酒"],
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
    base.update(overrides)
    return base


class _CountingAnalyzer:
    """假三阶段：记录每次调用（含 correction_note/use_cache/sector_states），按预置队列返回。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate_investment_advice_three_stage(self, bloggers, predictions,
                                               viewpoints=None, market_state=None,
                                               correction_note=None, use_cache=True,
                                               sector_states=None):
        self.calls.append({"correction_note": correction_note, "use_cache": use_cache,
                           "sector_states": sector_states})
        if self.outputs:
            return self.outputs.pop(0)
        raise AssertionError("预置输出用完了——LLM 被多调了")


def _wire(monkeypatch, analyzer, market_state=None):
    """把路由的时钟、证据、分析器都换成可控桩；DB 用真表（test_db）。"""
    evidence = {
        "bloggers": [{"name": "测试博主", "accuracy_rate": 70.0, "grade": "A",
                      "total_predictions": 5, "correct_predictions": 4}],
        "predictions": [{"prediction_id": 1, "blogger_id": 1, "blogger_name": "测试博主",
                         "sector": "白酒", "prediction_type": "up",
                         "prediction_content": "白酒企稳", "confidence": 65,
                         "days_to_target": 3, "weight": 0.4}],
        "viewpoints": [{"viewpoint_id": 1, "summary": "白酒情绪修复"}],
        "funds": [],
        "market_state": market_state or {},
        "evidence_hash": _evidence_hash_stub(),
        "meta": {},
    }
    monkeypatch.setattr(AdviceService, "get_data_for_advice", lambda self: evidence)
    monkeypatch.setattr(advice_route, "get_analyzer", lambda *a, **k: analyzer)
    monkeypatch.setattr(advice_route, "current_as_of", lambda: FIXED_TODAY)


def _post(db, force=False):
    req = advice_route.GenerateAdviceRequest(force=force)
    return advice_route.generate_advice(request=req, db=db)


def _seed(db, advice_date, created_at=None, data_hash="other-hash", content="旧内容"):
    row = InvestmentAdvice(
        advice_date=advice_date,
        advice_type="hold",
        advice_content=content,
        market_sentiment="neutral",
        confidence=60,
        data_hash=data_hash,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    return row


def test_same_day_second_click_returns_today_first_without_llm(test_db, monkeypatch):
    """①：当天已有记录 ⇒ 非 force 直接复用当天首条，一次 LLM 都不调。"""
    analyzer = _CountingAnalyzer([_ok_output()])
    _wire(monkeypatch, analyzer)
    _seed(test_db, FIXED_TODAY, content="今日首条")

    resp = _post(test_db, force=False)
    assert resp["success"] is True
    assert resp["data"]["cache_hit"] is True and resp["data"]["is_new"] is False
    assert resp["data"]["advice_content"] == "今日首条"
    assert analyzer.calls == []  # 复用路径不许打 LLM
    assert test_db.query(InvestmentAdvice).count() == 1  # 复用不落新行


def test_yesterday_record_is_never_reused_even_if_hash_matches(test_db, monkeypatch):
    """②：昨天的记录哪怕 data_hash 与今天算出的完全相同也不许复用——
    旧实现按证据哈希缓存，这一族回归（跨天复用/当天复点误重算）当场红。"""
    same_hash = advice_cache_digest(
        build_advice_cache_key(
            _evidence_hash_stub(),
            prompt_version=ADVICE_PROMPT_VERSION,
            model_version=advice_route._model_version(),
        )
    )
    analyzer = _CountingAnalyzer([_ok_output()])
    _wire(monkeypatch, analyzer)
    _seed(test_db, date(2026, 3, 14), data_hash=same_hash, content="昨日旧条")

    resp = _post(test_db, force=False)
    assert resp["success"] is True
    assert analyzer.calls and len(analyzer.calls) == 1  # 当天没有 ⇒ 必须生成
    assert resp["data"]["is_new"] is True
    assert resp["data"]["advice_content"] != "昨日旧条"
    assert test_db.query(InvestmentAdvice).count() == 2


def test_regenerate_forces_even_when_today_exists(test_db, monkeypatch):
    """③：「重新生成」（force=true）⇒ 当天已有也重算落新行。"""
    analyzer = _CountingAnalyzer([_ok_output()])
    _wire(monkeypatch, analyzer)
    _seed(test_db, FIXED_TODAY, content="今日首条")

    resp = _post(test_db, force=True)
    assert resp["success"] is True and resp["data"]["is_new"] is True
    assert len(analyzer.calls) == 1
    assert test_db.query(InvestmentAdvice).count() == 2


def test_force_passes_use_cache_off_to_three_stage(test_db, monkeypatch):
    """③'：「重新生成」必须把 use_cache=False 传到三阶段（镜像 2026-10-13 实测：
    不绕缓存时 force 复点 0.1 秒回放同一份输出再落一条——"重新生成"名不副实）。
    非 force 路径照常走缓存（省 token，缓存命中≠错误）。"""
    analyzer = _CountingAnalyzer([_ok_output(), _ok_output()])
    _wire(monkeypatch, analyzer)

    _post(test_db, force=False)
    assert analyzer.calls[-1]["use_cache"] is True  # 普通生成照常吃缓存
    _post(test_db, force=True)
    assert analyzer.calls[-1]["use_cache"] is False  # 重新生成必须绕开


def test_three_stage_propagates_use_cache_to_every_stage():
    """③''：use_cache=False 落到**每一段**的 _call_llm——只关一段，
    另两段仍回放旧结果，"重新生成"半新半旧，比全旧更难排查。"""
    from src.analyzer.llm_analyzer import LLMAnalyzer

    seen = []

    class _Probe(LLMAnalyzer):
        def __init__(self):
            pass

        def _call_llm(self, prompt, **kwargs):
            seen.append(kwargs.get("use_cache"))
            return "{}"

    _Probe().generate_investment_advice_three_stage(
        bloggers=[{"name": "x", "accuracy_rate": 60, "grade": "B"}],
        predictions=[{"prediction_id": 1, "sector": "白酒", "prediction_type": "up",
                      "blogger_grade": "B", "confidence": 60, "days_to_target": 3,
                      "prediction_content": "涨", "blogger_name": "x"}],
        viewpoints=[{"viewpoint_id": 1, "summary": "看多", "author": "x",
                    "market_direction": "up", "confidence": 60, "credibility_score": 60,
                    "weight": 1.0, "sectors_bullish": [], "sectors_bearish": [],
                    "reasoning": "", "is_summary": False, "viewpoint_date": "2026-03-14",
                    "source": "t"}],
        use_cache=False,
    )
    assert seen and all(v is False for v in seen), f"有三段没绕缓存: {seen}"


def test_new_advice_stamped_with_beijing_date(test_db, monkeypatch):
    """④：落库行的 advice_date 是钉住的北京日，不是容器墙钟日。"""
    analyzer = _CountingAnalyzer([_ok_output()])
    _wire(monkeypatch, analyzer)

    resp = _post(test_db, force=False)
    assert resp["success"] is True
    assert resp["data"]["advice_date"] == FIXED_TODAY.isoformat()
    row = test_db.query(InvestmentAdvice).order_by(InvestmentAdvice.id.desc()).first()
    assert row.advice_date == FIXED_TODAY


def test_service_default_advice_date_uses_beijing_clock(test_db, monkeypatch):
    """④'：服务层缺省同样走 current_as_of（防未来新调用方又拿到 UTC 墙钟日）。"""
    monkeypatch.setattr("src.services.advice_service.current_as_of", lambda: FIXED_TODAY)
    service = AdviceService(test_db)
    result = service.create_advice(
        advice_type="hold", advice_content="测试", market_sentiment="neutral",
        confidence=50, referenced_bloggers=[], data_hash="h",
    )
    assert result["advice_date"] == FIXED_TODAY.isoformat()


def test_contract_violation_retries_once_with_reason_injected(test_db, monkeypatch):
    """⑤：软闸矛盾（gate_conflict）⇒ 当场重试一次，且重试必须带上拒绝理由
    （理由进 stage3 提示词、提示词一变缓存键才变、才真能重新采样）。"""
    weak = {"stance": "weak", "enforce": True, "index_code": "510300",
            "position_label": "弱势减仓不新开仓", "basis": "510300 近20日 -4%"}
    violation = _ok_output(
        advice_type="buy",
        advice_content="大盘虽弱但半导体确定性强，建议买入半导体",
        position={"follow_gate": True, "position_label": "维持仓位", "override_reason": ""},
        sector_actions=[{"sector": "半导体", "action": "buy", "confidence": 70,
                         "reason": "博主看涨", "horizon": "short"}],
    )
    analyzer = _CountingAnalyzer([violation, _ok_output()])
    _wire(monkeypatch, analyzer, market_state=weak)

    resp = _post(test_db, force=False)
    assert resp["success"] is True and resp["data"]["is_new"] is True
    assert len(analyzer.calls) == 2  # 违规一次 + 自纠错一次
    assert analyzer.calls[0]["correction_note"] is None
    note = analyzer.calls[1]["correction_note"]
    assert note and "买入" in note  # 拒绝理由逐字进了重试指令


def test_structural_failure_is_not_retried(test_db, monkeypatch):
    """⑤'：阶段 failed / 坏 JSON 属结构性失败 ⇒ 不重试（fail-closed 老规矩），
    回执 success:false + 原因，不落库。"""
    broken = _ok_output(_stage_statuses={
        "stage1_viewpoints": "ok",
        "stage2_predictions": "failed",
        "stage3_advice": "ok",
    }, _stage_status="failed")
    analyzer = _CountingAnalyzer([broken])  # 只预置一份：多调一次就 AssertionError
    _wire(monkeypatch, analyzer)

    resp = _post(test_db, force=False)
    assert resp["success"] is False
    assert resp["code"] == "stage_failed"
    assert test_db.query(InvestmentAdvice).count() == 0


def test_same_day_records_return_newest_by_created_at(test_db):
    """⑥：同日两条（重新生成过）⇒ get_latest 拿 created_at 新的那条。"""
    _seed(test_db, FIXED_TODAY, created_at=datetime(2026, 3, 15, 9, 0, 0), content="上午那条")
    _seed(test_db, FIXED_TODAY, created_at=datetime(2026, 3, 15, 15, 0, 0), content="下午那条")
    service = AdviceService(test_db)
    got = service.get_latest_advice()
    assert got["advice_content"] == "下午那条"


def test_advice_view_warns_stale_and_persists_failure(test_db=None):
    """⑦：前端——①屏上建议不是当天 ⇒ 顶部警示"不是今天的结果"（这次老板就是被
    昨天挂着的那条误导）；②生成失败/被拒的原因持久写入页面（adviceGenError），
    不能只靠一闪而过的 alert。"""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    # 旧建议警示：由 advice_date 与当天比较驱动
    assert "adviceIsStale" in html and "advice.advice_date" in html
    assert "advice-gen-error stale" in html
    assert "不是今天的结果" in html
    # 失败原因持久化
    assert "const adviceGenError = ref('')" in html
    assert "adviceGenError.value = '这次没有生成建议：'" in html
    assert "adviceGenError.value = '生成失败：'" in html
    # 空态按钮说清"当天首点"的语义
    assert "生成今日投资建议" in html
