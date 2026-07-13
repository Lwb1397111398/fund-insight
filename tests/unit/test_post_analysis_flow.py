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
        content="我继续看好人工智能板块，接下来一周可能继续上涨。",
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
