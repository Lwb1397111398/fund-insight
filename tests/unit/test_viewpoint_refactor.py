from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.routes import viewpoints as viewpoint_routes
from src.models.database import CrawlerArticleRecord, Viewpoint
from src.services.viewpoint_workflow_service import ViewpointWorkflowService, beijing_today


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
        "content": (
            "看好半导体板块，订单和资金面均有改善，短期景气度持续回升，"
            "资金流入明显放大，建议重点关注相关产业链龙头标的，"
            "中期看行业景气有望延续，逢低可逐步建仓，"
            "重点把握细分赛道龙头的估值修复与业绩兑现机会。"
        ),
        "author": "分析师",
        "url": "https://example.test/article/1",
        "publish_time": beijing_today().isoformat(),
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

    response = viewpoint_routes.get_viewpoints(
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
    )

    assert response["meta"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
    assert len(response["data"]) == 1
    assert "content" not in response["data"][0]
    assert response["data"][0]["is_expired"] is False
    assert "is_summary" in response["data"][0]

def test_viewpoint_detail_excludes_soft_deleted_rows(test_db):
    viewpoint = _add_viewpoint(test_db, is_deleted=True)

    with pytest.raises(HTTPException) as exc_info:
        viewpoint_routes.get_viewpoint_detail(viewpoint.id, db=test_db)

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
        viewpoint_routes.delete_viewpoint(viewpoint.id, request=_Request({}), db=test_db)
    assert exc_info.value.status_code == 403

    response = viewpoint_routes.delete_viewpoint(
        viewpoint.id,
        request=_Request({"x-danger-confirm": "delete-viewpoint"}),
        db=test_db,
    )

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
        "content": (
            "看好半导体板块景气持续改善，订单与资金面共振，短期趋势偏强，"
            "建议关注相关产业链龙头标的，中期行业上行空间仍存，"
            "可逢低分批建仓，重点把握细分赛道龙头估值修复机会。"
        ),
        "author": "分析师",
        "url": "https://example.test/fetch/1",
        "publish_time": beijing_today().isoformat(),
    }
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )

    deep_calls = {"n": 0}

    def deep(item, source):
        deep_calls["n"] += 1
        return {"market_direction": "bullish", "confidence": 80, "summary": "x", "reasoning": "y"}

    assistant_calls = {"n": 0}

    def assistant(item, source):
        assistant_calls["n"] += 1
        return True

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [article]},
        deep_analyzer=deep,
        assistant_judge=assistant,
    )

    test_db.refresh(task)
    vp = test_db.query(Viewpoint).one()
    assert deep_calls["n"] == 0             # fetch 模式不调 deep analysis
    assert assistant_calls["n"] == 0        # 命中核心观点词 → 不触发辅助AI
    assert vp.analysis_summary == "pending"  # 仅入库, 等一键AI分析
    assert task.status == "succeeded"
    assert task.success_count == 1
    assert task.result_summary["assistant_calls"] == 0


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
    """fetch 模式正文过短记 skipped, 不入库, 不调 LLM。"""
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
    """正文达到质量门槛时进入采纳流程(fetch模式不调LLM直接入库pending)。"""
    content_ok = (
        "人工智能板块资金流入显著放大，短期趋势偏强，"
        "基本面与资金面形成共振，中期看仍具备上行空间，"
        "建议逢低布局，重点关注算力、模型与应用侧龙头标的，"
        "把握产业趋势驱动的持续性行情机会。"
    )
    assert len(content_ok) >= 80
    article = {"title": "边界", "content": content_ok, "url": "https://example.test/boundary"}
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


def test_fetch_defaults_include_three_live_sources():
    """观点抓取默认三源：新浪博客 + 热门股吧 + 热门基金吧。"""
    from src.services.viewpoint_workflow_service import (
        DEFAULT_SOURCES, ALLOWED_SOURCES,
    )
    assert DEFAULT_SOURCES == ("sina_blog", "stock_guba", "fund_guba")
    assert ALLOWED_SOURCES == frozenset({"sina_blog", "stock_guba", "fund_guba"})

    payload = viewpoint_routes.ViewpointFetchRequest()
    assert payload.sources == ["sina_blog", "stock_guba", "fund_guba"]
    assert payload.limit_per_source == 20
    assert payload.mode == "fetch"


def test_quality_gate_accepts_long_title_only_guba_style_posts():
    """股吧详情失败时，足够长且含观点词的标题仍可入库，避免整源 0 条。"""
    title = (
        "看多科技半导体板块，短期反弹可加仓，中期仍有上行空间和资金流入机会，"
        "建议关注龙头标的估值修复与业绩兑现。"
    )
    assert len(title) >= 40
    ok, reason = ViewpointWorkflowService._is_quality_viewpoint({
        "title": title,
        "content": title,
    })
    assert ok is True
    assert reason == "ok"


def test_quality_gate_rejects_short_title_only_posts():
    ok, reason = ViewpointWorkflowService._is_quality_viewpoint({
        "title": "看多科技",
        "content": "看多科技",
    })
    assert ok is False
    assert "内容" in reason


def test_list_serialization_uses_content_prefix_for_pending_rows():
    row = Viewpoint(
        content="人工智能板块资金流改善，短期趋势偏强，建议逢低布局相关龙头。",
        author="测试",
        source="stock_guba",
        viewpoint_date=date.today(),
        is_deleted=False,
        is_summary=False,
        summary=None,
        reasoning=None,
        market_direction=None,
    )
    data = viewpoint_routes._serialize_list(row)
    assert data["summary"].startswith("人工智能板块")
    assert data["analysis_status"] == "pending"


def test_stock_guba_board_code_mapping():
    from src.crawler.stock_guba_crawler import StockGubaCrawler
    crawler = StockGubaCrawler()
    assert crawler._board_code("000001") == "zssh000001"
    assert crawler._board_code("399001") == "sz399001"
    assert crawler._board_code("399006") == "sz399006"
    assert crawler._board_code("600519") == "sh600519"


def test_article_date_parses_chinese_and_post_time():
    assert ViewpointWorkflowService._article_date({
        "publish_time": "2026年03月07日 01:00",
    }) == date(2026, 3, 7)
    assert ViewpointWorkflowService._article_date({
        "post_time": "2026-07-28 16:16:21",
    }) == date(2026, 7, 28)
    assert ViewpointWorkflowService._article_date({
        "publish_time": "2026-07-19",
    }) == date(2026, 7, 19)


def test_batch_analyze_candidates_skip_summary_and_succeeded(test_db):
    from src.services.viewpoint_service import ViewpointService

    pending = _add_viewpoint(
        test_db,
        reasoning=None,
        summary=None,
        market_direction=None,
        analysis_summary="pending",
        is_summary=False,
        content="待分析原始观点，看多科技板块反弹机会。",
    )
    _add_viewpoint(
        test_db,
        is_summary=True,
        source="daily_summary",
        content="这是汇总，不应再送 AI",
        reasoning=None,
        summary="汇总摘要",
        analysis_summary="pending",
    )
    _add_viewpoint(
        test_db,
        analysis_summary="succeeded",
        reasoning="【AI深度分析】已完成",
        summary="已分析",
        content="已分析观点",
    )
    rows = ViewpointService(test_db).get_viewpoints_for_batch_analyze(limit=10, source="all", days=7)
    ids = [r.id for r in rows]
    assert pending.id in ids
    assert all(not r.is_summary for r in rows)
    assert all((r.analysis_summary or "") != "succeeded" for r in rows)


@pytest.mark.parametrize("source", ["eastmoney_blog", "eastmoney_guide", "sina_finance"])
def test_fetch_rejects_removed_sources(source):
    with pytest.raises(ValueError, match="不支持的观点来源"):
        ViewpointWorkflowService._normalize_sources([source])


def test_fetch_request_rejects_ai_analysis_mode():
    with pytest.raises(ValidationError):
        viewpoint_routes.ViewpointFetchRequest(mode="fetch_and_analyze")


# ===== 当天观点过滤 =====

def _long_viewpoint_content():
    return (
        "看好半导体板块景气持续改善，订单与资金面共振，短期趋势偏强，"
        "建议关注相关产业链龙头标的，中期行业上行空间仍存，"
        "可逢低分批建仓，重点把握细分赛道龙头估值修复机会。"
    )


def test_fetch_skips_articles_published_before_today(test_db):
    """非当天发布的观点直接跳过，不入库，跳过原因可审计。"""
    yesterday = (beijing_today() - timedelta(days=1)).isoformat()
    article = {
        "title": "昨天的文章",
        "content": _long_viewpoint_content(),
        "url": "https://example.test/old",
        "publish_time": yesterday,
    }
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
    assert task.result_summary["skipped"] == 1
    reasons = task.result_summary["sources"]["sina_blog"]["skipped_reasons"]
    assert reasons.get("非当天观点") == 1
    assert task.status == "succeeded"


def test_fetch_adopts_same_day_and_tolerates_slightly_future_dates(test_db):
    """当天观点正常入库；轻度未来时间（明天）容忍放行，防服务器时钟偏差误杀整源。"""
    today = beijing_today().isoformat()
    tomorrow = (beijing_today() + timedelta(days=1)).isoformat()
    articles = [
        {"title": "当天", "content": _long_viewpoint_content(),
         "url": "https://example.test/today", "publish_time": today},
        {"title": "明天时钟偏差", "content": _long_viewpoint_content(),
         "url": "https://example.test/tomorrow", "publish_time": tomorrow},
    ]
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )

    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: articles},
    )

    test_db.refresh(task)
    assert test_db.query(Viewpoint).count() == 2
    assert task.result_summary.get("skipped", 0) == 0


# ===== 深度分析剔除非理性内容 =====

def _run_batch_with_deep(test_db, raw, deep):
    task = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=1
    )[0]
    task.task_type = "viewpoint_batch"
    task.total_count = 0
    task.processed_count = 0
    task.success_count = 0
    test_db.commit()
    ViewpointWorkflowService._run_deep_retries(test_db, task, [raw.id], deep)
    test_db.refresh(task)
    return task


def test_deep_analysis_rejects_emotional_content_via_soft_delete(test_db):
    """AI 判定为情绪表达的观点：软删除 + rejected 标记，不进列表和每日汇总。"""
    raw = _add_viewpoint(
        test_db, reasoning=None, summary=None, market_direction=None,
        analysis_summary="pending", content="又跌了真恶心，垃圾市场，全部清仓跑路！",
    )

    def deep(item, source):
        return {
            "viewpoint_type": "情绪表达",
            "market_direction": "bearish", "confidence": 30,
            "summary": "x", "reasoning": "纯情绪", "analysis": "无依据",
        }

    task = _run_batch_with_deep(test_db, raw, deep)

    test_db.refresh(raw)
    assert raw.is_deleted is True
    assert (raw.analysis_summary or "").startswith("rejected:")
    assert "情绪表达" in (raw.reassessment_reason or "")
    assert raw.viewpoint_type == "情绪表达"
    # 拒绝是正常处理而非错误：任务仍 succeeded
    assert task.status == "succeeded"
    assert task.success_count == 1


@pytest.mark.parametrize("kept_type", ["明确预测", "深度分析", "行情复盘"])
def test_deep_analysis_keeps_rational_analysis_types(test_db, kept_type):
    """明确预测/深度分析/行情复盘属于理性市场分析，正常保留。"""
    raw = _add_viewpoint(
        test_db, reasoning=None, summary=None, market_direction=None,
        analysis_summary="pending",
    )

    def deep(item, source):
        return {
            "viewpoint_type": kept_type,
            "market_direction": "bullish", "confidence": 70,
            "summary": "AI摘要", "reasoning": "理由", "analysis": "深度结论",
        }

    _run_batch_with_deep(test_db, raw, deep)

    test_db.refresh(raw)
    assert raw.is_deleted is False
    assert raw.analysis_summary == "succeeded"
    assert raw.viewpoint_type == kept_type


def test_analysis_status_reports_rejected_rows():
    row = Viewpoint(
        content="广告内容",
        source="stock_guba",
        viewpoint_date=date.today(),
        analysis_summary="rejected:广告引流",
        is_deleted=True,
        is_summary=False,
    )
    assert viewpoint_routes._analysis_status(row) == "rejected"


def test_sina_is_before_today_uses_beijing_date():
    from src.crawler.sina_blog_crawler import SinaBlogCrawler

    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y年%m月%d日 10:00")
    today_str = now.strftime("%Y年%m月%d日 10:00")

    assert SinaBlogCrawler._is_before_today(yesterday) is True
    assert SinaBlogCrawler._is_before_today(today_str) is False
    # 解析失败/空值：放行，不误终止抓取
    assert SinaBlogCrawler._is_before_today("乱七八糟的时间") is False
    assert SinaBlogCrawler._is_before_today("") is False


# ===== 关键词分层 + 辅助AI裁决 =====

# 门禁关键词拆分前的原始集合：测试并集不能漏词、不能重叠。
_ORIGINAL_GATE_KEYWORDS = (
    '看多', '看空', '看涨', '看跌', '牛市', '熊市', '上涨', '下跌',
    '板块', '科技', '医药', '消费', '新能源', '半导体', '芯片', '军工',
    '金融', '地产', '银行', '券商', '白酒', '光伏', '锂电', 'AI',
    '加仓', '减仓', '建仓', '清仓', '调仓', '持仓', '仓位',
    '买入', '卖出', '观望', '抄底', '止盈', '止损',
    '突破', '跌破', '反弹', '回调', '震荡', '调整',
    '压力位', '支撑位', '目标位', '阻力位',
    '观点', '预测', '判断', '分析', '逻辑', '策略', '建议',
    '机会', '风险', '利好', '利空',
)


def test_keyword_split_union_equals_original_gate_without_overlap():
    from src.services import viewpoint_workflow_service as vws

    core = set(vws._CORE_VIEWPOINT_KEYWORDS)
    general = set(vws._GENERAL_VIEWPOINT_KEYWORDS)

    assert not (core & general), "核心词与泛化词不应重叠"
    assert core | general == set(_ORIGINAL_GATE_KEYWORDS), "拆分不能漏词"
    assert set(vws._VIEWPOINT_KEYWORDS) == core | general


def test_needs_assistant_judge_flags_only_general_keyword_hits():
    core_article = {
        "title": "看多科技板块",
        "content": "看多科技板块，建议逢低加仓半导体龙头，中期仍有上行空间。",
    }
    assert ViewpointWorkflowService._needs_assistant_judge(core_article) is False

    # 只命中"分析/观点/风险/机会"等泛化词、零核心词 → 规则拿不准，交给辅助AI
    general_article = {
        "title": "市场分析",
        "content": (
            "本文分析当前基金市场的形势，作者观点认为后续风险与机会并存，"
            "普通投资者应当注意风险管理，谨慎对待。"
        ),
    }
    assert ViewpointWorkflowService._needs_assistant_judge(general_article) is True
    # 同时确认它过得了质量门禁（否则轮不到辅助AI）
    ok, _ = ViewpointWorkflowService._is_quality_viewpoint(general_article)
    assert ok is True


def _borderline_article(index: int) -> dict:
    return {
        "title": "市场分析",
        "content": (
            "本文分析当前基金市场的形势，作者观点认为后续风险与机会并存，"
            "普通投资者应当注意风险管理，谨慎对待。"
        ),
        "url": f"https://example.test/borderline/{index}",
    }


def test_fetch_asks_assistant_ai_for_borderline_content(test_db):
    """边界内容（仅泛化词命中）交给辅助AI：AI 判否则跳过并记原因，判是则入库。"""
    calls = []

    def judge_reject(article, source):
        calls.append(article["url"])
        return False

    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [_borderline_article(1)]},
        assistant_judge=judge_reject,
    )

    test_db.refresh(task)
    assert calls == ["https://example.test/borderline/1"]
    assert test_db.query(Viewpoint).count() == 0
    reasons = task.result_summary["sources"]["sina_blog"]["skipped_reasons"]
    assert reasons.get("辅助AI判定非有效观点") == 1

    # 换一个 AI 判"是"的裁决函数 → 入库
    task2, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task2.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [_borderline_article(2)]},
        assistant_judge=lambda article, source: True,
    )
    assert test_db.query(Viewpoint).count() == 1


def test_fetch_caps_assistant_ai_calls_and_falls_back_to_adopt(test_db):
    """辅助AI每次任务最多调用15次；超限的边界内容按放行处理（深度分析二次兜底）。"""
    calls = {"n": 0}

    def judge(article, source):
        calls["n"] += 1
        return False

    articles = [_borderline_article(i) for i in range(16)]
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=20
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: articles},
        assistant_judge=judge,
    )

    test_db.refresh(task)
    assert calls["n"] == 15
    assert task.result_summary["assistant_calls"] == 15
    # 第16篇超过上限 → 放行入库
    assert test_db.query(Viewpoint).count() == 1


def test_fetch_assistant_ai_failure_falls_back_to_adopt(test_db):
    """辅助AI抛异常时按放行处理，不能因为AI抖动误杀观点。"""
    def judge(article, source):
        raise RuntimeError("LLM down")

    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: [_borderline_article(0)]},
        assistant_judge=judge,
    )

    test_db.refresh(task)
    assert test_db.query(Viewpoint).count() == 1
    assert task.status == "succeeded"


def test_quick_judge_returns_none_when_llm_unavailable(monkeypatch):
    from src.analyzer import viewpoint_analyzer as va

    def boom():
        raise RuntimeError("no llm configured")

    monkeypatch.setattr(va, "get_analyzer", boom)
    assert va.quick_judge_viewpoint("标题", "内容") is None


# ===== 抓取量保底 =====

def test_fetch_warns_when_adopted_count_below_ten(test_db):
    """当天采纳不足10条时任务结果给出低量预警，但不影响成功状态。"""
    articles = [
        {
            "title": f"看多科技板块{i}",
            "content": "看多科技板块，建议逢低加仓半导体龙头，中期仍有上行空间与资金流入机会。",
            "url": f"https://example.test/low{i}",
            "publish_time": beijing_today().isoformat(),
        }
        for i in range(2)
    ]
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=5
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: articles},
    )

    test_db.refresh(task)
    assert task.status == "succeeded"
    assert task.result_summary.get("low_volume") is True
    assert "不足10条" in task.result_summary["message"]


def test_fetch_no_low_volume_warning_at_ten_or_above(test_db):
    articles = [
        {
            "title": f"看多科技板块{i}",
            "content": "看多科技板块，建议逢低加仓半导体龙头，中期仍有上行空间与资金流入机会。",
            "url": f"https://example.test/ok{i}",
            "publish_time": beijing_today().isoformat(),
        }
        for i in range(10)
    ]
    task, _ = ViewpointWorkflowService.create_fetch_task(
        test_db, sources=["sina_blog"], limit_per_source=20
    )
    ViewpointWorkflowService.run_fetch_task(
        task.id,
        session_factory=lambda: _NonClosingSession(test_db),
        fetchers={"sina_blog": lambda limit: articles},
    )

    test_db.refresh(task)
    assert task.status == "succeeded"
    assert test_db.query(Viewpoint).count() == 10
    assert "low_volume" not in (task.result_summary or {})
