from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models.database import (
    Base,
    Blogger,
    FundHistory,
    FundInfo,
    InvestmentAdvice,
    Post,
    Prediction,
    SectorAlias,
    SectorFundMapping,
    Viewpoint,
)
from src.services.data_portability_service import DataPortabilityService


def _export_v1_payload():
    return {
        "export_version": "1.0",
        "export_date": "2026-07-09T08:42:13.554263",
        "bloggers": [
            {
                "id": 1,
                "name": "测试博主",
                "platform": "wechat",
                "description": "用于导入导出测试",
                "accuracy_rate": 50.0,
                "total_predictions": 1,
                "correct_predictions": 0,
                "total_verify_score": 0,
                "grade": "C",
                "ultra_short_accuracy": 0.0,
                "ultra_short_total": 0,
                "ultra_short_correct": 0,
                "sector_coverage": 1,
                "avg_prediction_period": 7.0,
                "risk_warning_count": 0,
                "last_prediction_date": "2026-07-09",
                "prediction_frequency": 1.0,
                "is_active": True,
                "created_at": "2026-07-09T08:42:13.554263",
                "updated_at": "2026-07-09T08:42:13.554263",
            }
        ],
        "posts": [
            {
                "id": 1,
                "blogger_id": 1,
                "title": "测试帖子",
                "content": "看好人工智能方向",
                "post_date": "2026-07-09",
                "source_url": "https://example.test/post/1",
                "analyzed": True,
                "analysis_result": {"summary": "看多"},
                "auto_titled": True,
                "created_at": "2026-07-09T08:43:13.554263",
            }
        ],
        "predictions": [
            {
                "id": 1,
                "post_id": 1,
                "blogger_id": 1,
                "fund_code": "000001",
                "fund_name": "测试基金",
                "sector": "人工智能",
                "sector_type": "tech",
                "prediction_type": "bullish",
                "prediction_content": "短期看涨",
                "confidence": 80,
                "prediction_date": "2026-07-09",
                "prediction_period": "1周",
                "target_date": "2026-07-16",
                "status": "pending",
                "verify_history": [{"date": "2026-07-10", "score": 0}],
                "created_at": "2026-07-09T08:44:13.554263",
            }
        ],
        "viewpoints": [
            {
                "id": 1,
                "blogger_id": 1,
                "post_id": 1,
                "fund_code": "000001",
                "fund_name": "测试基金",
                "content": "继续观察人工智能",
                "author": "测试博主",
                "source": "manual",
                "article_id": "article-1",
                "article_url": "https://example.test/article/1",
                "content_hash": "hash-1",
                "market_direction": "bullish",
                "confidence": 75,
                "sectors_bullish": ["人工智能"],
                "sectors_bearish": [],
                "reasoning": "资金流较好",
                "summary": "偏乐观",
                "time_horizon": "short",
                "validity_period": 7,
                "valid_until": "2026-07-16",
                "viewpoint_date": "2026-07-09",
                "created_at": "2026-07-09T08:45:13.554263",
            }
        ],
        "fund_info": [
            {
                "id": 1,
                "fund_code": "000001",
                "fund_name": "测试基金",
                "fund_type": "混合型",
                "sector_type": "tech",
                "latest_nav": 1.2345,
                "nav_date": "2026-07-09",
                "estimated_nav_time": "2026-07-09T14:30:00",
                "actual_nav_time": "2026-07-09T15:00:00",
                "can_delete": False,
                "is_core_fund": True,
                "updated_at": "2026-07-09T15:01:00",
            }
        ],
        "fund_history": [
            {
                "id": 1,
                "fund_code": "000001",
                "fund_name": "测试基金",
                "nav_date": "2026-07-09",
                "nav": 1.2345,
                "day_growth": 1.2,
                "data_quality": "normal",
                "quality_note": "测试",
                "created_at": "2026-07-09T15:02:00",
            }
        ],
        "sector_alias": [
            {
                "id": 1,
                "alias_name": "AI",
                "sector_name": "人工智能",
                "created_at": "2026-07-09T15:03:00",
            }
        ],
        "sector_fund_mapping": [
            {
                "id": 1,
                "sector_name": "人工智能",
                "fund_code": "000001",
                "fund_name": "测试基金",
                "keywords": ["AI", "人工智能"],
                "is_active": True,
                "reviewed": True,
                "created_at": "2026-07-09T15:04:00",
                "updated_at": "2026-07-09T15:04:00",
            }
        ],
        "investment_advice": [
            {
                "id": 1,
                "advice_date": "2026-07-09",
                "advice_type": "hold",
                "advice_content": "保持观察",
                "reasoning": "数据支持有限",
                "risk_warning": "不构成投资建议",
                "suggested_sectors": ["人工智能"],
                "avoid_sectors": [],
                "short_term_advice": {"action": "watch"},
                "mid_term_advice": {"action": "hold"},
                "avoid_reasoning": "无",
                "referenced_bloggers": [1],
                "referenced_predictions": [1],
                "market_sentiment": "neutral",
                "confidence": 60,
                "data_hash": "advice-hash-1",
                "created_at": "2026-07-09T15:05:00",
            }
        ],
        "summary": {
            "bloggers": 1,
            "posts": 1,
            "predictions": 1,
            "viewpoints": 1,
            "fund_info": 1,
            "fund_history": 1,
            "sector_alias": 1,
            "sector_fund_mapping": 1,
            "investment_advice": 1,
        },
    }


def test_import_export_v1_payload_restores_all_exported_sections(test_db):
    result = DataPortabilityService(test_db).import_data(_export_v1_payload())

    assert result["success"] is True
    assert result["data"]["total_imported"] == 9
    assert result["data"]["total_skipped"] == 0
    assert test_db.query(Blogger).count() == 1
    assert test_db.query(Post).count() == 1
    assert test_db.query(Prediction).count() == 1
    assert test_db.query(Viewpoint).count() == 1
    assert test_db.query(FundInfo).count() == 1
    assert test_db.query(FundHistory).count() == 1
    assert test_db.query(SectorAlias).count() == 1
    assert test_db.query(SectorFundMapping).count() == 1
    assert test_db.query(InvestmentAdvice).count() == 1

    blogger = test_db.query(Blogger).one()
    prediction = test_db.query(Prediction).one()
    fund = test_db.query(FundInfo).one()
    advice = test_db.query(InvestmentAdvice).one()

    assert isinstance(blogger.created_at, datetime)
    assert blogger.last_prediction_date == date(2026, 7, 9)
    assert prediction.prediction_date == date(2026, 7, 9)
    assert prediction.verify_history == [{"date": "2026-07-10", "score": 0}]
    assert fund.nav_date == date(2026, 7, 9)
    assert isinstance(fund.estimated_nav_time, datetime)
    assert advice.suggested_sectors == ["人工智能"]


def test_import_export_v1_payload_is_idempotent(test_db):
    service = DataPortabilityService(test_db)

    first = service.import_data(_export_v1_payload())
    second = service.import_data(_export_v1_payload())

    assert first["success"] is True
    assert second["success"] is True
    assert second["data"]["total_imported"] == 0
    assert second["data"]["total_skipped"] == 9
    assert test_db.query(FundHistory).count() == 1
    assert test_db.query(InvestmentAdvice).count() == 1


def test_import_creates_recovery_fund_for_legacy_mapping_dependency(test_db):
    payload = _export_v1_payload()
    payload["sector_fund_mapping"][0]["fund_code"] = "legacy-mapping-fund"
    payload["sector_fund_mapping"][0]["fund_name"] = "Legacy Mapping Fund"

    result = DataPortabilityService(test_db).import_data(payload)

    assert result["success"] is True
    assert result["data"]["total_imported"] == 9
    assert result["data"]["created_dependencies"] == {"fund_info": 1}
    assert result["data"]["total_created_dependencies"] == 1
    assert result["data"]["warnings"]
    placeholder = test_db.query(FundInfo).filter_by(fund_code="legacy-mapping-fund").one()
    assert placeholder.fund_name == "Legacy Mapping Fund"
    assert placeholder.data_quality == "recovery_placeholder"
    assert placeholder.can_delete is False
    assert test_db.query(SectorFundMapping).count() == 1


def test_import_invalid_date_rolls_back_entire_payload(test_db):
    payload = _export_v1_payload()
    payload["posts"][0]["post_date"] = "not-a-date"

    result = DataPortabilityService(test_db).import_data(payload)

    assert result["success"] is False
    assert "post_date" in result["message"]
    assert result["data"]["total_imported"] == 0
    assert result["data"]["rolled_back"] is True
    assert result["data"]["total_rolled_back"] == 2
    assert test_db.query(Blogger).count() == 0
    assert test_db.query(Post).count() == 0


def test_config_import_export_routes_preserve_v1_contract(monkeypatch):
    """前端使用的备份接口应调用同一服务并返回兼容统计。"""
    monkeypatch.setenv("ACCESS_PASSWORD", "data_portability_test_password")

    from src.api.deps import get_db
    from src.api.main import app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        headers = {"X-Access-Password": "data_portability_test_password"}

        imported = client.post(
            "/api/config/import",
            headers=headers,
            json={"data": _export_v1_payload()},
        )

        assert imported.status_code == 200
        assert imported.json()["success"] is True
        assert imported.json()["data"]["total_imported"] == 9
        assert imported.json()["data"]["failed"]["fund_history"] == 0

        exported = client.get("/api/config/export", headers=headers)
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/json")
        exported_payload = exported.json()
        assert exported_payload["export_version"] == "1.0"
        assert len(exported_payload["fund_history"]) == 1
        assert len(exported_payload["investment_advice"]) == 1
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
