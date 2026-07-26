import asyncio
from datetime import date

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from src.api.routes import posts as posts_routes
from src.api.schemas.post import PostUpdate
from src.models.database import (
    AnalysisLog,
    BatchAnalysisTask,
    Blogger,
    FundInfo,
    Post,
    Prediction,
    PredictionGroup,
    VerificationTask,
    Viewpoint,
)
from src.services.post_service import PostService


def _post(db, blogger, title, *, analyzed=False, analysis_result=None, day=10):
    value = Post(
        blogger_id=blogger.id,
        title=title,
        content=f"{title}：这是一段足够长的基金投资分析内容，用于测试搜索筛选分页和状态展示。",
        post_date=date(2026, 7, day),
        analyzed=analyzed,
        analysis_result=analysis_result,
    )
    db.add(value)
    db.flush()
    return value


def _prediction(db, post, fund_code="POST01", *, verified=True):
    value = Prediction(
        post_id=post.id,
        blogger_id=post.blogger_id,
        fund_code=fund_code,
        fund_name="帖子测试基金",
        prediction_type="up",
        prediction_content="未来一周上涨",
        prediction_date=post.post_date,
        prediction_period="1周",
        verify_count=1 if verified else 0,
        verify_score=80 if verified else 0,
        is_correct=True if verified else None,
        is_deleted=False,
    )
    db.add(value)
    db.flush()
    return value


def test_post_list_supports_filters_pagination_and_status_meta(test_db):
    blogger = Blogger(name="筛选博主", platform="wechat")
    other = Blogger(name="其他博主", platform="wechat")
    test_db.add_all([blogger, other])
    test_db.flush()
    analyzed = _post(test_db, blogger, "人工智能周报", analyzed=True, day=12)
    failed = _post(
        test_db,
        blogger,
        "人工智能复盘",
        analysis_result={"_meta": {"status": "failed", "error": "LLM错误"}},
        day=11,
    )
    _post(test_db, other, "消费观察", day=10)
    _prediction(test_db, analyzed, verified=False)
    test_db.commit()

    page = PostService(test_db).get_posts_page(
        skip=0,
        limit=1,
        keyword="人工智能",
        blogger_id=blogger.id,
        analysis_status="failed",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
    )

    assert page["meta"]["total"] == 1
    assert page["meta"]["status_counts"] == {
        "succeeded": 1,
        "failed": 1,
        "pending": 0,
        "running": 0,
        "skipped": 0,
    }
    assert page["data"][0]["id"] == failed.id
    assert page["data"][0]["analysis_status"] == "failed"
    assert page["data"][0]["prediction_count"] == 0


def test_post_list_can_exclude_low_quality_candidates(test_db):
    blogger = Blogger(name="质量筛选博主", platform="wechat")
    test_db.add(blogger)
    test_db.flush()
    normal = _post(test_db, blogger, "正常分析")
    low = Post(
        blogger_id=blogger.id,
        title="过短",
        content="hi",
        post_date=date(2026, 7, 10),
        analyzed=False,
    )
    test_db.add(low)
    test_db.commit()

    page = PostService(test_db).get_posts_page(quality="normal")

    assert [item["id"] for item in page["data"]] == [normal.id]


def test_post_edit_blocks_source_content_changes_after_predictions(test_db):
    blogger = Blogger(name="编辑博主", platform="wechat")
    test_db.add(blogger)
    test_db.flush()
    post = _post(test_db, blogger, "原始标题")
    _prediction(test_db, post, verified=False)
    test_db.commit()

    updated = PostService(test_db).update_post_fields(
        post.id,
        {"title": "修正标题", "source_url": "https://example.com/post"},
    )
    assert updated.title == "修正标题"
    assert updated.source_url == "https://example.com/post"

    with pytest.raises(ValueError, match="已有预测"):
        PostService(test_db).update_post_fields(post.id, {"content": "修改后的正文"})


def _seed_delete_graph(db):
    blogger = Blogger(
        name="删除博主",
        platform="wechat",
        total_predictions=1,
        correct_predictions=1,
        total_verify_score=80,
        accuracy_rate=80,
    )
    fund = FundInfo(
        fund_code="POST01",
        fund_name="帖子测试基金",
        active_predictions=1,
        can_delete=False,
    )
    db.add_all([blogger, fund])
    db.flush()
    post = _post(db, blogger, "待彻底删除帖子", analyzed=True)
    prediction = _prediction(db, post)
    verify_task = VerificationTask(
        prediction_id=prediction.id,
        task_date=date(2026, 7, 20),
    )
    group = PredictionGroup(
        blogger_id=blogger.id,
        fund_code=fund.fund_code,
        prediction_ids=[prediction.id],
        representative_id=prediction.id,
        prediction_count=1,
    )
    batch_task = BatchAnalysisTask(task_type="posts", status="succeeded")
    db.add(batch_task)
    db.flush()
    log = AnalysisLog(task_id=batch_task.id, post_id=post.id, parse_success=True)
    viewpoint = Viewpoint(
        blogger_id=blogger.id,
        post_id=post.id,
        content="保留的独立观点",
        viewpoint_date=post.post_date,
    )
    db.add_all([verify_task, group, log, viewpoint])
    db.commit()
    return blogger, fund, post, prediction, viewpoint


def test_delete_preview_and_permanent_delete_cover_all_dependencies(test_db):
    blogger, fund, post, _, viewpoint = _seed_delete_graph(test_db)
    fund_code = fund.fund_code
    service = PostService(test_db)

    preview = service.get_delete_preview(post.id)
    result = service.delete_post_permanently(post.id)

    assert preview["prediction_count"] == 1
    assert preview["verified_prediction_count"] == 1
    assert preview["verification_task_count"] == 1
    assert preview["prediction_group_count"] == 1
    assert preview["analysis_log_count"] == 1
    assert preview["viewpoint_detach_count"] == 1
    assert result["deleted_predictions"] == 1
    assert test_db.get(Post, post.id) is None
    assert test_db.query(Prediction).count() == 0
    assert test_db.query(VerificationTask).count() == 0
    assert test_db.query(PredictionGroup).count() == 0
    assert test_db.query(AnalysisLog).count() == 0
    test_db.expire_all()
    assert test_db.get(Viewpoint, viewpoint.id).post_id is None
    assert test_db.get(Blogger, blogger.id).total_predictions == 0
    saved_fund = test_db.query(FundInfo).filter(FundInfo.fund_code == fund_code).one()
    assert saved_fund.active_predictions == 0
    assert saved_fund.can_delete is True


def test_permanent_delete_rolls_back_everything_on_recalculation_failure(monkeypatch, test_db):
    _, _, post, prediction, _ = _seed_delete_graph(test_db)
    service = PostService(test_db)
    monkeypatch.setattr(
        service,
        "_recalculate_after_post_delete",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("重算失败")),
    )

    with pytest.raises(RuntimeError, match="重算失败"):
        service.delete_post_permanently(post.id)

    assert test_db.get(Post, post.id) is not None
    assert test_db.get(Prediction, prediction.id) is not None
    assert test_db.query(AnalysisLog).count() == 1


def _request_with_headers(headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "DELETE", "path": "/", "headers": raw_headers})


def test_delete_route_requires_confirmation_header(test_db):
    _, _, post, _, _ = _seed_delete_graph(test_db)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(posts_routes.delete_post(
            post_id=post.id,
            request=_request_with_headers(),
            db=test_db,
        ))

    assert exc.value.status_code == 403
    assert test_db.get(Post, post.id) is not None


