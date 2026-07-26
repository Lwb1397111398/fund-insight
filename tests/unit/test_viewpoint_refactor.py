import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.routes import viewpoints as viewpoint_routes
from src.models.database import CrawlerArticleRecord, Viewpoint
from src.services.viewpoint_workflow_service import ViewpointWorkflowService


def _add_viewpoint(db, **overrides):
    values = {
        "content": "人工智能板块资金流改善，短期趋势偏强。",
        "author": "测试作者",
        "source": "eastmoney_blog",
        "viewpoint_date": date.today(),
        "market_direction": "bullish",
        "confidence": 75,
        "summary": "人工智能短期偏强",
        "reasoning": "【AI深度分析】资金与趋势形成共振",
        "sectors_bullish": ["人工智能"],
        "sectors_bearish": [],
        "valid_until": date.today() + timedelta(days=7),
        "is_deleted": False,
        "is_summary": False,
    }
    values.update(overrides)
    viewpoint = Viewpoint(**values)
    db.add(viewpoint)
    db.commit()
    db.refresh(viewpoint)
    return viewpoint


class _NonClosingSession:
    """让后台任务测试复用 fixture 会话，同时忽略 close。"""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass

def test_fetch_job_deduplicates_stable_articles_and_persists_progress(test_db):
    article = {
        "title": "同一篇文章",
        "content": "看好半导体板块，订单和资金面均有改善，短期景气度持续回升，资金流入明显放大，建议重点关注相关产业链龙头标的。",
        "author": "分析师",
        "url": "https://example.test/article/1",
        "publish_time": date.today().isoformat(),
    }
    task, created = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=10
    )
    assert created is True

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [article, dict(article)]},
    )

    assert test_db.query(Viewpoint).count() == 1
    assert test_db.query(CrawlerArticleRecord).count() == 1
    test_db.refresh(task)
    assert task.status == "succeeded"
    assert task.processed_count == 2
    assert task.success_count == 1
    assert task.result_summary["sources"]["sina_blog"]["duplicates"] == 1


def test_fetch_job_records_sina_blog_source_failure(test_db):
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: (_ for _ in ()).throw(RuntimeError("source down"))},
    )

    test_db.refresh(task)
    assert test_db.query(Viewpoint).count() == 0
    assert task.status == "failed"
    assert task.success_count == 0
    assert task.result_summary["sources"]["sina_blog"]["error"] == "source down"


def test_summary_is_atomic_idempotent_and_repoints_crawler_records(test_db):
    target = date.today() - timedelta(days=1)
    first = _add_viewpoint(test_db, viewpoint_date=target)
    second = _add_viewpoint(test_db, viewpoint_date=target, source="sina_finance")
    record = CrawlerArticleRecord(
        article_id="eastmoney_blog:summary-test",
        source="eastmoney_blog",
        is_adopted=True,
        viewpoint_id=first.id,
    )
    test_db.add(record)
    test_db.commit()

    result = ViewpointWorkflowService.summarize_date(
        test_db,
        target,
        summarizer=lambda rows, day: {
            "success": True,
            "content": "当日市场观点汇总",
            "market_direction": "bullish",
            "confidence": 72,
            "topics": [],
            "sectors_bullish": ["人工智能"],
            "sectors_bearish": [],
            "reasoning": "多来源形成偏多共识",
        },
    )

    assert result["deleted_originals"] == 2
    summary = test_db.query(Viewpoint).one()
    assert summary.is_summary is True
    assert summary.original_count == 2
    test_db.refresh(record)
    assert record.viewpoint_id == summary.id

    repeated = ViewpointWorkflowService.summarize_date(test_db, target, summarizer=lambda *_: pytest.fail())
    assert repeated["summary_id"] == summary.id
    assert repeated["already_summarized"] is True


def test_summary_failure_rolls_back_and_unanalyzed_rows_block_deletion(test_db):
    target = date.today() - timedelta(days=2)
    original = _add_viewpoint(test_db, viewpoint_date=target)

    with pytest.raises(RuntimeError):
        ViewpointWorkflowService.summarize_date(
            test_db,
            target,
            summarizer=lambda *_: (_ for _ in ()).throw(RuntimeError("LLM failed")),
        )
    assert test_db.get(Viewpoint, original.id) is not None
    assert test_db.query(Viewpoint).filter(Viewpoint.is_summary.is_(True)).count() == 0

    original.reasoning = None
    test_db.commit()
    with pytest.raises(ValueError, match="尚未完成深度分析"):
        ViewpointWorkflowService.summarize_date(test_db, target, summarizer=lambda *_: {})
    assert test_db.get(Viewpoint, original.id) is not None


class _Request:
    def __init__(self, headers):
        self.headers = headers

def test_viewpoint_list_is_paginated_filtered_and_uses_dynamic_expiry(test_db):
    _add_viewpoint(test_db, content="匹配关键词但已经过期", valid_until=date.today() - timedelta(days=1))
    _add_viewpoint(test_db, content="匹配关键词且有效", source="sina_finance")
    _add_viewpoint(test_db, content="不应出现", is_deleted=True)

    response = asyncio.run(viewpoint_routes.get_viewpoints(
        page=1,
        page_size=1,
        keyword="匹配关键词",
        source=None,
        market_direction=None,
        analysis_status=None,
        date_from=None,
        date_to=None,
        viewpoint_type=None,
        db=test_db,
    ))

    assert response["meta"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
    assert len(response["data"]) == 1
    assert "content" not in response["data"][0]
    assert response["data"][0]["is_expired"] is False
    assert "is_summary" in response["data"][0]

def test_viewpoint_detail_excludes_soft_deleted_rows(test_db):
    viewpoint = _add_viewpoint(test_db, is_deleted=True)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(viewpoint_routes.get_viewpoint_detail(viewpoint.id, db=test_db))

    assert exc_info.value.status_code == 404

def test_permanent_delete_requires_confirmation_and_detaches_crawler_record(test_db):
    viewpoint = _add_viewpoint(test_db)
    record = CrawlerArticleRecord(
        article_id="eastmoney_blog:delete-test",
        source="eastmoney_blog",
        title="待删除文章",
        is_adopted=True,
        viewpoint_id=viewpoint.id,
    )
    test_db.add(record)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(viewpoint_routes.delete_viewpoint(viewpoint.id, request=_Request({}), db=test_db))
    assert exc_info.value.status_code == 403

    response = asyncio.run(viewpoint_routes.delete_viewpoint(
        viewpoint.id,
        request=_Request({"x-danger-confirm": "delete-viewpoint"}),
        db=test_db,
    ))

    assert response["success"] is True
    assert test_db.get(Viewpoint, viewpoint.id) is None
    test_db.refresh(record)
    assert record.viewpoint_id is None
    assert record.is_adopted is False

def test_static_task_routes_are_not_shadowed_by_viewpoint_id_route():
    paths = [route.path for route in viewpoint_routes.router.routes]

    assert paths.index("/viewpoints/tasks/latest") < paths.index("/viewpoints/{viewpoint_id}")


def test_fetch_mode_does_not_invoke_llm_capture_or_deep_analyzer(test_db):
    """fetch 模式不调 AI，全部非重复文章直接入库 pending。"""
    article = {
        "title": "fetch模式文章",
        "content": "看好半导体板块景气持续改善，订单与资金面共振，短期趋势偏强，建议关注相关产业链龙头标的。",
        "author": "分析师",
        "url": "https://example.test/fetch/1",
        "publish_time": date.today().isoformat(),
    }
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )

    deep_calls = {"n": 0}

    def deep(item, source):
        deep_calls["n"] += 1
        return {"market_direction": "bullish", "confidence": 80, "summary": "x", "reasoning": "y"}

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [article]},
        deep_analyzer=deep,
    )

    test_db.refresh(task)
    vp = test_db.query(Viewpoint).one()
    assert deep_calls["n"] == 0             # fetch 模式不调 deep analysis
    assert vp.analysis_summary == "pending"  # 仅入库, 等一键AI分析
    assert task.status == "succeeded"
    assert task.success_count == 1


def test_deep_retries_analyzes_pending_viewpoints_and_marks_succeeded(test_db):
    """_run_deep_retries 应对 pending 观点逐个补深度分析并置 succeeded。"""
    raw = _add_viewpoint(
        test_db,
        reasoning=None,
        summary=None,
        market_direction=None,
        analysis_summary="pending",
    )
    task = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=1
    )[0]
    # 手动改成 batch 任务并清空计数, 模拟 batch-analyze 后台入口
    task.task_type = "viewpoint_batch"
    task.total_count = 0
    task.processed_count = 0
    task.success_count = 0
    test_db.commit()

    def deep(item, source):
        return {
            "market_direction": "bullish",
            "confidence": 70,
            "summary": "AI补分析",
            "reasoning": "理由",
            "time_horizon": "medium",
            "sectors_bullish": ["半导体"],
            "sectors_bearish": [],
            "analysis": "深度结论",
        }

    ViewpointWorkflowService._run_deep_retries(test_db, task, [raw.id], deep)

    test_db.refresh(raw)
    test_db.refresh(task)
    assert raw.analysis_summary == "succeeded"
    assert raw.market_direction == "bullish"
    assert "AI深度分析" in (raw.reasoning or "")
    assert task.status == "succeeded"
    assert task.success_count == 1
    assert task.processed_count == 1


def test_deep_retries_records_failure_without_crashing(test_db):
    """deep_analyzer 抛异常时单个观点标 failed, 任务整体 failed 但不崩。"""
    raw = _add_viewpoint(
        test_db, reasoning=None, summary=None, market_direction=None, analysis_summary="pending"
    )
    task = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=1
    )[0]
    task.task_type = "viewpoint_batch"
    task.total_count = 0
    task.processed_count = 0
    test_db.commit()

    def deep(item, source):
        raise RuntimeError("LLM down")

    ViewpointWorkflowService._run_deep_retries(test_db, task, [raw.id], deep)

    test_db.refresh(raw)
    test_db.refresh(task)
    assert (raw.analysis_summary or "").startswith("failed:")
    assert task.status == "failed"
    assert task.failed_count == 1


def test_retry_path_runs_deep_retries_end_to_end(test_db):
    """retry_task→run_fetch_task 经 _run_deep_retries 对失败观点补分析, 整条链路不再断。"""
    raw = _add_viewpoint(
        test_db, reasoning=None, summary=None, market_direction=None, analysis_summary="failed:LLM unavailable"
    )
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    task.status = "failed"
    task.failed_count = 1
    task.failed_ids = [{"viewpoint_id": raw.id, "source": "sina_blog", "error": "LLM unavailable"}]
    test_db.commit()

    retried = ViewpointWorkflowService.retry_task(test_db, task.id)
    assert retried.task_params["retry_viewpoint_ids"] == [raw.id]

    ViewpointWorkflowService.run_fetch_task(
        retried.id,
        session_factory=lambda: _NonClosingSession(test_db),
        deep_analyzer=lambda item, source: {
            "market_direction": "neutral", "confidence": 60, "summary": "补完",
            "reasoning": "理由", "sectors_bullish": [], "sectors_bearish": [], "analysis": "c",
        },
    )
    test_db.refresh(retried)
    test_db.refresh(raw)
    assert retried.status == "succeeded"
    assert raw.analysis_summary == "succeeded"
    assert raw.market_direction == "neutral"


def test_fetch_mode_skips_short_content_below_threshold(test_db):
    """fetch 模式正文<30字记 skipped, 不入库, 不调 LLM。"""
    article = {"title": "只有标题", "content": "看好半导体。", "url": "https://example.test/short"}
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [article]},
        deep_analyzer=lambda item, source: (_ for _ in ()).throw(AssertionError("不应调deep")),
    )

    test_db.refresh(task)
    assert test_db.query(Viewpoint).count() == 0
    assert test_db.query(CrawlerArticleRecord).count() == 0
    assert task.result_summary["skipped"] == 1
    assert task.success_count == 0
    assert task.processed_count == 1
    assert task.status == "succeeded"


def test_fetch_mode_adopts_content_at_threshold_boundary(test_db):
    """正文恰好30字进入采纳流程(fetch模式不调LLM直接入库pending)。"""
    content_30 = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"  # 30字
    assert len(content_30) >= 30
    article = {"title": "边界", "content": content_30, "url": "https://example.test/boundary"}
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [article]},
        deep_analyzer=lambda item, source: (_ for _ in ()).throw(AssertionError("fetch不应调deep")),
    )
    test_db.refresh(task)
    vp = test_db.query(Viewpoint).one()
    assert vp.analysis_summary == "pending"
    assert task.success_count == 1
    assert task.result_summary["skipped"] == 0


def test_fetch_defaults_only_allow_twenty_sina_blog_posts():
    """观点抓取只允许新浪博客，默认直接抓取20篇。"""
    from src.services.viewpoint_workflow_service import (
        DEFAULT_SOURCES, ALLOWED_SOURCES,
    )
    assert DEFAULT_SOURCES == ("sina_blog",)
    assert ALLOWED_SOURCES == frozenset({"sina_blog"})

    payload = viewpoint_routes.ViewpointFetchRequest()
    assert payload.sources == ["sina_blog"]
    assert payload.limit_per_source == 20
    assert payload.mode == "fetch"


@pytest.mark.parametrize("source", ["eastmoney_blog", "eastmoney_guide", "sina_finance"])
def test_fetch_rejects_removed_sources(source):
    with pytest.raises(ValueError, match="不支持的观点来源"):
        ViewpointWorkflowService._normalize_sources([source])


def test_fetch_request_rejects_ai_analysis_mode():
    with pytest.raises(ValidationError):
        viewpoint_routes.ViewpointFetchRequest(mode="fetch_and_analyze")
