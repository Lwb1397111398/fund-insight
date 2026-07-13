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


def test_save_fund_mapping_does_not_create_duplicate_active_mapping(test_db):
    analyzer = object.__new__(LLMAnalyzer)

    analyzer._save_fund_mapping("医药", "001001", "医药基金", reviewed=False, db=test_db)
    analyzer._save_fund_mapping("医药", "001001", "医药基金", reviewed=False, db=test_db)

    mappings = test_db.query(SectorFundMapping).filter(
        SectorFundMapping.sector_name == "医药",
        SectorFundMapping.is_active == True,
    ).all()

    assert len(mappings) == 1
    assert mappings[0].fund_code == "001001"
    assert mappings[0].reviewed is False


def test_save_fund_mapping_does_not_override_reviewed_mapping(test_db):
    analyzer = object.__new__(LLMAnalyzer)
    reviewed = SectorFundMapping(
        sector_name="白酒",
        fund_code="161725",
        fund_name="人工审核白酒基金",
        reviewed=True,
        is_active=True,
    )
    test_db.add(reviewed)
    test_db.commit()

    analyzer._save_fund_mapping("白酒", "009999", "自动匹配基金", reviewed=False, db=test_db)

    mappings = test_db.query(SectorFundMapping).filter(
        SectorFundMapping.sector_name == "白酒",
        SectorFundMapping.is_active == True,
    ).all()

    assert len(mappings) == 1
    assert mappings[0].fund_code == "161725"
    assert mappings[0].fund_name == "人工审核白酒基金"
    assert mappings[0].reviewed is True
