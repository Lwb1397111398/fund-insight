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


DEFAULT_SOURCES = ("eastmoney_blog", "eastmoney_guide", "sina_finance")
OPTIONAL_SOURCES = ("sina_blog",)
ALLOWED_SOURCES = frozenset(DEFAULT_SOURCES + OPTIONAL_SOURCES)


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
        limit_per_source: int = 15,
    ) -> Tuple[BatchAnalysisTask, bool]:
        selected = cls._normalize_sources(sources)
        limit_per_source = max(1, min(int(limit_per_source or 15), 50))
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
            task_params={"sources": selected, "limit_per_source": limit_per_source},
            result_summary={"sources": source_stats, "adopted": 0, "duplicates": 0, "skipped": 0},
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task, True

    @staticmethod
    def _default_fetchers() -> Dict[str, Callable[[int], List[Dict[str, Any]]]]:
        from src.crawler.eastmoney_blog_crawler import crawler as eastmoney_blog
        from src.crawler.eastmoney_guide_crawler import get_guide_crawler
        from src.crawler.sina_finance_crawler import get_sina_crawler
        from src.crawler.sina_blog_crawler import get_blog_crawler

        return {
            "eastmoney_blog": lambda limit: eastmoney_blog.fetch_hot_articles(max_articles=limit),
            "eastmoney_guide": lambda limit: get_guide_crawler().fetch_guide_articles(max_articles=limit),
            "sina_finance": lambda limit: get_sina_crawler().fetch_articles(category="finance", num=limit),
            "sina_blog": lambda limit: get_blog_crawler().fetch_blog_posts(max_posts=limit),
        }

    @staticmethod
    def _default_capture_analyzer(article: Dict[str, Any], source: str) -> Tuple[bool, Dict[str, Any]]:
        from src.analyzer.post_analyzer import PostAnalyzer as EnhancedAIAnalyzer
        from src.services.crawler_service import CrawlerService

        analyzer = EnhancedAIAnalyzer()
        db = SessionLocal()
        try:
            helper = CrawlerService(db)
            return helper._analyze_article(
                analyzer,
                str(article.get("title") or ""),
                str(article.get("content") or ""),
                source=source,
            )
        finally:
            db.close()

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
        capture_analyzer: Optional[Callable[[Dict[str, Any], str], Tuple[bool, Dict[str, Any]]]] = None,
        deep_analyzer: Optional[Callable[[Dict[str, Any], str], Dict[str, Any]]] = None,
    ) -> None:
        fetchers = fetchers or cls._default_fetchers()
        capture_analyzer = capture_analyzer or cls._default_capture_analyzer
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
            limit = max(1, min(int(params.get("limit_per_source") or 15), 50))
            for source in sources:
                task = db.get(BatchAnalysisTask, task_id)
                if not task or task.status == "cancelled":
                    return
                summary = dict(task.result_summary or {})
                source_stats = dict((summary.get("sources") or {}).get(source) or {})
                try:
                    articles = list(fetchers[source](limit) or [])
                    source_stats["fetched"] = len(articles)
                    task.total_count = (task.total_count or 0) + len(articles)
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()
                except Exception as exc:
                    source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                    source_stats["error"] = str(exc)
                    failures = list(task.failed_ids or [])
                    failures.append({"source": source, "error": str(exc)})
                    task.failed_ids = failures
                    task.failed_count = len(failures)
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()
                    continue

                for article in articles:
                    try:
                        cls._process_article(
                            db,
                            task_id,
                            source,
                            article,
                            capture_analyzer,
                            deep_analyzer,
                        )
                    except Exception as exc:
                        db.rollback()
                        task = db.get(BatchAnalysisTask, task_id)
                        summary = dict(task.result_summary or {})
                        source_stats = dict((summary.get("sources") or {}).get(source) or {})
                        source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                        failures = list(task.failed_ids or [])
                        failures.append({
                            "article_id": cls._stable_article_id(source, article),
                            "source": source,
                            "error": str(exc),
                        })
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
    def _process_article(cls, db, task_id, source, article, capture_analyzer, deep_analyzer):
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

        should_capture, capture = capture_analyzer(article, source)
        record = CrawlerArticleRecord(
            article_id=article_id,
            source=source,
            title=str(article.get("title") or "")[:500],
            content_hash=cls._content_hash(article),
            url=article.get("url") or article.get("link"),
            author=article.get("author"),
            is_adopted=False,
            capture_score=float(capture.get("score") or 0),
        )
        db.add(record)
        if not should_capture:
            record.skip_reason = str(capture.get("reason") or "未达到采纳阈值")[:200]
            source_stats["skipped"] = (source_stats.get("skipped") or 0) + 1
            summary["skipped"] = (summary.get("skipped") or 0) + 1
            task.processed_count = (task.processed_count or 0) + 1
            processed = list(task.processed_ids or [])
            processed.append(article_id)
            task.processed_ids = processed
            cls._save_source_stats(task, summary, source, source_stats)
            db.commit()
            return

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

        try:
            analysis = deep_analyzer(article, source)
            cls._apply_deep_analysis(viewpoint, analysis)
            source_stats["adopted"] = (source_stats.get("adopted") or 0) + 1
            summary["adopted"] = (summary.get("adopted") or 0) + 1
            task.success_count = (task.success_count or 0) + 1
            processed = list(task.processed_ids or [])
            processed.append(article_id)
            task.processed_ids = processed
        except Exception as exc:
            viewpoint.analysis_summary = f"failed:{str(exc)[:180]}"
            source_stats["failed"] = (source_stats.get("failed") or 0) + 1
            failures = list(task.failed_ids or [])
            failures.append({"article_id": article_id, "viewpoint_id": viewpoint.id, "source": source, "error": str(exc)})
            task.failed_ids = failures
            task.failed_count = len(failures)
        task.processed_count = (task.processed_count or 0) + 1
        cls._save_source_stats(task, summary, source, source_stats)
        db.commit()

    @classmethod
    def _run_deep_retries(cls, db, task, viewpoint_ids, deep_analyzer):
        failures = []
        for viewpoint_id in viewpoint_ids:
            viewpoint = db.get(Viewpoint, viewpoint_id)
            if not viewpoint:
                continue
            article = {
                "title": (viewpoint.summary or viewpoint.content or "")[:100],
                "content": viewpoint.content,
                "author": viewpoint.author,
                "publish_time": viewpoint.viewpoint_date,
                "url": viewpoint.article_url,
            }
            try:
                viewpoint.analysis_summary = "pending"
                cls._apply_deep_analysis(viewpoint, deep_analyzer(article, viewpoint.source))
                task.success_count = (task.success_count or 0) + 1
            except Exception as exc:
                viewpoint.analysis_summary = f"failed:{str(exc)[:180]}"
                failures.append({"viewpoint_id": viewpoint.id, "source": viewpoint.source, "error": str(exc)})
            task.processed_count = (task.processed_count or 0) + 1
            db.commit()
        task.failed_ids = failures
        task.failed_count = len(failures)
        task.status = "failed" if failures else "succeeded"
        task.completed_at = datetime.now()
        params = dict(task.task_params or {})
        params.pop("retry_viewpoint_ids", None)
        task.task_params = params
        db.commit()

    @staticmethod
    def retry_task(db: Session, task_id: int) -> BatchAnalysisTask:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == task_id,
            BatchAnalysisTask.task_type.in_(("viewpoint_fetch", "viewpoint_summary")),
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
        return {
            "task_id": task.id,
            "task_type": task.task_type,
            "status": "succeeded" if task.status == "completed" else task.status,
            "total_count": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "progress": round(processed / total * 100, 1) if total else 0,
            "result_summary": dict(task.result_summary or {}),
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
                return {"success": True, "already_completed": True, "task_id": latest.id, **(latest.result_summary or {})}
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
