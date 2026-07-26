import asyncio
from datetime import date

import pytest
from fastapi import BackgroundTasks
from fastapi import HTTPException

from src.models.database import BatchAnalysisTask, Blogger, Post
from src.api.routes import posts as posts_route
from src.api.routes.posts import PostAnalysisJobRequest, PostCreate
from src.services.post_analysis_service import PostAnalysisService


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


def _add_unanalyzed_post(db):
    blogger = Blogger(name="路由任务博主", platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        content="我看好人工智能板块未来一周继续上涨，资金和市场趋势都比较积极。",
        post_date=date(2026, 7, 10),
        analyzed=False,
    )
    db.add(post)
    db.commit()
    return post


def test_single_post_analysis_route_creates_persistent_job(test_db):
    post = _add_unanalyzed_post(test_db)
    background_tasks = BackgroundTasks()

    response = asyncio.run(posts_route.analyze_post(
        post_id=post.id,
        background_tasks=background_tasks,
        db=test_db,
    ))

    task = test_db.query(BatchAnalysisTask).one()
    assert response["success"] is True
    assert response["data"]["task_id"] == task.id
    assert task.task_params["post_ids"] == [post.id]
    assert len(background_tasks.tasks) == 1


def test_legacy_batch_analysis_and_status_use_persistent_task(test_db):
    post = _add_unanalyzed_post(test_db)
    background_tasks = BackgroundTasks()

    started = asyncio.run(posts_route.batch_analyze_posts(
        background_tasks=background_tasks,
        db=test_db,
    ))
    status = asyncio.run(posts_route.get_batch_analyze_status(db=test_db))

    assert started["data"]["task_id"] == status["data"]["task_id"]
    assert started["data"]["total"] == 1
    assert status["data"]["status"] == "pending"
    assert status["data"]["in_progress"] is True
    assert status["data"]["total_count"] == 1


def test_existing_pending_job_is_rescheduled_after_render_restart(test_db):
    post = _add_unanalyzed_post(test_db)
    task, _ = PostAnalysisService.create_job(test_db, post_ids=[post.id])
    background_tasks = BackgroundTasks()

    response = asyncio.run(posts_route.start_analysis_job(
        payload=PostAnalysisJobRequest(post_ids=[post.id]),
        background_tasks=background_tasks,
        db=test_db,
    ))

    assert response["data"]["task_id"] == task.id
    assert len(background_tasks.tasks) == 1


def test_explicit_job_does_not_claim_unrelated_active_task(test_db):
    first = _add_unanalyzed_post(test_db)
    second = Post(
        blogger_id=first.blogger_id,
        content="第二篇帖子也有足够长的基金投资分析正文，应该等待当前任务完成后再加入队列。",
        post_date=date(2026, 7, 11),
        analyzed=False,
    )
    test_db.add(second)
    test_db.commit()
    PostAnalysisService.create_job(test_db, post_ids=[first.id])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(posts_route.start_analysis_job(
            payload=PostAnalysisJobRequest(post_ids=[second.id]),
            background_tasks=BackgroundTasks(),
            db=test_db,
        ))

    assert exc_info.value.status_code == 409
