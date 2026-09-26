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


def _身份门(monkeypatch, verdict=(None, None)):
    """把"这码是不是基金"那把尺子换成桩，并记录它被问了几次、问的是谁。

    必须打在**模块属性**上：`llm_analyzer` 是在函数体里 `from … import`，
    调用时才取那个属性（打在别处会桩了个没人读的名字）。
    """
    import src.services.sector_fund_service as sfs
    asked = []

    def fake(code, name, sector):
        asked.append((code, name, sector))
        return verdict

    monkeypatch.setattr(sfs, '_manual_identity_verdict', fake)
    return asked


def test_save_fund_mapping_does_not_create_duplicate_active_mapping(test_db, monkeypatch):
    _身份门(monkeypatch)
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


def test_the_llm_mapping_gate_asks_whether_the_code_is_a_fund_at_all(test_db, monkeypatch):
    """S6 那批垃圾档案的**来路**就在这条自动建映射的路上。

    LLM 会抽出「秦安股份 → 603758」「泉阳泉 → 600189」这类 A 股 / 债券代码，
    旧代码一律 `ensure_fund_info_exists` + 建映射 ⇒ 机器自己把股票名当基金写进库里，
    生产因此攒下 6 个"活预测 0、净值 0"的垃圾基金档案（老板让删的那批）。
    现在建之前先问身份门：判"根本不是基金"就不建档、不建映射。
    """
    from src.models.database import FundInfo
    analyzer = object.__new__(LLMAnalyzer)

    asked = _身份门(monkeypatch, ('这只标的在基金域里查不到，实为股票「秦安股份」',
                                 {'verdict': 'not_a_fund'}))
    assert analyzer._save_fund_mapping("秦安股份", "603758", "秦安股份",
                                       reviewed=False, db=test_db) is None
    assert asked == [('603758', '秦安股份', '秦安股份')], '身份门没被问 ⇒ 闸是空的'
    assert test_db.query(SectorFundMapping).filter_by(sector_name='秦安股份').count() == 0, \
        '判"不是基金"却还是建了映射'
    assert test_db.query(FundInfo).filter_by(fund_code='603758').count() == 0, \
        '判"不是基金"却还是补了基金档案 ⇒ 垃圾码又回来了'

    # 反向对照：门说"没意见"（unknown / 探针不可用）时必须照旧放行，
    # 否则一次网络抖动就能让所有新板块建不了映射。
    _身份门(monkeypatch, (None, None))
    analyzer._save_fund_mapping("医药", "001001", "医药基金", reviewed=False, db=test_db)
    assert test_db.query(SectorFundMapping).filter_by(sector_name='医药').count() == 1, \
        '身份门把正常写入也挡了 ⇒ 它建成了墙'


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
