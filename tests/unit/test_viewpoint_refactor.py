import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

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
        "content": "看好半导体板块，订单和资金面均有改善。",
        "author": "分析师",
        "url": "https://example.test/article/1",
        "publish_time": date.today().isoformat(),
    }
    task, created = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["eastmoney_blog"], limit_per_source=10
    )
    assert created is True

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"eastmoney_blog": lambda limit: [article, dict(article)]},
        capture_analyzer=lambda item, source: (True, {"score": 90}),
        deep_analyzer=lambda item, source: {
            "market_direction": "bullish",
            "confidence": 80,
            "summary": "半导体景气改善",
            "reasoning": "订单与资金改善",
            "time_horizon": "short",
            "sectors_bullish": ["半导体"],
            "sectors_bearish": [],
        },
    )

    assert test_db.query(Viewpoint).count() == 1
    assert test_db.query(CrawlerArticleRecord).count() == 1
    test_db.refresh(task)
    assert task.status == "succeeded"
    assert task.processed_count == 2
    assert task.success_count == 1
    assert task.result_summary["sources"]["eastmoney_blog"]["duplicates"] == 1


def test_fetch_job_keeps_raw_viewpoint_when_deep_analysis_fails_and_can_retry(test_db):
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_finance"], limit_per_source=5
    )
    article = {"title": "待重试", "content": "正文", "url": "https://example.test/retry"}

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_finance": lambda limit: [article]},
        capture_analyzer=lambda item, source: (True, {"score": 80}),
        deep_analyzer=lambda item, source: (_ for _ in ()).throw(RuntimeError("LLM unavailable")),
    )

    test_db.refresh(task)
    raw = test_db.query(Viewpoint).one()
    assert task.status == "failed"
    assert task.failed_count == 1
    assert raw.reasoning is None

    retried = ViewpointWorkflowService.retry_task(test_db, task.id)
    assert retried.status == "pending"
    assert retried.failed_count == 0
    assert retried.task_params["retry_viewpoint_ids"] == [raw.id]


def test_fetch_job_continues_when_one_source_fails(test_db):
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["eastmoney_blog", "sina_finance"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={
            "eastmoney_blog": lambda limit: (_ for _ in ()).throw(RuntimeError("source down")),
            "sina_finance": lambda limit: [{"title": "正常来源", "content": "正常正文"}],
        },
        capture_analyzer=lambda item, source: (True, {"score": 80}),
        deep_analyzer=lambda item, source: {
            "market_direction": "neutral", "confidence": 60, "summary": "正常完成",
            "reasoning": "来源可用", "sectors_bullish": [], "sectors_bearish": [],
        },
    )

    test_db.refresh(task)
    assert test_db.query(Viewpoint).count() == 1
    assert task.status == "failed"
    assert task.success_count == 1
    assert task.result_summary["sources"]["eastmoney_blog"]["error"] == "source down"


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


def test_default_capture_analyzer_uses_real_crawler_service_db(test_db, monkeypatch):
    """默认 capture_analyzer 必须持有有效的 CrawlerService.db，避免 AttributeError。"""
    from src.analyzer.post_analyzer import PostAnalyzer, PostAnalysisResult

    monkeypatch.setattr(
        PostAnalyzer,
        "should_capture",
        lambda self, post, source="manual": PostAnalysisResult(should_capture=True, score=8.0),
    )
    monkeypatch.setattr(
        PostAnalyzer,
        "analyze_post_simple",
        lambda self, post: {"sentiment": "bullish", "sentiment_score": 0.7, "sectors": ["半导体"]},
    )

    should_capture, analysis = ViewpointWorkflowService._default_capture_analyzer(
        {"title": "半导体景气改善", "content": "看好半导体板块"}, "eastmoney_blog"
    )
    assert should_capture is True
    assert analysis["score"] == 8.0
    assert analysis["sentiment"] == "bullish"


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
