"""analyze_post 的重试不得内外两层相乘（否则批量分析最坏 9 次调用）。"""
import json

from src.analyzer.llm_analyzer import AnalysisResultCache, LLMAnalyzer


def _bare_analyzer(monkeypatch):
    analyzer = object.__new__(LLMAnalyzer)
    analyzer.result_cache = AnalysisResultCache()
    analyzer._call_stats = {"cache_hits": 0}
    monkeypatch.setattr(
        analyzer,
        "_get_period_with_confidence",
        lambda full_text, parsed_date: (7, "1周", "test", "medium", ""),
    )
    monkeypatch.setattr(analyzer, "_build_time_context_simplified", lambda *a, **k: "")
    monkeypatch.setattr(analyzer, "_get_jargon_guide", lambda: "")
    monkeypatch.setattr(analyzer, "_normalize_prediction_periods", lambda result: result)
    monkeypatch.setattr(analyzer, "_fill_fund_from_sector", lambda result: None)
    return analyzer


def test_analyze_post_delegates_retries_to_outer_loop(monkeypatch):
    analyzer = _bare_analyzer(monkeypatch)
    calls = []

    def fake_call_llm(prompt, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            {"predictions": [], "viewpoint": {}, "summary": "ok"}, ensure_ascii=False
        )

    monkeypatch.setattr(analyzer, "_call_llm", fake_call_llm)
    monkeypatch.setattr(analyzer, "_parse_json_with_fallback", lambda text: json.loads(text))

    analyzer.analyze_post("标题", "内容 医药短期看涨", post_date="2026-07-10")

    assert len(calls) == 1
    # 内层只试一次：重试由 analyze_post 的外层循环统一负责
    assert calls[0].get("retry_count") == 1


def test_analyze_post_outer_loop_still_retries_on_parse_failure(monkeypatch):
    analyzer = _bare_analyzer(monkeypatch)
    monkeypatch.setattr("src.analyzer.llm_analyzer.time.sleep", lambda *_: None)
    attempts = []

    def fake_call_llm(prompt, **kwargs):
        attempts.append(kwargs.get("retry_count"))
        # 前两次返回坏 JSON，第三次成功：外层循环应重试到成功
        if len(attempts) < 3:
            return "not-json"
        return json.dumps(
            {"predictions": [], "viewpoint": {}, "summary": "recovered"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(analyzer, "_call_llm", fake_call_llm)
    monkeypatch.setattr(
        analyzer,
        "_parse_json_with_fallback",
        lambda text: json.loads(text) if text.startswith("{") else None,
    )

    result = analyzer.analyze_post("标题", "内容", post_date="2026-07-10", retry_count=3)

    assert result["summary"] == "recovered"
    assert attempts == [1, 1, 1]
