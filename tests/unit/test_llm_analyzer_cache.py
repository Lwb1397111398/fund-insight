import json

from src.analyzer.llm_analyzer import AnalysisResultCache, LLMAnalyzer
from src.models.database import SectorFundMapping


def test_analyze_post_cache_key_uses_full_content(monkeypatch):
    analyzer = object.__new__(LLMAnalyzer)
    analyzer.result_cache = AnalysisResultCache()
    analyzer._call_stats = {"cache_hits": 0}

    monkeypatch.setattr(
        analyzer,
        "_get_period_with_confidence",
        lambda full_text, parsed_date: (7, "1周", "test", "medium", ""),
    )
    monkeypatch.setattr(
        analyzer,
        "_build_time_context_simplified",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(analyzer, "_get_jargon_guide", lambda: "")
    monkeypatch.setattr(analyzer, "_normalize_prediction_periods", lambda result: result)
    monkeypatch.setattr(analyzer, "_fill_fund_from_sector", lambda result: None)

    llm_calls = []
    responses = [
        {
            "predictions": [{"sector": "医药", "prediction_type": "up", "confidence": 70}],
            "viewpoint": {},
            "summary": "first",
        },
        {
            "predictions": [{"sector": "白酒", "prediction_type": "down", "confidence": 65}],
            "viewpoint": {},
            "summary": "second",
        },
    ]

    def fake_call_llm(prompt, **kwargs):
        llm_calls.append(prompt)
        return json.dumps(responses[len(llm_calls) - 1], ensure_ascii=False)

    monkeypatch.setattr(analyzer, "_call_llm", fake_call_llm)
    monkeypatch.setattr(analyzer, "_parse_json_with_fallback", lambda text: json.loads(text))

    shared_prefix = "A" * 200
    first = analyzer.analyze_post(
        "same-title",
        shared_prefix + " 医药短期看涨",
        post_date="2026-07-10",
        retry_count=1,
        use_cache=True,
    )
    second = analyzer.analyze_post(
        "same-title",
        shared_prefix + " 白酒短期看跌",
        post_date="2026-07-10",
        retry_count=1,
        use_cache=True,
    )

    assert first["summary"] == "first"
    assert second["summary"] == "second"
    assert len(llm_calls) == 2
