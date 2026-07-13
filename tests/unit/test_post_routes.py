import asyncio
from datetime import date

from src.api.routes import posts as posts_route
from src.api.routes.posts import PostCreate


def test_create_post_propagates_service_failure(monkeypatch):
    class FakePostService:
        def __init__(self, db):
            pass

        def create_post_with_analysis(self, **kwargs):
            return {
                "success": False,
                "message": "分析失败：LLM未能提取有效预测",
                "id": 1,
                "predictions_created": 0,
            }

    monkeypatch.setattr(posts_route, "PostService", FakePostService)

    response = asyncio.run(posts_route.create_post(
        PostCreate(
            blogger_id=1,
            content="这是一段足够长但没有明确预测的基金帖子内容",
            post_date=date(2026, 7, 10),
            async_mode=False,
        ),
        db=None,
    ))

    assert response["success"] is False
    assert response["message"] == "分析失败：LLM未能提取有效预测"
    assert response["data"]["success"] is False
