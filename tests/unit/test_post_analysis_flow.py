from datetime import date, timedelta
from types import SimpleNamespace

from src.models.database import Blogger, Post, Prediction
from src.services import post_service as post_service_module
from src.services.post_service import PostService


class _FakeAnalyzer:
    def analyze_post(self, title, content, post_date=None):
        return {
            "predictions": [
                {
                    "sector": "人工智能",
                    "sector_type": "tech",
                    "prediction_type": "up",
                    "prediction_content": "看好人工智能板块",
                    "confidence": 80,
                    "prediction_period": "1周",
                }
            ],
            "summary": "看多人工智能",
        }

    def get_fund_for_sector(self, sector):
        return {"code": "015719", "name": "默认人工智能基金"}

    def calculate_target_date(self, prediction_date, prediction_period):
        return prediction_date + timedelta(days=7)

    def calculate_next_verify_date(self, prediction_date, target_date):
        return target_date


class _FakeFundAutoManager:
    def auto_add_fund_for_prediction(self, sector, db):
        return True, "使用用户审查映射", SimpleNamespace(
            fund_code="999999",
            fund_name="用户审查人工智能基金",
        )

    def get_category_for_sector(self, sector):
        return "tech"


def test_create_post_with_analysis_prefers_database_mapping_over_builtin_sector_map(monkeypatch, test_db):
    monkeypatch.setattr(post_service_module, "get_analyzer", lambda: _FakeAnalyzer())
    monkeypatch.setattr(
        "src.fund.fund_auto_manager.fund_auto_manager",
        _FakeFundAutoManager(),
    )

    blogger = Blogger(name="测试博主", platform="wechat")
    test_db.add(blogger)
    test_db.commit()

    result = PostService(test_db).create_post_with_analysis(
        blogger_id=blogger.id,
        content="我继续看好人工智能板块，接下来一周可能继续上涨，资金流和市场趋势都比较积极。",
        post_date=date(2026, 7, 10),
        async_mode=False,
    )

    prediction = test_db.query(Prediction).one()

    assert result["success"] is True
    assert prediction.fund_code == "999999"
    assert prediction.fund_name == "用户审查人工智能基金"


def test_batch_analysis_skips_low_quality_post_without_deleting_it(monkeypatch, test_db):
    """批量分析只能跳过低质量帖子，不能绕过清理保护直接删除资料。"""
    from sqlalchemy.orm import sessionmaker

    blogger = Blogger(name="低质量帖子博主", platform="wechat")
    test_db.add(blogger)
    test_db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="",
        content="hi",
        post_date=date(2026, 7, 10),
    )
    test_db.add(post)
    test_db.commit()
    post_id = post.id

    monkeypatch.setattr(
        "src.models.database.SessionLocal",
        sessionmaker(bind=test_db.get_bind()),
    )

    result = PostService(test_db).batch_analyze_posts()

    preserved = test_db.query(Post).filter(Post.id == post_id).one_or_none()
    assert preserved is not None
    assert preserved.analyzed is False
    assert result["deleted"] == 0
    assert result["skipped"] == 1


def test_fund_auto_manager_does_not_commit_an_external_session(monkeypatch, test_db):
    from src.fund.fund_auto_manager import FundAutoManager
    from src.fund.fund_api import fund_data_manager

    manager = FundAutoManager()
    monkeypatch.setattr(
        manager,
        "auto_fetch_fund_for_sector",
        lambda sector: {"code": "999998", "name": "事务测试基金"},
    )
    monkeypatch.setattr(
        "src.fund.fund_auto_manager.fund_api.get_fund_info",
        lambda code: {
            "fund_name": "事务测试基金",
            "fund_type": "指数型",
            "nav": 1.0,
            "nav_date": "2026-07-10",
            "day_growth": 0.1,
        },
    )
    monkeypatch.setattr(fund_data_manager, "update_fund_history", lambda fund_code, days, db: 0)

    commit_calls = []

    def forbidden_commit():
        commit_calls.append(True)
        raise AssertionError("外部事务不能由基金匹配提前提交")

    monkeypatch.setattr(test_db, "commit", forbidden_commit)

    success, _, fund = manager.auto_add_fund_for_prediction("人工智能", db=test_db)

    assert success is True
    assert fund.fund_code == "999998"
    assert commit_calls == []


def test_post_service_sync_creation_delegates_to_unified_analysis(monkeypatch, test_db):
    from src.services.post_analysis_service import PostAnalysisService

    blogger = Blogger(name="统一分析博主", platform="wechat")
    test_db.add(blogger)
    test_db.commit()
    analyzed_post_ids = []

    monkeypatch.setattr(
        PostAnalysisService,
        "analyze_post",
        lambda self, post_id, task_id=None: analyzed_post_ids.append(post_id) or {
            "success": True,
            "status": "succeeded",
            "message": "统一分析完成",
            "predictions_created": 2,
        },
    )
    monkeypatch.setattr(
        post_service_module,
        "get_analyzer",
        lambda: (_ for _ in ()).throw(AssertionError("不应再调用旧分析实现")),
    )

    result = PostService(test_db).create_post_with_analysis(
        blogger_id=blogger.id,
        content="我看好人工智能和半导体未来一周继续上涨，市场和资金趋势比较积极。",
        post_date=date(2026, 7, 10),
        async_mode=False,
    )

    assert analyzed_post_ids == [result["id"]]
    assert result["message"] == "统一分析完成"
    assert result["predictions_created"] == 2
