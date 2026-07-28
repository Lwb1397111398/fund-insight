"""观点抓取、深度分析和每日汇总的持久化工作流。"""
from __future__ import annotations

import hashlib
import logging
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.models.database import BatchAnalysisTask, CrawlerArticleRecord, SessionLocal, Viewpoint
from src.services.viewpoint_service import get_source_authority


logger = logging.getLogger(__name__)


DEFAULT_SOURCES = ("sina_blog", "stock_guba", "fund_guba")
ALLOWED_SOURCES = frozenset(DEFAULT_SOURCES)

# 观点内容门禁：内容必须包含至少一个，才视为"博主市场观点"。
_VIEWPOINT_KEYWORDS = (
    '大盘', '板块', '看多', '看空', '加仓', '减仓', '建仓', '清仓', '补仓',
    '调仓', '换股', '持股', '空仓', '满仓', '半仓', '仓位', '持仓', '观望',
    '回调', '反弹', '突破', '跌破', '上涨', '下跌', '调整', '震荡',
    '牛市', '熊市', '行情', '走势', '点位', '目标', '利好', '利空',
    '股市', '市场', '投资', '观点', '预测', '预期', '判断', '分析', '逻辑',
    '龙头', '白马', '蓝筹', '成长', '价值', '赛道', '概念', '风口',
    'ETF', '基金', '股票', 'A股', '港股', '美股', '指数', '沪指', '深指',
    '创业板', '科创板', '北交所', '金融', '科技', '医药', '消费', '新能源',
    '地产', '军工', '能源', '光伏', '风电', '储能', '锂电池', '半导体',
    '芯片', '人工智能', 'AI', '银行', '保险', '证券', '券商', '白酒',
    '谨慎', '乐观', '悲观', '风险', '机会', '建议', '策略', '操作',
)

# 命中即丢弃的垃圾关键词（广告、导流、非法荐股）。
_SPAM_KEYWORDS = (
    '加群', '加微信', '加QQ', '私聊', '代客理财', '荐股', '牛股', '黑马',
    '内幕', '跟单', '带盘', '分成', '保本', '稳赚', '包赚', '合作', '咨询',
    '联系我', '扫码', '关注公众号', '添加好友', '免费领取', '限时',
)

# 低于此字数的内容视为无分析价值。
_MIN_CONTENT_LENGTH = 80


class ViewpointWorkflowService:
    """使用现有表实现可恢复的观点流水线。"""

    @staticmethod
    def _stable_article_id(source: str, article: Dict[str, Any]) -> str:
        stable_value = next(
            (
                str(article.get(key)).strip()
                for key in ("article_id", "id", "newsid", "url", "link")
                if article.get(key)
            ),
            "|".join(
                str(article.get(key) or "").strip()
                for key in ("title", "author", "publish_time", "date")
            ),
        )
        digest = hashlib.md5(stable_value.encode("utf-8")).hexdigest()
        return f"{source}:{digest}"

    @staticmethod
    def _content_hash(article: Dict[str, Any]) -> str:
        content = str(article.get("content") or article.get("title") or "").strip()
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _article_date(article: Dict[str, Any]) -> date:
        value = article.get("publish_time") or article.get("publish_date") or article.get("date")
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value:
            text = str(value).strip().replace("/", "-")
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return datetime.strptime(text[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
        return date.today()

    @staticmethod
    def _normalize_sources(sources: Optional[Iterable[str]]) -> List[str]:
        selected = list(dict.fromkeys(sources or DEFAULT_SOURCES))
        invalid = [source for source in selected if source not in ALLOWED_SOURCES]
        if invalid:
            raise ValueError(f"不支持的观点来源: {', '.join(invalid)}")
        return selected

    @classmethod
    def create_fetch_task(
        cls,
        db: Session,
        *,
        sources: Optional[Iterable[str]] = None,
        limit_per_source: int = 20,
        mode: str = "fetch",
    ) -> Tuple[BatchAnalysisTask, bool]:
        if mode != "fetch":
            raise ValueError("观点抓取仅支持 fetch 模式")
        selected = cls._normalize_sources(sources)
        limit_per_source = max(1, min(int(limit_per_source or 20), 50))
        active = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.task_type == "viewpoint_fetch",
            BatchAnalysisTask.status.in_(("pending", "running")),
        ).order_by(BatchAnalysisTask.created_at.desc()).first()
        if active:
            if active.status == "running" and active.updated_at:
                if datetime.now() - active.updated_at > timedelta(minutes=20):
                    active.status = "pending"
                    active.error_message = "检测到 Render 重启中断，任务已恢复等待执行"
                    db.commit()
            return active, False

        source_stats = {
            source: {"fetched": 0, "adopted": 0, "duplicates": 0, "skipped": 0, "failed": 0}
            for source in selected
        }
        task = BatchAnalysisTask(
            task_type="viewpoint_fetch",
            status="pending",
            total_count=0,
            processed_count=0,
            success_count=0,
            failed_count=0,
            processed_ids=[],
            failed_ids=[],
            task_params={"sources": selected, "limit_per_source": limit_per_source, "mode": mode},
            result_summary={"sources": source_stats, "adopted": 0, "duplicates": 0, "skipped": 0},
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task, True

    @staticmethod
    def _default_fetchers() -> Dict[str, Callable[[int], List[Dict[str, Any]]]]:
        from src.crawler.sina_blog_crawler import get_blog_crawler
        from src.crawler.stock_guba_crawler import StockGubaCrawler
        from src.crawler.tiantian_crawler import TiantianCrawler

        stock_guba = StockGubaCrawler()
        fund_guba = TiantianCrawler()

        # 热门指数和基金代码
        HOT_STOCKS = ['000001', '399001', '399006']  # 上证、深证、创业板
        HOT_FUNDS = ['000001', '110022', '519772', '161725']  # 热门基金示例

        def fetch_stock_guba(limit: int) -> List[Dict[str, Any]]:
            """抓取热门股吧"""
            results = stock_guba.fetch_hot_stocks(HOT_STOCKS)
            all_posts = []
            for posts in results.values():
                all_posts.extend(posts)
            return all_posts[:limit]

        def fetch_fund_guba(limit: int) -> List[Dict[str, Any]]:
            """抓取热门基金吧"""
            all_posts = []
            for fund_code in HOT_FUNDS:
                posts = fund_guba.fetch_fund_posts(fund_code)
                all_posts.extend(posts)
                if len(all_posts) >= limit:
                    break
            return all_posts[:limit]

        return {
            "sina_blog": lambda limit: get_blog_crawler().fetch_blog_posts(max_posts=limit),
            "stock_guba": fetch_stock_guba,
            "fund_guba": fetch_fund_guba,
        }

    @staticmethod
    def _default_deep_analyzer(article: Dict[str, Any], source: str) -> Dict[str, Any]:
        from src.analyzer.viewpoint_analyzer import get_viewpoint_analyzer

        return get_viewpoint_analyzer().analyze_viewpoint(
            title=str(article.get("title") or ""),
            content=str(article.get("content") or article.get("title") or ""),
            author=str(article.get("author") or ""),
            source=source,
        )

    @staticmethod
    def _apply_deep_analysis(viewpoint: Viewpoint, analysis: Dict[str, Any]) -> None:
        horizon = analysis.get("time_horizon") or "medium"
        valid_days = {"short": 7, "medium": 30, "long": 90}.get(horizon, 30)
        viewpoint.market_direction = analysis.get("market_direction") or "neutral"
        viewpoint.confidence = int(analysis.get("confidence") or 50)
        viewpoint.sectors_bullish = analysis.get("sectors_bullish") or []
        viewpoint.sectors_bearish = analysis.get("sectors_bearish") or []
        analysis_text = analysis.get("analysis") or ""
        reasoning = analysis.get("reasoning") or ""
        viewpoint.reasoning = f"【AI深度分析】{analysis_text}\n\n【判断理由】{reasoning}".strip()
        viewpoint.summary = analysis.get("summary") or viewpoint.content[:80]
        viewpoint.time_horizon = horizon
        viewpoint.validity_period = f"{valid_days}天"
        viewpoint.valid_until = viewpoint.viewpoint_date + timedelta(days=valid_days)
        viewpoint.credibility_score = int(analysis.get("credibility") or 50)
        viewpoint.tags = analysis.get("key_points") or []
        viewpoint.action_suggestion = analysis.get("action_suggestion") or "观望"
        viewpoint.risk_level = analysis.get("risk_level") or "medium"
        viewpoint.source_authority = get_source_authority(viewpoint.source)
        viewpoint.analysis_summary = "succeeded"
        viewpoint.calculate_weight()

    @classmethod
    def run_fetch_task(
        cls,
        task_id: int,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        fetchers: Optional[Dict[str, Callable[[int], List[Dict[str, Any]]]]] = None,
        deep_analyzer: Optional[Callable[[Dict[str, Any], str], Dict[str, Any]]] = None,
    ) -> None:
        """抓取新浪博客并直接入库，不调用 AI 筛选或深度分析。"""
        fetchers = fetchers or cls._default_fetchers()
        deep_analyzer = deep_analyzer or cls._default_deep_analyzer
        db = session_factory()
        try:
            task = db.query(BatchAnalysisTask).filter(
                BatchAnalysisTask.id == task_id,
                BatchAnalysisTask.task_type == "viewpoint_fetch",
            ).with_for_update().first()
            if not task or task.status in ("succeeded", "cancelled"):
                return
            task.status = "running"
            task.started_at = task.started_at or datetime.now()
            task.completed_at = None
            task.error_message = None
            db.commit()

            params = dict(task.task_params or {})
            retry_ids = list(params.get("retry_viewpoint_ids") or [])
            if retry_ids:
                cls._run_deep_retries(db, task, retry_ids, deep_analyzer)
                return

            sources = cls._normalize_sources(params.get("sources"))
            limit = max(1, min(int(params.get("limit_per_source") or 20), 50))

            # ===== 阶段 1: 抓取 =====
            # 先从各源拉取文章到内存, 记录抓取进度(阶段耗时在进度栏显示)
            fetch_started = datetime.now()
            source_articles: Dict[str, List[Dict[str, Any]]] = {}
            total_to_fetch = 0
            for source in sources:
                task = db.get(BatchAnalysisTask, task_id)
                if not task or task.status == "cancelled":
                    return
                summary = dict(task.result_summary or {})
                source_stats = dict((summary.get("sources") or {}).get(source) or {})
                try:
                    articles = list(fetchers[source](limit) or [])
                    source_articles[source] = articles
                    source_stats["fetched"] = len(articles)
                    total_to_fetch += len(articles)
                    task.total_count = total_to_fetch
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()
                except Exception as exc:
                    source_articles[source] = []
                    source_stats["fetched"] = 0
                    source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                    source_stats["error"] = str(exc)
                    failures = list(task.failed_ids or [])
                    failures.append({"source": source, "error": str(exc)})
                    task.failed_ids = failures
                    task.failed_count = len(failures)
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()

            # 记录抓取阶段耗时与总数
            task = db.get(BatchAnalysisTask, task_id)
            if task:
                summary = dict(task.result_summary or {})
                summary["fetch_duration_seconds"] = round((datetime.now() - fetch_started).total_seconds(), 1)
                summary["fetched_total"] = total_to_fetch
                task.result_summary = summary
                db.commit()

            for source, articles in source_articles.items():
                task = db.get(BatchAnalysisTask, task_id)
                if not task or task.status == "cancelled":
                    return
                for article in articles:
                    try:
                        cls._process_article(db, task_id, source, article)
                    except Exception as exc:
                        db.rollback()
                        task = db.get(BatchAnalysisTask, task_id)
                        summary = dict(task.result_summary or {})
                        source_stats = dict((summary.get("sources") or {}).get(source) or {})
                        source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                        failures = list(task.failed_ids or [])
                        failures.append({"article_id": cls._stable_article_id(source, article), "source": source, "error": str(exc)})
                        task.failed_ids = failures
                        task.failed_count = len(failures)
                        task.processed_count = (task.processed_count or 0) + 1
                        cls._save_source_stats(task, summary, source, source_stats)
                        db.commit()

            task = db.get(BatchAnalysisTask, task_id)
            if task and task.status != "cancelled":
                task.status = "failed" if task.failed_count else "succeeded"
                task.completed_at = datetime.now()
                db.commit()
        except Exception as exc:
            db.rollback()
            task = db.get(BatchAnalysisTask, task_id)
            if task and task.status != "cancelled":
                task.status = "failed"
                task.error_message = str(exc)
                task.error_stack = traceback.format_exc()
                task.completed_at = datetime.now()
                db.commit()
            logger.exception("观点抓取任务 %s 失败", task_id)
        finally:
            db.close()

    @staticmethod
    def _save_source_stats(task, summary, source, source_stats):
        sources = dict(summary.get("sources") or {})
        sources[source] = source_stats
        summary["sources"] = sources
        task.result_summary = summary

    @classmethod
    def _is_quality_viewpoint(cls, article: Dict[str, Any]) -> Tuple[bool, str]:
        """判断文章是否是值得入库的博主观点。返回 (是否通过, 原因)。"""
        title = str(article.get("title") or "").strip()
        content = str(article.get("content") or "").strip()

        # 内容等于标题 → 详情页根本没抓到正文，只有列表页标题。
        if content and content == title:
            return False, f"内容等于标题，疑似未抓到正文（{len(content)}字）"

        if len(content) < _MIN_CONTENT_LENGTH:
            return False, f"内容过短（{len(content)}字 < {_MIN_CONTENT_LENGTH}）"

        text = f"{title} {content}"
        for spam in _SPAM_KEYWORDS:
            if spam in text:
                return False, f"命中垃圾关键词：{spam}"

        if not any(kw in text for kw in _VIEWPOINT_KEYWORDS):
            return False, "未包含任何市场/观点关键词，疑似非观点内容"

        return True, "ok"

    @classmethod
    def _process_article(cls, db, task_id, source, article):
        task = db.get(BatchAnalysisTask, task_id)
        article_id = cls._stable_article_id(source, article)
        summary = dict(task.result_summary or {})
        source_stats = dict((summary.get("sources") or {}).get(source) or {})
        existing = db.query(CrawlerArticleRecord).filter(
            CrawlerArticleRecord.article_id == article_id
        ).first()
        if existing:
            source_stats["duplicates"] = (source_stats.get("duplicates") or 0) + 1
            summary["duplicates"] = (summary.get("duplicates") or 0) + 1
            task.processed_count = (task.processed_count or 0) + 1
            cls._save_source_stats(task, summary, source, source_stats)
            db.commit()
            return

        quality_ok, quality_reason = cls._is_quality_viewpoint(article)
        if not quality_ok:
            source_stats["skipped"] = (source_stats.get("skipped") or 0) + 1
            source_stats["skipped_reasons"] = source_stats.get("skipped_reasons") or {}
            source_stats["skipped_reasons"][quality_reason] = (
                source_stats["skipped_reasons"].get(quality_reason, 0) + 1
            )
            summary["skipped"] = (summary.get("skipped") or 0) + 1
            task.processed_count = (task.processed_count or 0) + 1
            processed = list(task.processed_ids or [])
            processed.append(article_id)
            task.processed_ids = processed
            cls._save_source_stats(task, summary, source, source_stats)
            db.commit()
            return

        record = CrawlerArticleRecord(
            article_id=article_id,
            source=source,
            title=str(article.get("title") or "")[:500],
            content_hash=cls._content_hash(article),
            url=article.get("url") or article.get("link"),
            author=article.get("author"),
            is_adopted=False,
            capture_score=0.0,
        )
        db.add(record)

        viewpoint = Viewpoint(
            viewpoint_date=cls._article_date(article),
            content=str(article.get("content") or article.get("title") or ""),
            author=str(article.get("author") or "未知"),
            source=source,
            article_id=article_id,
            article_url=record.url,
            content_hash=record.content_hash,
            source_authority=get_source_authority(source),
            is_deleted=False,
            is_summary=False,
            analysis_summary="pending",
        )
        db.add(viewpoint)
        db.flush()
        record.is_adopted = True
        record.viewpoint_id = viewpoint.id
        db.commit()

        source_stats["adopted"] = source_stats.get("adopted", 0) + 1
        summary["adopted"] = summary.get("adopted", 0) + 1
        task.success_count = (task.success_count or 0) + 1
        processed = list(task.processed_ids or [])
        processed.append(article_id)
        task.processed_ids = processed
        task.processed_count = (task.processed_count or 0) + 1
        cls._save_source_stats(task, summary, source, source_stats)
        db.commit()

    @classmethod
    def _run_deep_retries(
        cls,
        db: Session,
        task: BatchAnalysisTask,
        viewpoint_ids: List[int],
        deep_analyzer: Callable[[Dict[str, Any], str], Dict[str, Any]],
    ) -> None:
        """对已有 viewpoint 逐个补深度分析并写回，更新任务进度与状态。

        被 batch-analyze 后台任务和 retry_task(带 retry_viewpoint_ids) 共用。
        """
        ids = list(dict.fromkeys(int(vid) for vid in viewpoint_ids if vid))
        task.status = "running"
        task.started_at = task.started_at or datetime.now()
        if not task.total_count:
            task.total_count = len(ids)
        task.completed_at = None
        task.error_message = None
        task.failed_ids = list(task.failed_ids or [])
        db.commit()

        for vid in ids:
            task = db.get(BatchAnalysisTask, task.id)
            if not task or task.status == "cancelled":
                return
            viewpoint = db.get(Viewpoint, vid)
            if not viewpoint or viewpoint.is_deleted or viewpoint.is_summary:
                task.processed_count = (task.processed_count or 0) + 1
                db.commit()
                continue
            article = {
                "title": (viewpoint.content or "")[:200],
                "content": viewpoint.content or "",
                "author": viewpoint.author or "",
            }
            try:
                analysis = deep_analyzer(article, viewpoint.source or "manual")
                cls._apply_deep_analysis(viewpoint, analysis)
                task.success_count = (task.success_count or 0) + 1
                processed = list(task.processed_ids or [])
                processed.append(vid)
                task.processed_ids = processed
            except Exception as exc:
                db.rollback()
                task = db.get(BatchAnalysisTask, task.id)
                viewpoint = db.get(Viewpoint, vid)
                if viewpoint:
                    viewpoint.analysis_summary = f"failed:{str(exc)[:180]}"
                failures = list(task.failed_ids or [])
                failures.append({"viewpoint_id": vid, "source": viewpoint.source if viewpoint else None, "error": str(exc)})
                task.failed_ids = failures
                task.failed_count = len(failures)
            task.processed_count = (task.processed_count or 0) + 1
            db.commit()

        task = db.get(BatchAnalysisTask, task.id)
        if task and task.status != "cancelled":
            task.status = "failed" if task.failed_count else "succeeded"
            task.completed_at = datetime.now()
            db.commit()

    @staticmethod
    def retry_task(db: Session, task_id: int) -> BatchAnalysisTask:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == task_id,
            BatchAnalysisTask.task_type.in_(("viewpoint_fetch", "viewpoint_summary", "viewpoint_batch")),
        ).with_for_update().first()
        if not task:
            raise ValueError("观点任务不存在")
        if task.status not in ("failed", "cancelled"):
            raise ValueError("只有失败或已取消的任务可以重试")
        params = dict(task.task_params or {})
        retry_ids = [item.get("viewpoint_id") for item in (task.failed_ids or []) if item.get("viewpoint_id")]
        if retry_ids:
            params["retry_viewpoint_ids"] = list(dict.fromkeys(retry_ids))
        task.task_params = params
        task.status = "pending"
        task.failed_ids = []
        task.failed_count = 0
        task.error_message = None
        task.error_stack = None
        task.completed_at = None
        task.processed_count = 0
        if not retry_ids:
            task.total_count = 0
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def serialize_task(task: BatchAnalysisTask) -> Dict[str, Any]:
        total = task.total_count or 0
        processed = task.processed_count or 0
        summary = dict(task.result_summary or {})

        # 耗时与 ETA
        now = datetime.now()
        started = task.started_at or task.created_at or now
        completed = task.completed_at
        end_time = completed or now
        elapsed_seconds = round((end_time - started).total_seconds(), 1)
        eta_seconds = None
        if not completed and processed > 0 and total > 0:
            rate = processed / max(elapsed_seconds, 0.1)  # 项/秒
            remaining = max(total - processed, 0)
            eta_seconds = round(remaining / rate, 1)

        # 抓取阶段耗时(抓取模式/混合模式均记录)
        fetch_duration = summary.get("fetch_duration_seconds")

        return {
            "task_id": task.id,
            "task_type": task.task_type,
            "status": "succeeded" if task.status == "completed" else task.status,
            "total_count": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "progress": round(processed / total * 100, 1) if total else 0,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "fetch_duration_seconds": fetch_duration,
            "result_summary": summary,
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    @staticmethod
    def summarize_date(
        db: Session,
        target_date: date,
        *,
        summarizer: Optional[Callable[[List[Dict[str, Any]], str], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from src.analyzer.llm_analyzer import summarize_viewpoints_by_date

        summarizer = summarizer or summarize_viewpoints_by_date
        try:
            existing = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date == target_date,
                Viewpoint.is_summary.is_(True),
                Viewpoint.is_deleted.is_(False),
            ).with_for_update().first()
            if existing:
                return {
                    "success": True,
                    "summary_id": existing.id,
                    "deleted_originals": 0,
                    "already_summarized": True,
                }

            originals = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date == target_date,
                Viewpoint.is_summary.is_(False),
                Viewpoint.is_deleted.is_(False),
            ).with_for_update().all()
            if not originals:
                raise ValueError("该日期没有可汇总观点")
            incomplete = [row.id for row in originals if not row.market_direction or not row.summary or not row.reasoning]
            if incomplete:
                raise ValueError(f"仍有 {len(incomplete)} 条观点尚未完成深度分析")

            payload = [
                {
                    "id": row.id,
                    "summary": row.summary,
                    "market_direction": row.market_direction,
                    "confidence": row.confidence,
                    "sectors_bullish": row.sectors_bullish or [],
                    "sectors_bearish": row.sectors_bearish or [],
                    "source": row.source,
                }
                for row in originals
            ]
            result = summarizer(payload, target_date.isoformat())
            if not result or result.get("success") is False:
                raise RuntimeError((result or {}).get("error") or "观点汇总失败")

            original_ids = [row.id for row in originals]
            summary = Viewpoint(
                viewpoint_date=target_date,
                source="daily_summary",
                author="系统汇总",
                content=result.get("content") or "",
                summary=(result.get("content") or "")[:200],
                market_direction=result.get("market_direction") or "neutral",
                confidence=int(result.get("confidence") or 50),
                topics=result.get("topics") or [],
                sectors_bullish=result.get("sectors_bullish") or [],
                sectors_bearish=result.get("sectors_bearish") or [],
                reasoning=result.get("reasoning") or "",
                is_summary=True,
                original_count=len(originals),
                original_ids=original_ids,
                credibility_score=75,
                weight=1.0,
                source_authority=1.0,
                time_horizon="short",
                validity_period="7天",
                valid_until=target_date + timedelta(days=7),
                is_deleted=False,
            )
            db.add(summary)
            db.flush()
            db.query(CrawlerArticleRecord).filter(
                CrawlerArticleRecord.viewpoint_id.in_(original_ids)
            ).update({CrawlerArticleRecord.viewpoint_id: summary.id}, synchronize_session=False)
            deleted = db.query(Viewpoint).filter(Viewpoint.id.in_(original_ids)).delete(synchronize_session=False)
            db.commit()
            db.refresh(summary)
            return {
                "success": True,
                "summary_id": summary.id,
                "deleted_originals": deleted,
                "crawler_records_relinked": db.query(CrawlerArticleRecord).filter(
                    CrawlerArticleRecord.viewpoint_id == summary.id
                ).count(),
                "already_summarized": False,
            }
        except Exception:
            db.rollback()
            raise

    @classmethod
    def summarize_pending_dates(cls, db: Session) -> Dict[str, Any]:
        dates = [
            row[0]
            for row in db.query(Viewpoint.viewpoint_date).filter(
                Viewpoint.viewpoint_date < date.today(),
                Viewpoint.is_summary.is_(False),
                Viewpoint.is_deleted.is_(False),
            ).distinct().order_by(Viewpoint.viewpoint_date.asc()).all()
        ]
        completed = []
        skipped = []
        for target in dates:
            try:
                completed.append(cls.summarize_date(db, target))
            except ValueError as exc:
                skipped.append({"date": target.isoformat(), "reason": str(exc)})
        return {"success": True, "completed": completed, "skipped": skipped}

    @classmethod
    def run_daily_summary_task(
        cls,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> Dict[str, Any]:
        """创建可审计的每日汇总任务，并同步执行一次。"""
        db = session_factory()
        task = None
        try:
            today = date.today()
            latest = db.query(BatchAnalysisTask).filter(
                BatchAnalysisTask.task_type == "viewpoint_summary",
            ).order_by(BatchAnalysisTask.created_at.desc()).first()
            if latest and latest.created_at and latest.created_at.date() == today and latest.status == "succeeded":
                # 仅当上次实际汇总过(completed 非空)才视为"已完成"; 全被跳过则允许重试
                prev_completed = (latest.result_summary or {}).get("completed") or []
                if prev_completed:
                    return {"success": True, "already_completed": True, "task_id": latest.id, **(latest.result_summary or {})}
                task = latest
            else:
                task = latest if latest and latest.created_at and latest.created_at.date() == today else None
            if task is None:
                task = BatchAnalysisTask(
                    task_type="viewpoint_summary",
                    status="pending",
                    total_count=0,
                    processed_count=0,
                    success_count=0,
                    failed_count=0,
                    processed_ids=[],
                    failed_ids=[],
                    task_params={"run_date": today.isoformat()},
                    result_summary={},
                )
                db.add(task)
                db.commit()
                db.refresh(task)
            task.status = "running"
            task.started_at = task.started_at or datetime.now()
            task.completed_at = None
            db.commit()

            result = cls.summarize_pending_dates(db)
            task = db.get(BatchAnalysisTask, task.id)
            completed = list(result.get("completed") or [])
            skipped = list(result.get("skipped") or [])
            task.total_count = len(completed) + len(skipped)
            task.processed_count = task.total_count
            task.success_count = len(completed)
            task.failed_count = 0
            task.status = "succeeded"
            task.result_summary = result
            task.completed_at = datetime.now()
            db.commit()
            return {"success": True, "task_id": task.id, **result}
        except Exception as exc:
            db.rollback()
            if task is not None:
                task = db.get(BatchAnalysisTask, task.id)
                if task:
                    task.status = "failed"
                    task.failed_count = 1
                    task.error_message = str(exc)
                    task.error_stack = traceback.format_exc()
                    task.completed_at = datetime.now()
                    db.commit()
            return {"success": False, "task_id": task.id if task else None, "error": str(exc)}
        finally:
            db.close()
