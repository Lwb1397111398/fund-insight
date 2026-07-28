"""帖子分析和持久化任务服务。"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from src.analyzer.llm_analyzer import get_analyzer
from src.models.database import AnalysisLog, BatchAnalysisTask, Post, Prediction
from src.utils.fund_matching import match_fund_with_fallback

logger = logging.getLogger(__name__)


class PostAnalysisService:
    """统一单条、批量和后台任务的帖子分析实现。"""

    INTERRUPTED_AFTER = timedelta(minutes=15)

    def __init__(
        self,
        db: Optional[Session] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        analyzer_factory: Callable[[], Any] = get_analyzer,
        fund_auto_manager: Any = None,
    ):
        self.db = db
        self.session_factory = session_factory
        self.analyzer_factory = analyzer_factory
        if fund_auto_manager is None:
            from src.fund.fund_auto_manager import fund_auto_manager as default_manager

            fund_auto_manager = default_manager
        self.fund_auto_manager = fund_auto_manager

    @staticmethod
    def _session_local():
        # 延迟读取，测试和维护脚本可以安全替换 SessionLocal。
        from src.models import database

        return database.SessionLocal

    @staticmethod
    def _normalize_result(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @classmethod
    def _with_meta(
        cls,
        value: Any,
        status: str,
        *,
        error: Optional[str] = None,
        reason: Optional[str] = None,
        task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = cls._normalize_result(value)
        meta = dict(result.get("_meta") or {})
        meta.update({"status": status, "updated_at": datetime.now().isoformat()})
        if error:
            meta["error"] = error[:1000]
        else:
            meta.pop("error", None)
        if reason:
            meta["reason"] = reason
        else:
            meta.pop("reason", None)
        if task_id is not None:
            meta["task_id"] = task_id
        result["_meta"] = meta
        return result

    @staticmethod
    def is_low_quality_post(title: str, content: str) -> tuple[bool, str]:
        if not content:
            return True, "内容为空"

        content = content.strip()
        if len(content) < 30:
            return True, f"内容过短（{len(content)}字符）"

        ad_keywords = (
            "开户", "佣金", "手续费", "返现", "红包", "优惠", "加微信", "加群",
            "私聊", "联系方式", "二维码", "推广", "广告", "合作", "商务", "代理",
            "免费领取", "限时优惠", "点击链接",
        )
        for keyword in ad_keywords:
            if keyword in content.lower():
                return True, f"疑似广告（包含'{keyword}'）"

        chat_keywords = ("早上好", "晚安", "吃饭", "天气", "周末愉快", "节日快乐")
        investment_keywords = (
            "涨", "跌", "买", "卖", "加仓", "减仓", "看涨", "看跌", "板块", "基金",
            "股票", "ETF", "行情", "走势", "预测", "看好", "看空", "震荡", "突破",
            "回调", "反弹",
        )
        if any(word in content for word in chat_keywords) and not any(
            word in content for word in investment_keywords
        ):
            return True, "纯闲聊内容"

        if len(re.sub(r"[^一-龥a-zA-Z0-9]", "", content)) < 10:
            return True, "内容过少（多为表情/符号）"
        return False, ""

    def _claim(self, db: Session, post_id: int, task_id: Optional[int]) -> Dict[str, Any]:
        post = db.query(Post).filter(Post.id == post_id).with_for_update().first()
        if not post:
            return {"proceed": False, "success": False, "status": "failed", "message": "帖子不存在"}

        active_predictions = db.query(Prediction).filter(
            Prediction.post_id == post_id,
            Prediction.is_deleted.is_(False),
        ).count()
        if active_predictions or post.analyzed:
            return {
                "proceed": False,
                "success": True,
                "status": "skipped",
                "message": "帖子已有有效预测，已跳过重复分析",
                "predictions_created": 0,
            }

        existing_meta = self._normalize_result(post.analysis_result).get("_meta") or {}
        if existing_meta.get("status") == "running":
            try:
                updated_at = datetime.fromisoformat(existing_meta.get("updated_at", ""))
            except (TypeError, ValueError):
                updated_at = datetime.now()
            if datetime.now() - updated_at <= self.INTERRUPTED_AFTER:
                return {
                    "proceed": False,
                    "success": True,
                    "status": "skipped",
                    "message": "帖子正在分析，已跳过重复任务",
                    "predictions_created": 0,
                }

        # 不再在调用 LLM 前用本地规则预判低质量：是否为预测内容交由 LLM 判定。
        # LLM 正常返回但无预测的帖子，将在 _persist_success 中自动删除。
        post.analysis_result = self._with_meta(post.analysis_result, "running", task_id=task_id)
        snapshot = {
            "id": post.id,
            "title": post.title or "",
            "content": post.content,
            "post_date": post.post_date,
            "blogger_id": post.blogger_id,
        }
        db.commit()
        return {"proceed": True, "snapshot": snapshot}

    def _build_prediction(self, db: Session, post: Post, pred: Dict[str, Any], analyzer: Any) -> Prediction:
        sector = pred.get("sector", "")
        fund_code, fund_name = match_fund_with_fallback(
            pred=pred,
            sector=sector,
            fund_auto_manager=self.fund_auto_manager,
            llm_analyzer=analyzer,
            db=db,
        )
        period = pred.get("prediction_period", "1周")
        target_date = analyzer.calculate_target_date(post.post_date, period)
        next_verify_date = analyzer.calculate_next_verify_date(post.post_date, target_date)
        return Prediction(
            post_id=post.id,
            blogger_id=post.blogger_id,
            fund_code=fund_code,
            fund_name=fund_name,
            sector=sector,
            sector_type=pred.get(
                "sector_type",
                self.fund_auto_manager.get_category_for_sector(sector) if sector else "其他",
            ),
            prediction_type=pred.get("prediction_type") or "up",
            prediction_content=pred.get("prediction_content"),
            confidence=pred.get("confidence", 50),
            prediction_date=post.post_date,
            prediction_period=period,
            target_date=target_date,
            next_verify_date=next_verify_date,
        )

    def _persist_success(
        self,
        db: Session,
        post_id: int,
        result: Dict[str, Any],
        analyzer: Any,
        task_id: Optional[int],
    ) -> Dict[str, Any]:
        post = db.query(Post).filter(Post.id == post_id).with_for_update().first()
        if not post:
            raise ValueError("帖子不存在")

        existing_count = db.query(Prediction).filter(
            Prediction.post_id == post_id,
            Prediction.is_deleted.is_(False),
        ).count()
        if existing_count or post.analyzed:
            db.rollback()
            return {
                "success": True,
                "status": "skipped",
                "message": "帖子已有有效预测，已跳过重复分析",
                "predictions_created": 0,
            }

        predictions = list(result.get("predictions") or [])
        if not predictions:
            # LLM 正常返回但未提取到任何预测：说明该帖子并非真正的预测内容
            # （可能是公众号心得/经验/日记等分享）。直接删除该帖，避免占用分析队列。
            db.rollback()
            return self._auto_delete_no_prediction(db, post_id, result, analyzer, task_id)

        created_predictions = [
            self._build_prediction(db, post, pred, analyzer) for pred in predictions
        ]
        db.add_all(created_predictions)
        from src.services.fund_service import FundService

        FundService(db).refresh_prediction_counts([
            prediction.fund_code for prediction in created_predictions
        ])

        post.analyzed = True
        post.analysis_result = self._with_meta(result, "succeeded", task_id=task_id)
        db.commit()
        return {
            "success": True,
            "status": "succeeded",
            "message": f"分析完成，创建 {len(predictions)} 个预测",
            "predictions_created": len(predictions),
            "analysis_result": post.analysis_result,
            "llm_model": getattr(analyzer, "model", None),
        }

    def _auto_delete_no_prediction(
        self,
        db: Session,
        post_id: int,
        result: Dict[str, Any],
        analyzer: Any,
        task_id: Optional[int],
    ) -> Dict[str, Any]:
        """LLM 正常返回但无任何预测：判定为非预测内容，自动删除该帖。

        复用 PostService 的级联删除（预测/验证任务/分析日志/观点解关联/博主统计重算）。
        """
        from src.services.post_service import PostService

        delete_info = PostService(db).delete_post_permanently(post_id)
        logger.info(
            "帖子 %s 经 LLM 分析无有效预测，已自动删除（级联：%s）",
            post_id,
            delete_info,
        )
        return {
            "success": True,
            "status": "skipped",
            "message": "无有效预测（非预测内容），帖子已自动删除",
            "predictions_created": 0,
            "auto_deleted": True,
            "reason": "no_prediction",
            "analysis_result": self._with_meta(
                result,
                "skipped",
                reason="无有效预测（非预测内容），帖子已自动删除",
                task_id=task_id,
            ),
            "llm_model": getattr(analyzer, "model", None),
        }

    def _mark_failed(self, db: Session, post_id: int, error: str, task_id: Optional[int]) -> None:
        db.rollback()
        post = db.get(Post, post_id)
        if not post:
            return
        post.analyzed = False
        post.analysis_result = self._with_meta(
            post.analysis_result,
            "failed",
            error=error,
            task_id=task_id,
        )
        db.commit()

    def analyze_post(self, post_id: int, task_id: Optional[int] = None) -> Dict[str, Any]:
        """分析单条帖子；后台模式会在 LLM 调用前关闭读取会话。"""
        session_factory = self.session_factory or (None if self.db is not None else self._session_local())
        claim_db = self.db or session_factory()
        try:
            claim = self._claim(claim_db, post_id, task_id)
        finally:
            if self.db is None:
                claim_db.close()
        if not claim.get("proceed"):
            return claim

        analyzer = self.analyzer_factory()
        snapshot = claim["snapshot"]
        try:
            result = analyzer.analyze_post(
                title=snapshot["title"],
                content=snapshot["content"],
                post_date=snapshot["post_date"].isoformat() if snapshot["post_date"] else None,
            )
            write_db = self.db or session_factory()
            try:
                return self._persist_success(write_db, post_id, result, analyzer, task_id)
            except Exception:
                write_db.rollback()
                raise
            finally:
                if self.db is None:
                    write_db.close()
        except Exception as exc:
            failure_db = self.db or session_factory()
            try:
                self._mark_failed(failure_db, post_id, str(exc), task_id)
            finally:
                if self.db is None:
                    failure_db.close()
            return {
                "success": False,
                "status": "failed",
                "message": f"分析失败: {exc}",
                "predictions_created": 0,
                "error": str(exc),
                "llm_model": getattr(analyzer, "model", None),
            }

    @classmethod
    def create_job(
        cls,
        db: Session,
        post_ids: Optional[Iterable[int]] = None,
        limit: int = 100,
    ) -> tuple[BatchAnalysisTask, bool]:
        """创建帖子分析任务；同一时间只保留一个可运行任务。"""
        now = datetime.now()
        active_tasks = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.task_type == "posts",
            BatchAnalysisTask.status.in_(("pending", "running")),
        ).order_by(BatchAnalysisTask.created_at.desc()).all()
        for task in active_tasks:
            if task.status == "running" and task.updated_at and now - task.updated_at > cls.INTERRUPTED_AFTER:
                task.status = "pending"
                task.error_message = "检测到 Render 中断，可恢复执行"
                task.completed_at = None
                db.commit()
                return task, False
            return task, False

        limit = max(1, min(int(limit or 100), 1000))
        if post_ids is None:
            selected_ids = []
            candidates = db.query(Post).filter(
                Post.analyzed.is_(False)
            ).order_by(Post.created_at.desc()).yield_per(200)
            for candidate in candidates:
                meta = cls._normalize_result(candidate.analysis_result).get("_meta") or {}
                if meta.get("status") in ("failed", "skipped", "running", "succeeded"):
                    continue
                selected_ids.append(candidate.id)
                if len(selected_ids) >= limit:
                    break
        else:
            requested = list(dict.fromkeys(int(post_id) for post_id in post_ids))[:limit]
            existing = {
                row[0]
                for row in db.query(Post.id).filter(Post.id.in_(requested)).all()
            } if requested else set()
            selected_ids = [post_id for post_id in requested if post_id in existing]

        task = BatchAnalysisTask(
            task_type="posts",
            status="pending",
            total_count=len(selected_ids),
            processed_count=0,
            success_count=0,
            failed_count=0,
            processed_ids=[],
            failed_ids=[],
            task_params={"limit": limit, "post_ids": selected_ids},
            result_summary={"analyzed": 0, "failed": 0, "skipped": 0, "total": len(selected_ids)},
        )
        db.add(task)
        db.flush()
        for post in db.query(Post).filter(Post.id.in_(selected_ids)).all() if selected_ids else []:
            meta = cls._normalize_result(post.analysis_result).get("_meta") or {}
            if meta.get("status") not in ("running", "succeeded"):
                post.analysis_result = cls._with_meta(post.analysis_result, "pending", task_id=task.id)
        db.commit()
        db.refresh(task)
        return task, True

    @classmethod
    def resume_job(cls, db: Session, task_id: int) -> BatchAnalysisTask:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == task_id,
            BatchAnalysisTask.task_type == "posts",
        ).with_for_update().first()
        if not task:
            raise ValueError("任务不存在")
        if task.status == "running" and task.updated_at and datetime.now() - task.updated_at <= cls.INTERRUPTED_AFTER:
            raise ValueError("任务仍在运行")

        processed_ids = list(task.processed_ids or [])
        task.status = "pending"
        task.processed_ids = processed_ids
        task.failed_ids = []
        task.processed_count = len(processed_ids)
        task.failed_count = 0
        task.completed_at = None
        task.error_message = None
        task.error_stack = None
        summary = dict(task.result_summary or {})
        summary["failed"] = 0
        task.result_summary = summary
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def cancel_job(db: Session, task_id: int) -> BatchAnalysisTask:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == task_id,
            BatchAnalysisTask.task_type == "posts",
        ).with_for_update().first()
        if not task:
            raise ValueError("任务不存在")
        if task.status in ("succeeded", "failed"):
            raise ValueError("任务已结束，无法取消")
        task.status = "cancelled"
        task.completed_at = datetime.now()
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def serialize_job(task: BatchAnalysisTask) -> Dict[str, Any]:
        total = task.total_count or 0
        processed = task.processed_count or 0
        return {
            "task_id": task.id,
            "status": task.status,
            "total_count": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "progress": (processed / total * 100) if total else 0,
            "processed_ids": list(task.processed_ids or []),
            "failed_ids": list(task.failed_ids or []),
            "result_summary": dict(task.result_summary or {}),
            "error_message": task.error_message,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }

    @classmethod
    def run_job(
        cls,
        task_id: int,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        analyzer_factory: Optional[Callable[[], Any]] = None,
        fund_auto_manager: Any = None,
    ) -> None:
        """运行任务。每次数据库操作都使用独立短会话。"""
        session_factory = session_factory or cls._session_local()
        # 延迟求值，便于测试 monkeypatch get_analyzer
        analyzer_factory = analyzer_factory or get_analyzer
        db = session_factory()
        try:
            task = db.query(BatchAnalysisTask).filter(
                BatchAnalysisTask.id == task_id,
                BatchAnalysisTask.task_type == "posts",
            ).with_for_update().first()
            if not task or task.status in ("cancelled", "succeeded", "failed"):
                return
            if (
                task.status == "running"
                and task.updated_at
                and datetime.now() - task.updated_at <= cls.INTERRUPTED_AFTER
            ):
                return
            task.status = "running"
            task.started_at = task.started_at or datetime.now()
            task.completed_at = None
            task.error_message = None
            post_ids = list((task.task_params or {}).get("post_ids") or [])
            task.total_count = len(post_ids)
            db.commit()
        finally:
            db.close()

        try:
            for post_id in post_ids:
                state_db = session_factory()
                try:
                    task = state_db.get(BatchAnalysisTask, task_id)
                    if not task or task.status == "cancelled":
                        return
                    if post_id in set(task.processed_ids or []):
                        continue
                finally:
                    state_db.close()

                started = time.monotonic()
                result = cls(
                    session_factory=session_factory,
                    analyzer_factory=analyzer_factory,
                    fund_auto_manager=fund_auto_manager,
                ).analyze_post(post_id, task_id=task_id)

                progress_db = session_factory()
                try:
                    task = progress_db.query(BatchAnalysisTask).filter(
                        BatchAnalysisTask.id == task_id
                    ).with_for_update().first()
                    if not task:
                        return

                    processed_ids = list(task.processed_ids or [])
                    failed_ids = [
                        item for item in list(task.failed_ids or [])
                        if int(item.get("id", -1)) != post_id
                    ]
                    summary = dict(task.result_summary or {})
                    status = result.get("status")
                    if status in ("succeeded", "skipped"):
                        if post_id not in processed_ids:
                            processed_ids.append(post_id)
                            if status == "succeeded":
                                task.success_count = (task.success_count or 0) + 1
                                summary["analyzed"] = (summary.get("analyzed") or 0) + 1
                            else:
                                summary["skipped"] = (summary.get("skipped") or 0) + 1
                    else:
                        failed_ids.append({"id": post_id, "error": result.get("error") or result.get("message")})

                    task.processed_ids = processed_ids
                    task.failed_ids = failed_ids
                    task.failed_count = len(failed_ids)
                    task.processed_count = len(processed_ids) + len(failed_ids)
                    summary.update({
                        "failed": len(failed_ids),
                        "total": task.total_count,
                    })
                    task.result_summary = summary
                    log_post_id = post_id if progress_db.get(Post, post_id) is not None else None
                    progress_db.add(AnalysisLog(
                        task_id=task_id,
                        post_id=log_post_id,
                        llm_model=result.get("llm_model"),
                        llm_response=json.dumps(result.get("analysis_result"), ensure_ascii=False)
                        if result.get("analysis_result") else None,
                        parse_success=status in ("succeeded", "skipped"),
                        parse_method=status,
                        parse_error=result.get("error"),
                        analysis_duration=time.monotonic() - started,
                    ))
                    progress_db.commit()
                finally:
                    progress_db.close()

            final_db = session_factory()
            try:
                task = final_db.query(BatchAnalysisTask).filter(
                    BatchAnalysisTask.id == task_id
                ).with_for_update().first()
                if task and task.status != "cancelled":
                    task.status = "failed" if task.failed_count else "succeeded"
                    task.completed_at = datetime.now()
                    final_db.commit()
            finally:
                final_db.close()
        except Exception as exc:
            logger.exception("帖子分析任务 %s 执行失败", task_id)
            failure_db = session_factory()
            try:
                task = failure_db.get(BatchAnalysisTask, task_id)
                if task and task.status != "cancelled":
                    task.status = "failed"
                    task.error_message = str(exc)
                    task.error_stack = traceback.format_exc()
                    task.completed_at = datetime.now()
                    failure_db.commit()
            finally:
                failure_db.close()
