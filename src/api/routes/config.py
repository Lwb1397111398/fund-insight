"""
配置路由
处理配置相关的 API 请求
"""
import os
import json
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Any, Optional
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.core.config import config
from src.core.safety import destructive_cleanup_enabled
from src.api.deps import get_db
from src.models.database import (
    Prediction, Viewpoint, Post, FundInfo, Blogger,
    SectorAlias, SectorFundMapping
)
from src.services.data_portability_service import DataPortabilityService
from src.fund.fund_api import fund_api

router = APIRouter(prefix="/config", tags=["配置"])


def _require_destructive_cleanup(request: Request) -> None:
    """批量清理必须由隔离维护环境显式开启并二次确认。"""
    if not destructive_cleanup_enabled():
        raise HTTPException(
            status_code=403,
            detail="数据清理接口已禁用。请将 ENABLE_DATA_CLEANUP 设为 true（或删除该变量以使用默认开启）后再使用",
        )
    if request.headers.get("X-Danger-Confirm") != "cleanup-data":
        raise HTTPException(status_code=403, detail="缺少数据清理确认头")


class ConfigUpdate(BaseModel):
    """配置更新请求"""
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_light_model: Optional[str] = None
    llm_provider: Optional[str] = None
    volcengine_api_key: Optional[str] = None
    volcengine_model: Optional[str] = None
    volcengine_light_model: Optional[str] = None


class CleanupExecuteRequest(BaseModel):
    preview_fingerprint: str


class ThreeBucketExecuteRequest(BaseModel):
    preview_fingerprint: str
    buckets: Optional[list[str]] = None


@router.get("")
def get_config():
    """获取配置信息"""
    return {
        "success": True,
        "data": {
            "llm_provider": config.LLM_PROVIDER,
            "llm_api_key_set": bool(config.LLM_API_KEY),
            "llm_base_url": config.LLM_BASE_URL,
            "llm_model": config.LLM_MODEL,
            "llm_light_model": config.LLM_LIGHT_MODEL,
            "llm_strategy": config.LLM_STRATEGY,
            "volcengine_api_key_set": bool(config.VOLCENGINE_API_KEY),
            "volcengine_base_url": config.VOLCENGINE_BASE_URL,
            "volcengine_model": config.VOLCENGINE_MODEL,
            "volcengine_light_model": config.VOLCENGINE_LIGHT_MODEL,
            "server_host": config.SERVER_HOST,
            "server_port": config.SERVER_PORT,
            "crawler_enabled": config.CRAWLER_ENABLED,
            "crawler_request_delay": config.CRAWLER_REQUEST_DELAY,
            "max_posts_per_fund": config.MAX_POSTS_PER_FUND,
            "crawler_timeout": config.CRAWLER_TIMEOUT
        }
    }


@router.post("")
def update_config(config_update: ConfigUpdate):
    """更新配置"""
    updated = []
    
    if config_update.llm_provider:
        config.LLM_PROVIDER = config_update.llm_provider
        updated.append("llm_provider")
    
    if config_update.llm_api_key:
        config.LLM_API_KEY = config_update.llm_api_key
        updated.append("llm_api_key")
    
    if config_update.llm_base_url:
        config.LLM_BASE_URL = config_update.llm_base_url
        updated.append("llm_base_url")
    
    if config_update.llm_model:
        config.LLM_MODEL = config_update.llm_model
        updated.append("llm_model")
    
    if config_update.llm_light_model:
        config.LLM_LIGHT_MODEL = config_update.llm_light_model
        updated.append("llm_light_model")
    
    if config_update.volcengine_api_key:
        config.VOLCENGINE_API_KEY = config_update.volcengine_api_key
        updated.append("volcengine_api_key")
    
    if config_update.volcengine_model:
        config.VOLCENGINE_MODEL = config_update.volcengine_model
        updated.append("volcengine_model")
    
    if config_update.volcengine_light_model:
        config.VOLCENGINE_LIGHT_MODEL = config_update.volcengine_light_model
        updated.append("volcengine_light_model")
    
    if updated:
        from src.analyzer.llm_analyzer import reset_analyzer
        reset_analyzer()
        config.save_persisted_config()

    return {
        "success": True,
        "message": f"已更新配置: {', '.join(updated)}" if updated else "无更新",
        "data": {
            "updated_fields": updated
        }
    }


def _set_cleanup_task_progress(task_id: str, processed: int, total: int, category: str):
    from src.models.database import CleanupTask, SessionLocal

    db = SessionLocal()
    try:
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if task:
            task.current_item = processed
            task.total_items = total
            task.progress = 100 if total == 0 else min(100, int(processed * 100 / total))
            task.cleanup_params = {**(task.cleanup_params or {}), "current_category": category}
            db.commit()
    finally:
        db.close()


def _run_cleanup_background(
    task_id: str,
    fingerprint: str,
    categories: Optional[list[str]] = None,
):
    from src.models.database import CleanupTask, SessionLocal
    from src.services.retention_cleanup_service import (
        CleanupPlanChanged,
        RetentionCleanupService,
    )

    db = SessionLocal()
    try:
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if not task:
            return
        task.status = "running"
        task.started_at = datetime.now()
        db.commit()
        result = RetentionCleanupService(db).execute(
            expected_fingerprint=fingerprint,
            categories=set(categories) if categories else None,
            progress_callback=lambda done, total, category: _set_cleanup_task_progress(
                task_id, done, total, category
            ),
        )
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        task.status = "completed" if result["success"] else "partial"
        task.progress = 100
        task.current_item = task.total_items
        task.result = result
        task.completed_at = datetime.now()
        db.commit()
    except CleanupPlanChanged as exc:
        db.rollback()
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if task:
            task.status = "failed"
            task.error = f"预览已过期，请刷新后重试。当前指纹: {exc.current_fingerprint}"
            task.completed_at = datetime.now()
            db.commit()
    except Exception as exc:
        db.rollback()
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if task:
            task.status = "failed"
            task.error = str(exc)
            task.completed_at = datetime.now()
            db.commit()
    finally:
        db.close()


def _queue_cleanup(
    request: Request,
    payload: Optional[CleanupExecuteRequest],
    background_tasks: BackgroundTasks,
    db: Session,
    categories: Optional[set[str]] = None,
):
    from src.models.database import CleanupTask
    from src.services.retention_cleanup_service import (
        HARD_DELETE_DISABLED,
        HARD_DELETE_DISABLED_REASON,
        RetentionCleanupService,
    )

    # 旧执行器硬删下线：先走统一开关/确认头校验，再给出改道提示（preview 仍可用）
    _require_destructive_cleanup(request)
    if HARD_DELETE_DISABLED:
        raise HTTPException(
            status_code=403,
            detail={
                "message": HARD_DELETE_DISABLED_REASON,
                "use": "POST /api/config/cleanup/three-buckets",
                "hard_delete_disabled": True,
            },
        )

    if payload is None or not payload.preview_fingerprint:
        raise HTTPException(status_code=400, detail="必须携带预览指纹")
    plan = RetentionCleanupService(db).build_plan()
    if plan.fingerprint != payload.preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "预览已过期，请刷新后重试",
                "current_fingerprint": plan.fingerprint,
            },
        )

    task_id = str(uuid.uuid4())
    selected_categories = categories or set(plan.candidate_ids)
    selected_total = sum(len(plan.candidate_ids[name]) for name in selected_categories)
    task = CleanupTask(
        task_id=task_id,
        status="pending",
        progress=0,
        current_item=0,
        total_items=selected_total,
        cleanup_types=sorted(selected_categories),
        cleanup_params={
            "preview_fingerprint": plan.fingerprint,
            "rule_version": "retention-v2",
        },
    )
    db.add(task)
    db.commit()
    background_tasks.add_task(
        _run_cleanup_background,
        task_id,
        plan.fingerprint,
        sorted(selected_categories),
    )
    return {
        "success": True,
        "message": "安全清理任务已创建",
        "data": {"task_id": task_id, "status": "pending", "total_items": selected_total},
    }


@router.post("/cleanup")
def run_cleanup(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Optional[CleanupExecuteRequest] = None,
    db: Session = Depends(get_db),
):
    return _queue_cleanup(request, payload, background_tasks, db)


@router.post("/cleanup/oldest")
def cleanup_oldest_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Optional[CleanupExecuteRequest] = None,
    db: Session = Depends(get_db),
):
    """兼容旧入口，但不允许绕过统一保留规则和预览指纹。"""
    return _queue_cleanup(request, payload, background_tasks, db)


@router.get("/cleanup/orphan-funds/preview")
def preview_orphan_funds(db: Session = Depends(get_db)):
    from src.services.retention_cleanup_service import RetentionCleanupService

    plan = RetentionCleanupService(db).build_plan()
    return {
        "success": True,
        "message": f"发现 {len(plan.candidate_ids['funds'])} 个可清理的孤儿基金",
        "data": {
            "preview_fingerprint": plan.fingerprint,
            "orphan_funds": plan.samples["funds"],
            "total_orphans": len(plan.candidate_ids["funds"]),
            "protected_funds": plan.protected_counts["protected_funds"],
        },
    }


@router.post("/cleanup/orphan-funds")
def cleanup_orphan_funds(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Optional[CleanupExecuteRequest] = None,
    db: Session = Depends(get_db),
):
    return _queue_cleanup(
        request,
        payload,
        background_tasks,
        db,
        categories={"funds"},
    )


@router.get("/cleanup/preview")
def get_cleanup_preview(db: Session = Depends(get_db)):
    from src.services.retention_cleanup_service import POLICY_VERSION, RetentionCleanupService

    plan = RetentionCleanupService(db).build_plan()
    samples = plan.samples
    return {
        "success": True,
        "data": {
            "cleanup_enabled": destructive_cleanup_enabled(),
            "rule_version": POLICY_VERSION,
            "generated_at": plan.generated_at.isoformat(),
            "preview_fingerprint": plan.fingerprint,
            "policy": plan.policy.to_dict(),
            "counts": {
                category: len(ids) for category, ids in plan.candidate_ids.items()
            },
            "total": plan.total_candidates,
            "samples": samples,
            # 兼容旧前端字段（样本预览，最多 20 条）
            "predictions": samples.get("predictions", []),
            "viewpoints": samples.get("viewpoints", []),
            "posts": samples.get("posts", []),
            "funds": samples.get("funds", []),
            "bloggers": samples.get("bloggers", []),
            "summary": {"total": plan.total_candidates},
            "protected_counts": plan.protected_counts,
            "health_warnings": plan.health_warnings,
        },
    }


def _three_bucket_preview_payload(db: Session) -> dict:
    from src.services.retention_three_buckets import (
        POLICY_NAME,
        ThreeBucketRetentionService,
    )

    service = ThreeBucketRetentionService(db)
    plan = service.build_plan()
    counts = {name: len(ids) for name, ids in plan.candidate_ids.items()}
    cascade = service.estimate_cascade_rows(plan)
    return {
        "cleanup_enabled": destructive_cleanup_enabled(),
        "rule_version": POLICY_NAME,
        "as_of": plan.as_of.isoformat(),
        "preview_fingerprint": plan.fingerprint,
        "policy": plan.policy.to_dict(),
        "counts": counts,
        "total": plan.total,
        # 连带删除（删基金时跟着走的净值/重试行），不计入 total 但会真的消失
        "cascade_counts": cascade,
        "total_rows_removed": plan.total + sum(cascade.values()),
        "truncated_by_global_cap": plan.truncated,
        "protected_counts": plan.protected_counts,
        "labels": dict(ThreeBucketRetentionService.BUCKET_LABELS),
        "notes": plan.notes,
        "samples": plan.samples,
        "table_sizes": service.table_sizes(),
    }


@router.post("/cleanup/reclaim-space")
def reclaim_database_space(request: Request, db: Session = Depends(get_db)):
    """单独对大表回收磁盘空间（Postgres VACUUM FULL / SQLite VACUUM）。

    用于「之前已经删过但空间没还」的情况；本身不删任何数据。
    """
    from src.services.db_space import format_bytes, reclaim_space
    from src.services.retention_three_buckets import ThreeBucketRetentionService

    _require_destructive_cleanup(request)
    tables = sorted(
        {
            table
            for buckets in ThreeBucketRetentionService.BUCKET_TABLES.values()
            for table in buckets
        }
    )
    result = reclaim_space(db, tables)
    freed = result.get("bytes_freed")
    return {
        "success": bool(result.get("success", False)) or bool(result.get("skipped")),
        "message": (
            f"空间回收完成，释放 {format_bytes(freed)}"
            if freed
            else "空间回收完成（本次未释放可测量空间）"
        ),
        "data": result,
    }


def _run_three_bucket_background(
    task_id: str,
    fingerprint: str,
    buckets: Optional[list[str]] = None,
):
    from src.models.database import CleanupTask, SessionLocal
    from src.services.retention_three_buckets import (
        CONFIRM_TOKEN,
        BucketPlanChanged,
        ThreeBucketRetentionService,
    )

    db = SessionLocal()
    try:
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if not task:
            return
        task.status = "running"
        task.started_at = datetime.now()
        db.commit()
        result = ThreeBucketRetentionService(db).execute(
            dry_run=False,
            confirm_token=CONFIRM_TOKEN,
            expected_fingerprint=fingerprint,
            buckets=set(buckets) if buckets else None,
            progress_callback=lambda done, total, bucket: _set_cleanup_task_progress(
                task_id, done, total, bucket
            ),
        )
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        task.status = "completed"
        task.progress = 100
        task.current_item = task.total_items
        task.result = {
            "success": True,
            "total_deleted": result.get("total_deleted", 0),
            "total_rows_removed": result.get("total_rows_removed", 0),
            "deleted_counts": result.get("deleted_counts", {}),
            "cascade_counts": result.get("cascade_counts", {}),
            "protected_counts": result.get("protected_counts", {}),
            "truncated_by_global_cap": result.get("truncated_by_global_cap", False),
            "space_reclaim": result.get("space_reclaim"),
            "cleanup_log_id": result.get("cleanup_log_id"),
        }
        task.completed_at = datetime.now()
        db.commit()
    except BucketPlanChanged as exc:
        db.rollback()
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if task:
            task.status = "failed"
            task.error = f"预览已过期，请刷新后重试。当前指纹: {exc.current_fingerprint}"
            task.completed_at = datetime.now()
            db.commit()
    except Exception as exc:
        db.rollback()
        task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
        if task:
            task.status = "failed"
            task.error = str(exc)
            task.completed_at = datetime.now()
            db.commit()
    finally:
        db.close()


@router.get("/cleanup/three-buckets/preview")
def preview_three_bucket_cleanup(db: Session = Depends(get_db)):
    """三桶保留策略只读预览（唯一在线硬删路径的预览入口）。"""
    return {"success": True, "data": _three_bucket_preview_payload(db)}


@router.post("/cleanup/three-buckets")
def run_three_bucket_cleanup(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: ThreeBucketExecuteRequest,
    db: Session = Depends(get_db),
):
    """按三桶策略执行受控硬删：需清理开关 + 确认头 + 未过期预览指纹。"""
    from src.models.database import CleanupTask
    from src.services.retention_three_buckets import ThreeBucketRetentionService

    _require_destructive_cleanup(request)

    service = ThreeBucketRetentionService(db)
    plan = service.build_plan()
    if plan.fingerprint != payload.preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "预览已过期，请刷新后重试",
                "current_fingerprint": plan.fingerprint,
            },
        )

    selected = set(payload.buckets) if payload.buckets else set(service.BUCKETS)
    unknown = selected - set(service.BUCKETS)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"未知清理桶: {sorted(unknown)}"
        )
    selected_total = sum(len(plan.candidate_ids.get(name, [])) for name in selected)
    if selected_total == 0:
        return {
            "success": True,
            "message": "当前没有可清理资料",
            "data": {"task_id": None, "status": "completed", "total_items": 0},
        }

    task_id = str(uuid.uuid4())
    task = CleanupTask(
        task_id=task_id,
        status="pending",
        progress=0,
        current_item=0,
        total_items=selected_total,
        cleanup_types=sorted(selected),
        cleanup_params={
            "preview_fingerprint": plan.fingerprint,
            "rule_version": "three-buckets-v2",
        },
    )
    db.add(task)
    db.commit()
    background_tasks.add_task(
        _run_three_bucket_background,
        task_id,
        plan.fingerprint,
        sorted(selected),
    )
    return {
        "success": True,
        "message": "三桶清理任务已创建",
        "data": {"task_id": task_id, "status": "pending", "total_items": selected_total},
    }


def _mark_cleanup_task_stale_if_needed(task, now: datetime) -> bool:
    if task.status == "pending":
        reference = task.created_at
        timeout = timedelta(minutes=5)
    elif task.status == "running":
        reference = task.started_at or task.created_at
        timeout = timedelta(minutes=30)
    else:
        return False
    if reference and now - reference > timeout:
        task.status = "failed"
        task.error = "清理任务因服务中断或执行超时而停止，请刷新预览后重新执行"
        task.completed_at = now
        return True
    return False


@router.get("/cleanup/tasks/{task_id}")
def get_cleanup_task(task_id: str, db: Session = Depends(get_db)):
    from src.models.database import CleanupTask

    task = db.query(CleanupTask).filter(CleanupTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="清理任务不存在")
    if _mark_cleanup_task_stale_if_needed(task, datetime.now()):
        db.commit()
    return {
        "success": True,
        "data": {
            "task_id": task.task_id,
            "status": task.status,
            "progress": task.progress,
            "current_item": task.current_item,
            "total_items": task.total_items,
            "current_category": (task.cleanup_params or {}).get("current_category"),
            "result": task.result,
            "error": task.error,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        },
    }


@router.get("/cleanup/preview-legacy", include_in_schema=False)
async def get_cleanup_preview_legacy(db: Session = Depends(get_db)):
    return await get_cleanup_preview(db)
    """
    获取可清理数据预览
    
    返回真正满足清理条件的数据：
    - 过期预测：target_date < 今天-7天
    - 过期观点：viewpoint_date < 今天-10天
    - 空帖子：没有任何预测的帖子
    - 无用基金：没有预测关联的基金
    - 无用博主：没有任何预测的博主
    """
    try:
        today = date.today()
        
        cutoff_7_days = today - timedelta(days=7)
        expired_predictions = db.query(Prediction).filter(
            Prediction.target_date < cutoff_7_days,
            Prediction.is_deleted == False
        ).all()
        
        # 批量加载所有需要的 blogger，避免 N+1 查询
        blogger_ids = list(set(p.blogger_id for p in expired_predictions if p.blogger_id))
        bloggers_map = {b.id: b for b in db.query(Blogger).filter(Blogger.id.in_(blogger_ids)).all()} if blogger_ids else {}

        predictions_list = []
        for p in expired_predictions:
            blogger = bloggers_map.get(p.blogger_id)
            predictions_list.append({
                "id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": blogger.name if blogger else "-",
                "sector": p.sector,
                "prediction_content": p.prediction_content,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "is_correct": p.is_correct
            })
        
        cutoff_10_days = today - timedelta(days=10)
        expired_viewpoints = db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date < cutoff_10_days
        ).all()
        
        viewpoints_list = []
        for v in expired_viewpoints:
            viewpoints_list.append({
                "id": v.id,
                "source": v.source,
                "author": v.author,
                "content": v.content,
                "valid_until": v.valid_until.isoformat() if v.valid_until else None
            })
        
        posts_with_predictions = db.query(Prediction.post_id).filter(
            Prediction.is_deleted == False
        ).distinct().subquery()
        
        empty_posts = db.query(Post).filter(
            ~Post.id.in_(posts_with_predictions)
        ).all()
        
        # 批量加载所有需要的 blogger，避免 N+1 查询
        post_blogger_ids = list(set(p.blogger_id for p in empty_posts if p.blogger_id))
        post_bloggers_map = {b.id: b for b in db.query(Blogger).filter(Blogger.id.in_(post_blogger_ids)).all()} if post_blogger_ids else {}

        posts_list = []
        for p in empty_posts:
            blogger = post_bloggers_map.get(p.blogger_id)
            posts_list.append({
                "id": p.id,
                "title": p.title or "(无标题)",
                "blogger_name": blogger.name if blogger else "-",
                "post_date": p.post_date.isoformat() if p.post_date else None
            })
        
        # 与 cleanup_orphan_funds 保持一致的逻辑
        # 所有预测使用的基金代码（包括已删除的）
        used_fund_codes = set(
            row[0] for row in db.query(Prediction.fund_code).filter(
                Prediction.fund_code.isnot(None),
                Prediction.fund_code != ''
            ).distinct().all()
        )
        # 板块映射中的基金代码
        mapped_fund_codes = set(
            row[0] for row in db.query(SectorFundMapping.fund_code).filter(
                SectorFundMapping.fund_code.isnot(None),
                SectorFundMapping.fund_code != ''
            ).distinct().all()
        )

        all_funds = db.query(FundInfo).all()
        funds_list = []
        for f in all_funds:
            if f.is_core_fund:
                continue
            if not f.can_delete:
                continue
            if f.active_predictions and f.active_predictions > 0:
                continue
            if f.fund_code in used_fund_codes:
                continue
            if f.fund_code in mapped_fund_codes:
                continue
            funds_list.append({
                "id": f.id,
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "sector_type": f.sector_type
            })
        
        bloggers_with_predictions = db.query(Prediction.blogger_id).filter(
            Prediction.is_deleted == False
        ).distinct().subquery()
        
        unused_bloggers = db.query(Blogger).filter(
            ~Blogger.id.in_(bloggers_with_predictions)
        ).all()
        
        bloggers_list = []
        for b in unused_bloggers:
            bloggers_list.append({
                "id": b.id,
                "name": b.name,
                "grade": b.grade
            })

        return {
            "success": True,
            "data": {
                "cleanup_enabled": destructive_cleanup_enabled(),
                "predictions": predictions_list,
                "viewpoints": viewpoints_list,
                "posts": posts_list,
                "funds": funds_list,
                "bloggers": bloggers_list,
                "summary": {
                    "predictions_count": len(predictions_list),
                    "viewpoints_count": len(viewpoints_list),
                    "posts_count": len(posts_list),
                    "funds_count": len(funds_list),
                    "bloggers_count": len(bloggers_list),
                    "total": len(predictions_list) + len(viewpoints_list) + len(posts_list) + len(funds_list) + len(bloggers_list)
                }
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取清理预览失败: {str(e)}",
            "data": None
        }


@router.post("/test-llm")
def test_llm():
    """测试LLM连接"""
    try:
        from src.analyzer.llm_analyzer import get_analyzer
        
        analyzer = get_analyzer()
        
        test_prompt = "你好，请回复'LLM连接成功！'这四个字，不要回复其他内容。"
        
        result = analyzer._call_llm(test_prompt, task_type='simple', max_tokens=50, temperature=0.1)
        
        return {
            "success": True,
            "message": "LLM连接测试成功",
            "data": {
                "provider": config.LLM_PROVIDER,
                "model": analyzer.model if hasattr(analyzer, 'model') else config.LLM_MODEL,
                "response": result.strip() if result else None
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"LLM连接测试失败: {str(e)}",
            "data": {
                "provider": config.LLM_PROVIDER,
                "error": str(e)
            }
        }


@router.post("/test-volcengine-light")
def test_volcengine_light():
    """测试火山引擎辅助模型"""
    try:
        from src.analyzer.llm_analyzer import get_analyzer
        
        analyzer = get_analyzer()
        
        if config.LLM_PROVIDER != 'volcengine':
            return {
                "success": False,
                "message": "辅助模型测试仅支持火山引擎",
                "data": None
            }
        
        test_prompt = "你好，请回复'辅助模型连接成功！'这六个字，不要回复其他内容。"
        result = analyzer._call_llm_with_model(
            config.VOLCENGINE_LIGHT_MODEL,
            test_prompt,
            max_tokens=50,
            temperature=0.1
        )
        
        return {
            "success": True,
            "message": "火山引擎辅助模型测试成功",
            "data": {
                "provider": config.LLM_PROVIDER,
                "light_model": config.VOLCENGINE_LIGHT_MODEL,
                "response": result.strip() if result else None
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"辅助模型测试失败: {str(e)}",
            "data": {
                "provider": config.LLM_PROVIDER,
                "error": str(e)
            }
        }


# ===== 板块别名管理 =====

class AliasCreate(BaseModel):
    """创建别名请求"""
    alias_name: str
    sector_name: str


@router.get("/aliases")
def get_aliases(db: Session = Depends(get_db)):
    """获取所有别名（硬编码+自定义）"""
    from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP

    # 硬编码别名
    builtin = [
        {"alias_name": k, "sector_name": v, "source": "builtin"}
        for k, v in sorted(SECTOR_ALIASES.items())
    ]

    # 数据库自定义别名
    custom_rows = db.query(SectorAlias).order_by(SectorAlias.created_at.desc()).all()
    custom = [
        {
            "id": a.id,
            "alias_name": a.alias_name,
            "sector_name": a.sector_name,
            "source": "custom",
            "created_at": a.created_at.isoformat() if a.created_at else None
        }
        for a in custom_rows
    ]

    return {
        "success": True,
        "data": {
            "builtin": builtin,
            "custom": custom,
            "total": len(builtin) + len(custom),
            "standard_sectors": sorted(SECTOR_FUND_MAP.keys())
        }
    }


@router.post("/aliases")
def create_alias(alias: AliasCreate, db: Session = Depends(get_db)):
    """添加自定义别名"""
    # 检查是否与已有别名冲突
    existing = db.query(SectorAlias).filter(SectorAlias.alias_name == alias.alias_name).first()
    if existing:
        return {
            "success": False,
            "message": f"别名 '{alias.alias_name}' 已存在（映射到 {existing.sector_name}）"
        }

    # 检查是否与硬编码别名冲突
    from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP
    if alias.alias_name in SECTOR_ALIASES:
        return {
            "success": False,
            "message": f"'{alias.alias_name}' 是系统内置别名（映射到 {SECTOR_ALIASES[alias.alias_name]}），无需重复添加"
        }
    if alias.alias_name in SECTOR_FUND_MAP:
        return {
            "success": False,
            "message": f"'{alias.alias_name}' 是系统内置板块名，无需添加为别名"
        }

    new_alias = SectorAlias(alias_name=alias.alias_name, sector_name=alias.sector_name)
    db.add(new_alias)
    db.commit()
    db.refresh(new_alias)

    # 刷新别名缓存
    from src.constants.sector_fund_map import refresh_db_aliases_cache
    refresh_db_aliases_cache()

    return {
        "success": True,
        "message": f"已添加别名: {alias.alias_name} → {alias.sector_name}",
        "data": {
            "id": new_alias.id,
            "alias_name": new_alias.alias_name,
            "sector_name": new_alias.sector_name
        }
    }


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int, db: Session = Depends(get_db)):
    """删除自定义别名"""
    alias = db.query(SectorAlias).filter(SectorAlias.id == alias_id).first()
    if not alias:
        return {"success": False, "message": "别名不存在"}

    db.delete(alias)
    db.commit()

    # 刷新别名缓存
    from src.constants.sector_fund_map import refresh_db_aliases_cache
    refresh_db_aliases_cache()

    return {
        "success": True,
        "message": f"已删除别名: {alias.alias_name} → {alias.sector_name}"
    }


# ===== 板块匹配管理 =====

class MappingUpdate(BaseModel):
    """更新映射请求"""
    fund_code: Optional[str] = None
    fund_name: Optional[str] = None


class MappingCreate(BaseModel):
    """创建映射请求"""
    sector_name: str
    fund_code: str
    fund_name: Optional[str] = None


# ===== 审计结论定向回写（生产写回的服务端出口） =====

# 真写必须显式带这个字面量（与 scripts/push_sector_mappings_to_prod.py 的 --confirm 同一个词，
# 少一个口令就少一次"老板记错哪个串"的机会）
AUDIT_IMPORT_CONFIRM = 'WRITE-TO-PROD'
AUDIT_IMPORT_CONFIRM_HEADER = 'X-Audit-Confirm'

# 逐列照搬的审计字段：sector_name 只用来寻址，本地 id 一律不采信
AUDIT_FIELDS = ('fund_code', 'fund_name', 'keywords', 'is_active', 'reviewed',
                'match_source', 'match_kind', 'confidence', 'verified_at',
                'verify_message', 'llm_reason', 'is_fetchable', 'evidence',
                'reviewed_by', 'owner_locked')

# 列宽（PostgreSQL 真的会截断/报错，SQLite 不会：本地过、线上炸是最坏的组合）
AUDIT_MAX_LEN = {'fund_code': 20, 'fund_name': 100, 'match_source': 20,
                 'match_kind': 12, 'reviewed_by': 30}


class AuditMappingRow(BaseModel):
    """一行审计结论，形状 = `export_repaired_mappings.py` 导出的清单条目。

    用 `model_fields_set` 区分"清单显式给了 null"（必须照搬，例：`reviewed_by=None`
    就是把结论退回未审查）与"根本没给这一列"（不碰目标库现值），
    所以缺列不会把生产值悄悄抹成 NULL。

    寻址/代码两列也做成可选：整列缺失不该让 145 行的批次一起吃 422，
    逐行校验、逐行回执才有意义（空/缺 fund_code 会被拒成 `empty_fund_code`）。
    """
    sector_name: Optional[str] = None
    fund_code: Optional[str] = None
    fund_name: Optional[str] = None
    keywords: Optional[Any] = None
    is_active: Optional[bool] = None
    reviewed: Optional[bool] = None
    match_source: Optional[str] = None
    match_kind: Optional[str] = None
    confidence: Optional[float] = None
    verified_at: Optional[str] = None
    verify_message: Optional[str] = None
    llm_reason: Optional[str] = None
    is_fetchable: Optional[bool] = None
    evidence: Optional[str] = None
    reviewed_by: Optional[str] = None
    owner_locked: Optional[bool] = None


class AuditImportRequest(BaseModel):
    """批量回写请求：默认只出计划，真写要 `confirm`（字段或 `X-Audit-Confirm` 头）。"""
    mappings: list[AuditMappingRow] = []
    dry_run: bool = True
    confirm: Optional[str] = None


class BatchReviewRequest(BaseModel):
    """批量审查请求"""
    ids: list[int]
    reviewed: bool = True
    owner_confirm: bool = False


@router.get("/verify-fund")
def verify_fund_fetchable(fund_code: str, fund_name: Optional[str] = None):
    """验证基金代码能否从数据源抓取（板块映射/添加基金前的审查程序）"""
    result = fund_api.verify_fund_fetchable(fund_code, fund_name)
    return {"success": True, "data": result}


@router.post("/verify-all-funds")
def verify_all_funds(db: Session = Depends(get_db)):
    """一键验证所有板块映射基金能否抓取，列出抓取失败的问题基金。

    复用与 GET /sector-mappings 相同的合并逻辑（DB 映射 + 内置映射），
    相同基金代码只请求一次；相邻请求间加节流间隔，降低被数据源限流概率。
    """
    from src.services.sector_fund_service import get_sector_fund_service
    from src.constants.sector_fund_map import SECTOR_FUND_MAP

    service = get_sector_fund_service(db)
    db_mappings = service.get_all_mappings_with_status(reviewed_filter=None)
    db_sectors = {m['sector_name'] for m in db_mappings}

    items = []
    for m in db_mappings:
        items.append({
            'sector_name': m['sector_name'],
            'fund_code': m['fund_code'],
            'fund_name': m['fund_name'],
        })
    for sector_name, fund_info in sorted(SECTOR_FUND_MAP.items()):
        if sector_name not in db_sectors:
            items.append({
                'sector_name': sector_name,
                'fund_code': fund_info.get('code', ''),
                'fund_name': fund_info.get('name', ''),
            })

    summary = fund_api.verify_funds_batch(items, delay=0.2)
    return {"success": True, "data": summary}


# ===== 板块→基金 AI 匹配（agent 闭环） =====

class AiMatchRequest(BaseModel):
    """单板块 AI 匹配请求；按 sector_name 寻址，内置映射（id=None）同样可用。"""
    sector_name: str
    mapping_id: Optional[int] = None
    apply: bool = False
    # 采纳时必须回传预览拿到的 token：agent 是不确定的，重跑一次可能给出别的基金，
    # 那样老板点的"采纳"就不是他看过的那份证据了。
    decision_token: Optional[str] = None


class AiBatchRequest(BaseModel):
    sectors: Optional[list[str]] = None
    only_pending: bool = False
    only_gap: bool = False
    apply: bool = True
    limit: Optional[int] = None
    run_id: Optional[str] = None


def _sector_work_order(db: Session, only_pending: bool, only_gap: bool) -> list:
    """跑批工单：DB 映射行 + 内置字典板块 + 预测里出现过但没有映射行的板块。

    老板抱怨的错配主要来自"从没被审查过的内置表"，只跑 DB 那 118 条等于漏掉问题主体。
    """
    from src.models.database import Prediction, SectorFundMapping
    from src.constants.sector_fund_map import SECTOR_FUND_MAP

    rows = db.query(SectorFundMapping).filter(
        SectorFundMapping.is_active == True      # noqa: E712
    ).all()
    db_sectors = {r.sector_name for r in rows}
    sectors = []
    if only_pending:
        sectors.extend(sorted({r.sector_name for r in rows if not r.reviewed}))
    if only_gap:
        builtin_gap = sorted(set(SECTOR_FUND_MAP) - db_sectors)
        predicted = {row[0] for row in db.query(Prediction.sector).filter(
            Prediction.sector.isnot(None),
            Prediction.is_deleted == False,       # noqa: E712
        ).distinct().all() if row[0]}
        sectors.extend(sorted((set(builtin_gap) | predicted) - db_sectors))
    if not sectors:
        # only_pending/only_gap 选中的集合为空时**不能**退化成"整个库都跑一遍"：
        # 前端"AI 匹配待审查"按钮在零待审查时会变成全库写操作。
        return []
    seen, ordered = set(), []
    for raw in sectors:
        s = (raw or '').strip()
        try:
            from src.constants.sector_fund_map import normalize_sector_name
            s = normalize_sector_name(s) or s
        except Exception:
            pass
        if not s or len(s) > 50:
            continue
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


@router.post("/sector-mappings/ai-match")
def sector_ai_match(payload: AiMatchRequest, db: Session = Depends(get_db)):
    """跑一次 agent 闭环，返回候选/验证/判定全过程证据；apply=true 才写库。"""
    from src.services.sector_fund_agent import resolve_sector_fund, apply_decision

    if not (payload.sector_name or '').strip():
        raise HTTPException(status_code=400, detail='sector_name 不能为空')
    from src.services.sector_fund_agent import remember_decision, recall_decision

    if payload.apply:
        # 采纳必须回传预览拿到的 token：agent 是不确定的，apply=True 却不带 token 时
        # 悄悄重跑一次再写库，等于把"老板点的采纳就是他看过的那份证据"这句保证废掉，
        # 而且白烧一次 LLM。宁可让前端重来一遍。
        if not payload.decision_token:
            raise HTTPException(
                status_code=400,
                detail='采纳并写入必须带预览返回的 decision_token：'
                       '请先点「AI 匹配」看证据，再点「采纳并写入」')
        decision = recall_decision(payload.decision_token)
        if decision is None or decision.sector != payload.sector_name.strip():
            raise HTTPException(status_code=410,
                                detail='预览结果已过期或与板块不符，请重新点「AI 匹配」再看一遍')
    else:
        decision = resolve_sector_fund(payload.sector_name.strip(), budget_ms=45000, db=db)
    token = remember_decision(decision)
    result = apply_decision(db, decision, mapping_id=payload.mapping_id) if payload.apply \
        else {'applied': False, 'reason': '未开启 apply'}
    return {'success': True,
            'data': {'decision': decision.to_dict(), 'decision_token': token,
                     'apply': result}}


@router.post("/sector-mappings/ai-batch")
def sector_ai_batch(payload: AiBatchRequest, db: Session = Depends(get_db)):
    """异步批量匹配（前端轮询 /ai-batch/status）。"""
    from src.services.sector_ai_match_task import get_sector_ai_match_manager

    sectors = payload.sectors or _sector_work_order(
        db, payload.only_pending, payload.only_gap)
    if payload.limit:
        sectors = sectors[:max(1, payload.limit)]
    started = get_sector_ai_match_manager().start(
        sectors, apply=payload.apply, run_id=payload.run_id)
    if not started.get('success'):
        raise HTTPException(status_code=409, detail=started.get('message', '启动失败'))
    return {'success': True, 'data': started['data']}


@router.get("/sector-mappings/ai-batch/status")
def sector_ai_batch_status():
    from src.services.sector_ai_match_task import get_sector_ai_match_manager
    return {'success': True, 'data': get_sector_ai_match_manager().status()}


@router.get("/fund-search")
def fund_search(keyword: str):
    """按关键词搜基金，供前端"人工换一只"和 agent 复核。

    底层是**混合证券搜索**（实测 000938→紫光股份、液冷→冰山冷热），所以这里必须按
    CATEGORYDESC 过滤掉股票，否则"人工换一只"的下拉框会把股票喂给老板选。
    """
    keyword = (keyword or '').strip()
    if not keyword:
        raise HTTPException(status_code=400, detail='keyword 不能为空')
    results = fund_api.search_fund(keyword)
    if results is None:
        raise HTTPException(status_code=502, detail='基金搜索接口不可用，请稍后重试')
    funds = [r for r in results if r.get('is_fund')]
    return {'success': True, 'data': funds,
            'dropped_non_fund': len(results) - len(funds)}


@router.get("/sector-mappings")
def get_sector_mappings(
    reviewed: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取所有板块映射（含 reviewed 状态 + 硬编码映射）"""
    from src.services.sector_fund_service import get_sector_fund_service
    from src.constants.sector_fund_map import SECTOR_FUND_MAP

    # 手动解析布尔值（FastAPI 对 query string 的 bool 解析不可靠）
    reviewed_filter = None
    if reviewed is not None:
        reviewed_filter = reviewed.lower() in ('true', '1', 'yes')

    service = get_sector_fund_service(db)
    # db_sectors 必须基于未过滤的全量列表计算，否则某板块的 DB 行被
    # reviewed 过滤掉后，同名内置映射会错误地重新出现在筛选结果里
    all_db_mappings = service.get_all_mappings_with_status(reviewed_filter=None)
    db_sectors = {m['sector_name'] for m in all_db_mappings}
    if reviewed_filter is None:
        db_mappings = all_db_mappings
    else:
        db_mappings = [m for m in all_db_mappings if m['reviewed'] == reviewed_filter]

    # 合入硬编码映射中 DB 没有的条目
    # 内置映射一律按"已审查"处理：筛选"待审查"时不能把它们混进来
    merged = list(db_mappings)
    for sector_name, fund_info in sorted(SECTOR_FUND_MAP.items()):
        if sector_name not in db_sectors:
            if reviewed_filter is False:
                continue
            merged.append({
                'id': None,
                'sector_name': sector_name,
                'fund_code': fund_info.get('code', ''),
                'fund_name': fund_info.get('name', ''),
                'reviewed': True,
                'source': 'builtin',
                # 内置表没有体检结论：显式标 unaudited，前端才不会把它算成"已确认可服务"
                'servable': True,
                'identity_verdict': None,
                'identity_reason': None,
                'identity_official_name': None,
                'identity_suggestion': None,
                'relevance_low': False,
                'realigned': None,
                'audited': False,
            })

    mappings = merged
    reviewed_count = sum(1 for m in mappings if m['reviewed'])
    unreviewed_count = len(mappings) - reviewed_count

    return {
        "success": True,
        "data": {
            "mappings": mappings,
            "total": len(mappings),
            "reviewed_count": reviewed_count,
            "unreviewed_count": unreviewed_count
        }
    }


# ===== 审计结论定向回写（生产写回的服务端出口） =====


def _audit_row_values(item: AuditMappingRow):
    """清单行 → {列名: 值}，只包含请求里**显式给出**的列。

    返回 (values, error)：error 非空表示这行不合法，整行拒掉并说明原因，
    绝不"尽力而为"写一半 —— 超长在本地 SQLite 不报、到生产 PostgreSQL 才炸
    是这轮最怕的错位。
    """
    given = item.model_fields_set
    values: dict = {}
    for field in AUDIT_FIELDS:
        if field not in given:
            continue
        value = getattr(item, field)
        if isinstance(value, str):
            value = value.strip() or None
        if field == 'verified_at':
            if value is not None:
                try:
                    value = datetime.fromisoformat(str(value))
                except ValueError:
                    return values, 'bad_verified_at'
        if field == 'evidence' and value is not None:
            if len(value) > 100000:
                return values, 'evidence_too_large'
            try:
                json.loads(value)
            except Exception:
                return values, 'bad_evidence_json'   # 下游一律 json.loads，写坏等于自毁证据
        limit = AUDIT_MAX_LEN.get(field)
        if value is not None and limit and len(str(value)) > limit:
            return values, 'too_long:%s' % field
        values[field] = value
    if not values.get('fund_code'):
        return values, 'empty_fund_code'
    return values, None


def _find_mapping_by_sector(db: Session, sector: str):
    """按**板块名**在目标库定位行（绝不按本地 id：两边 id 不同，按 id 写会写错行）。

    同名多行时沿用 create/update 的同一套选择规则（active 优先、id 最小优先）。
    精确名找不到才退到归一名（别名/黑话），并把命中方式回给调用方，
    让老板看得见这一行是按哪个名字落地的。
    """
    def _pick(name):
        if not name:
            return None
        return db.query(SectorFundMapping).filter(
            SectorFundMapping.sector_name == name
        ).order_by(SectorFundMapping.is_active.desc(), SectorFundMapping.id.asc()).first()

    row = _pick(sector)
    if row is not None:
        # 命中 inactive 行也要说出来：导入本身合法（该行可能被重新启用），但回执只写
        # "updated" 会让老板以为页面少了 1 行其实是筛掉了（第 7 轮 MINOR 复现）。
        return row, ('exact' if row.is_active is not False else 'exact|inactive')
    try:
        from src.constants.sector_fund_map import normalize_sector_name
        normalized = normalize_sector_name(sector)
    except Exception:
        normalized = None
    if normalized and normalized != sector:
        row = _pick(normalized)
        if row is not None:
            return row, 'normalized:%s' % normalized
    return None, None


def _audit_owner_guard(row) -> Optional[str]:
    """老板的行机器不许覆盖：与 `scripts/sweep_sector_mappings.py` 同一条判据。"""
    if row is None:
        return None
    if getattr(row, 'owner_locked', None):
        return 'owner_locked'
    if getattr(row, 'reviewed_by', None) == 'owner':
        return 'reviewed_by_owner'
    return None


def _audit_apply_row(db: Session, service, row, sector: str, values: dict) -> Optional[str]:
    """把审计字段逐列照搬落库。成功返回 None，失败返回拒绝原因。

    第 7 轮评审 BLOCKER-1：这个端点"逐字段照搬"，不设线等于把 M4 刚堵上的
    "人工写入不带身份证明"重新开成一个更大的口子 —— 一份清单就能把
    `600519 贵州茅台` 写进生产，还顺手给自己盖 owner 免疫章（体检与 agent 从此
    碰不到它）。四条线全部在这里，不在调用方：
    1. 总开关：默认关闭，与 `ENABLE_DATABASE_IMPORT` 同一套仓库惯例；
    2. `owner_locked`/`reviewed_by` 一律从载荷剔掉 —— 免疫只能由老板在页面上盖；
    3. 标的代码先过基金域自证，判"根本不是基金/是同码的另一只"整行拒；
    4. 落库前物化镜像不变量：不可服务或"机器换标的没人确认"的行不得带着
       `reviewed=True` 进门（否则第 5 轮修的"一键复活降级行"又通了）。
    """
    from src.services.sector_fund_service import _manual_identity_verdict
    from src.services.sector_identity_audit import (
        UNSERVABLE_VERDICTS, identity_verdict_of, machine_swap_of)

    if os.getenv('ENABLE_SECTOR_AUDIT_IMPORT', '').lower() != 'true':
        return 'import_disabled（服务端需设 ENABLE_SECTOR_AUDIT_IMPORT=true 才开这个口）'
    # 免疫只能由老板在页面上盖：清单不许把自己锁上，也不许冒充老板的"已审查"。
    # 但 `reviewed_by='agent'` 这类**真实来源**要照搬，`owner_locked=False` /
    # `reviewed_by=None`（=取消免疫、退回未审查）也要照搬 —— 一律剔掉这两列会让
    # 回写永久不幂等（清单写 False、库里是 NULL，一比就永远"有差异"）。
    if values.get('owner_locked'):
        values.pop('owner_locked', None)
    if str(values.get('reviewed_by') or '').strip().lower() == 'owner':
        values.pop('reviewed_by', None)
    if 'fund_name' in values and not (values.get('fund_name') or '').strip():
        # 只挡"把这行名字**抹空**"的写法：名字一空，体检从此判 unknown=不指控，
        # 这行就永久免疫。第 9 轮实测 `{fund_code:'600519', fund_name:null, reviewed:true}`
        # 打在一只本来有名字的基金上映射上，名字被洗成 NULL 且没跑身份证明。
        # "没给 fund_name 这一列"是合法的局部更新（保留现值），不该一起挡掉。
        return 'fund_name_wiped'
    if row is not None and 'fund_name' not in values and not (row.fund_name or '').strip():
        # 库里本来就没名字：先补名字再谈回写，否则这行同样永远过不了体检。
        return 'row_has_no_fund_name'
    # 探测是网络活：一次一码，行数由脚本的 --limit 控制；只在真要写时打
    accusation, _identity = _manual_identity_verdict(
        values.get('fund_code'), values.get('fund_name'), sector)
    if accusation:
        return 'identity_unproven:%s' % accusation[:120]
    if row is None:
        # 新建行要有能看的东西：`fund_name=None` 会顺着改标传到
        # `prediction.fund_name`（第 8 轮 MAJOR-3 实测），置信度越界则会让审查门与
        # 改标门槛比出一个永远不成立的数。
        if not values.get('fund_name'):
            return 'created_row_needs_fund_name'
        conf = values.get('confidence')
        if conf is not None and not (0.0 <= float(conf) <= 1.0):
            return 'confidence_out_of_range:%s' % conf
        kws = values.get('keywords')
        if isinstance(kws, str):
            return 'keywords_must_be_a_list'
    try:
        # 判据一律取**改之前**的行状态：先 setattr 再判的话，载荷里带一份
        # `evidence={"identity":{"verdict":"ok"}}` 就能把行上原有的"机器换标的未确认"章
        # 连同旧结论一起洗掉（第 8 轮 MAJOR-2 实测：改完再看 → 旗标消失、reviewed=True 落地）。
        prev_verdict = identity_verdict_of(row) if row is not None else None
        prev_unacked_swap = machine_swap_of(row) if row is not None else None
        incoming_verdict = None
        if values.get('evidence'):
            try:
                incoming_verdict = ((json.loads(values['evidence']) or {})
                                    .get('identity') or {}).get('verdict')
            except Exception:
                incoming_verdict = None
        will_review = bool(values.get('reviewed',
                                      getattr(row, 'reviewed', False) if row else False))
        verdict_after = incoming_verdict if 'evidence' in values else prev_verdict

        # 三条"不许进门"的判断全部前置 —— `ensure_fund_info_exists` 内部会 commit，
        # 先补档案再拒行，就会留下一只没人认领的基金被同步任务拉净值（第 8 轮 MAJOR-4）。
        if will_review and verdict_after in UNSERVABLE_VERDICTS:
            return 'unservable_but_reviewed'
        if will_review and prev_unacked_swap is not None:
            return 'unacknowledged_machine_swap'
        if verdict_after in UNSERVABLE_VERDICTS:
            values['is_fetchable'] = False      # 镜像不变量：verdict 否 ⇒ 列必须 False
        # 外键保障：sector_fund_mapping.fund_code 指向 fund_info，先补最小档案再改映射
        # （复用 PUT/POST 同一个 helper，别再抄一份）
        created_archive = service.ensure_fund_info_exists(
            values.get('fund_code'), values.get('fund_name'), sector)
        if row is None:
            row = SectorFundMapping(sector_name=sector,
                                    fund_code=values.get('fund_code'))
            db.add(row)
        for field, value in values.items():
            setattr(row, field, value)
        db.commit()
        return None
    except Exception as exc:
        db.rollback()
        # 已经 commit 的最小档案不在映射事务里：写映射失败就把它一并撤掉，
        # 否则生产会多出只查得到净值、却没有任何板块引用它的基金。
        if locals().get('created_archive') and values.get('fund_code'):
            try:
                from src.models.database import FundInfo
                db.query(FundInfo).filter(
                    FundInfo.fund_code == values['fund_code']).delete()
                db.commit()
            except Exception:
                db.rollback()
                print('[warn] 审计回写失败且孤儿档案未清除：%s' % values.get('fund_code'))
        return 'write_failed:%s' % str(exc)[:160]


@router.post("/sector-mappings/-/audit-import")
def import_sector_mapping_audit(payload: AuditImportRequest, request: Request,
                                db: Session = Depends(get_db)):
    """把本地体检/审计结论**逐字段**导入本库（按 sector_name 寻址，不整库覆盖）。

    为什么不能走现成的 `PUT /sector-mappings/{id}`：它的请求模型 `MappingUpdate`
    只有 fund_code/fund_name 两列，Pydantic 会静默丢掉其余 11 个审计字段，而
    `service.update_mapping()` 当时还把碰到的每行标成 `reviewed=True` +
    `owner_locked=True` + `reviewed_by='owner'`（第 18 轮 MAJOR-1 起署名与锁定只认
    显式 `owner_confirm`，但"字段丢一半"这件事没变）。本轮实测（临时库跑真实路由）
    13 个字段只有 2 个落地：降级旗标 `is_fetchable=False` 变回 NULL、行被永久锁定，
    回写等于什么都没做，还顺手拆掉了唯一的审查护栏 —— 而脚本照样打印「成功 N」。
    这里按板块名寻址、逐列照搬，让接收端**恰好**停在被审计的状态。

    四条规矩：
    1. 默认 `dry_run=true` 只回计划；真写必须 `confirm == 'WRITE-TO-PROD'`
       （字段或 `X-Audit-Confirm` 头），否则原样返回计划、一行不写；
    2. 只按 `sector_name` 匹配，本地 id 一律不采信；
    3. 只创建/更新，**绝不删除**；
    4. 老板的行（`owner_locked` 或 `reviewed_by='owner'`，例：有意代理
       债券→512000、SpaceX→159206）机器不覆盖，拒了并说明原因。
    """
    rows = payload.mappings or []
    if len(rows) > 2000:
        raise HTTPException(status_code=400, detail='单次最多 2000 行，请分批回写')

    from src.services.sector_fund_service import get_sector_fund_service

    confirm_given = (payload.confirm or '') == AUDIT_IMPORT_CONFIRM or \
        request.headers.get(AUDIT_IMPORT_CONFIRM_HEADER, '') == AUDIT_IMPORT_CONFIRM
    # 没有口令就退化成 dry-run：宁可让调用方多点一次按钮，也不允许"忘了传"变成写生产
    blocked_without_confirm = bool(not payload.dry_run and not confirm_given)
    dry_run = bool(payload.dry_run) or not confirm_given

    service = get_sector_fund_service(db)
    counts = {'updated': 0, 'created': 0, 'unchanged': 0, 'refused': 0}
    items = []
    written = 0

    for item in rows:
        sector = (item.sector_name or '').strip()
        entry = {'sector_name': sector, 'outcome': 'refused', 'reason': None,
                 'mapping_id': None, 'matched_by': None,
                 'fund_code': (item.fund_code or '').strip(), 'changed_fields': []}
        if not sector:
            counts['refused'] += 1
            entry['reason'] = 'empty_sector_name'
            items.append(entry)
            continue
        if len(sector) > 50:
            counts['refused'] += 1
            entry['reason'] = 'too_long:sector_name'
            items.append(entry)
            continue
        values, err = _audit_row_values(item)
        entry['fund_code'] = values.get('fund_code') or entry['fund_code']
        if err:
            counts['refused'] += 1
            entry['reason'] = err
            items.append(entry)
            continue

        row, matched_by = _find_mapping_by_sector(db, sector)
        entry['mapping_id'] = row.id if row is not None else None
        entry['matched_by'] = matched_by
        guard = _audit_owner_guard(row)
        if guard:
            counts['refused'] += 1
            entry['reason'] = guard
            entry['current_fund_code'] = row.fund_code
            entry['current_reviewed'] = bool(row.reviewed)
            items.append(entry)
            continue

        changed = sorted(f for f, v in values.items()
                         if row is None or getattr(row, f, None) != v)
        outcome = 'created' if row is None else ('unchanged' if not changed else 'updated')
        if not dry_run and outcome != 'unchanged':
            apply_err = _audit_apply_row(db, service, row, sector, values)
            if apply_err:
                outcome = 'refused'
                entry['reason'] = apply_err
            else:
                written += 1
        entry['outcome'] = outcome
        entry['changed_fields'] = changed
        counts[outcome] += 1
        items.append(entry)

    if written:
        # 结论改了 reviewed / is_fetchable / evidence：三处进程内缓存必须一起认账，
        # 否则"生产刚写完、自己却读不到"（体检降级要等到下次重启才生效）。
        try:
            service.refresh_cache()
        except Exception as exc:
            print(f"[审计回写] 映射缓存刷新失败（不影响已写入的数据）: {exc}")
        try:
            from src.services.sector_identity_audit import invalidate_denied_cache
            invalidate_denied_cache()
        except Exception as exc:
            print(f"[审计回写] 拒绝集缓存失效失败: {exc}")
        try:
            from src.constants.sector_fund_map import refresh_db_aliases_cache
            refresh_db_aliases_cache()
        except Exception as exc:
            print(f"[审计回写] 别名缓存刷新失败: {exc}")

    reasons = {}
    for i in items:
        if i['outcome'] == 'refused':
            reasons[i['reason']] = reasons.get(i['reason'], 0) + 1
    message = '%s更新 %d、新建 %d、已一致 %d、拒绝 %d（共 %d 行）' % (
        '[dry-run] 计划：' if dry_run else '[审计回写] ',
        counts['updated'], counts['created'], counts['unchanged'],
        counts['refused'], len(rows))
    if blocked_without_confirm:
        message += '；本次未写入：真写必须带 confirm=%s（或 %s 头）' % (
            AUDIT_IMPORT_CONFIRM, AUDIT_IMPORT_CONFIRM_HEADER)
    return {
        'success': True,
        'dry_run': dry_run,
        'confirm_ok': confirm_given,
        'written': written,
        'message': message,
        'data': {
            'total': len(rows),
            'counts': counts,
            'refused_reasons': reasons,
            'items': items,
            'audit_fields': list(AUDIT_FIELDS),
        }
    }


def _mapping_review_tail(result: dict) -> str:
    """保存回执的括号部分：只能说真话。

    `reviewed=True` 有两种来源（老板逐行确认 / 只是编辑过），免疫只给前者 ——
    以前这里固定写"自动标记为已审查"，老板会以为这一行已经锁定了。
    """
    if not result.get('reviewed'):
        return '未标记为已审查'
    if result.get('reviewed_by') == 'owner':
        return '已标记为老板已审查，之后的身份体检不再判它不可服务'
    return '已看过；老板署名与体检免疫需要点"审查"并明确确认'


@router.put("/sector-mappings/{mapping_id}")
def update_sector_mapping(mapping_id: int, update: MappingUpdate,
                          owner_confirm: bool = False,
                          db: Session = Depends(get_db)):
    """更新映射（编辑即视为已看过；老板署名与体检豁免另需 `owner_confirm=true`）。"""
    from src.services.sector_fund_service import get_sector_fund_service

    try:
        service = get_sector_fund_service(db)

        # 外键保障：改绑到新基金代码时，若该代码不在 fund_info 先补最小档案
        # （先确认映射存在，避免为不存在的映射创建孤儿基金档案）
        if update.fund_code:
            exists = db.query(SectorFundMapping.id).filter(
                SectorFundMapping.id == mapping_id
            ).first()
            if not exists:
                return {"success": False, "message": "映射不存在"}
            service.ensure_fund_info_exists(update.fund_code, update.fund_name)

        result = service.update_mapping(
            mapping_id=mapping_id,
            fund_code=update.fund_code,
            fund_name=update.fund_name,
            owner_confirm=owner_confirm
        )

        if not result:
            return {"success": False,
                    "message": "映射不存在，或身份体检不通过（不可服务的行不能只改名字就回到已审查）"}

        # 级联清理冲突
        if result.get('sector_name') and result.get('fund_code'):
            try:
                service.cascade_cleanup_conflicts(
                    result['sector_name'], result['fund_code'], result.get('fund_name', '')
                )
            except Exception as e:
                print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")

        return {
            "success": True,
            "message": "已更新映射: {} → {}（{}）".format(
                result['sector_name'], result['fund_name'], _mapping_review_tail(result)),
            "data": result
        }
    except Exception as e:
        return {"success": False, "message": f"保存失败: {str(e)}"}


@router.post("/sector-mappings")
def create_sector_mapping(mapping: MappingCreate, owner_confirm: bool = False,
                          db: Session = Depends(get_db)):
    """创建新的板块映射（覆盖内置映射或新增）

    覆盖已有行走 `update_mapping`，所以署名/体检免疫同样只认显式 `owner_confirm`
    （第 18 轮 MAJOR-1：PUT 修了、POST 不传等于留了个后门）。
    """
    from src.services.sector_fund_service import get_sector_fund_service

    try:
        service = get_sector_fund_service(db)

        # 外键保障：基金代码不在 fund_info 时先补最小档案，避免 FK 报错
        service.ensure_fund_info_exists(mapping.fund_code, mapping.fund_name, mapping.sector_name)

        # 检查是否已存在同板块的 DB 映射
        # active 优先：同板块可能残留被级联清理置为 inactive 的历史行，
        # 必须优先命中 active 行，避免更新到不可见的 inactive 行上
        existing_mapping = db.query(SectorFundMapping).filter(
            SectorFundMapping.sector_name == mapping.sector_name
        ).order_by(
            SectorFundMapping.is_active.desc(), SectorFundMapping.id.asc()
        ).first()
        if existing_mapping:
            # 已存在，更新
            result = service.update_mapping(
                mapping_id=existing_mapping.id,
                fund_code=mapping.fund_code,
                fund_name=mapping.fund_name,
                owner_confirm=owner_confirm
            )
            if result is None:
                # 门禁拒绝不能报成"更新成功"（前端会显示"已更新映射"）
                return {
                    "success": False,
                    "message": "更新被拒绝：该行身份体检不通过（股票名/同码别的基金）。"
                               "请改基金代码，或先重新验证抓取。",
                    "data": None
                }
            if result.get('sector_name') and result.get('fund_code'):
                try:
                    service.cascade_cleanup_conflicts(
                        result['sector_name'], result['fund_code'], result.get('fund_name', '')
                    )
                except Exception as e:
                    print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")
            return {
                "success": True,
                "message": "已更新映射: {} → {}（{}）".format(
                    mapping.sector_name, mapping.fund_name or mapping.fund_code,
                    _mapping_review_tail(result)),
                "data": result
            }

        # 不存在，创建新记录
        # 第 7 轮 MAJOR-2：创建路径也是"人工写入、没有身份证明"，PUT 那条修了、
        # 这条没修等于没修 —— UI 里新建 白酒→600519 贵州茅台 会直接得到一行
        # reviewed=True 且可服务的股票映射。判"根本不是基金"就不给已审查，
        # 并把理由写清楚（与 update_mapping 同一条规则：探测失败一律不冤枉）。
        # SectorFundMapping 使用模块顶部导入（第 21 行），
        # 不要在此函数内局部 import，否则会把整个函数内的同名变量变成局部变量，
        # 上面 db.query(SectorFundMapping) 会抛 UnboundLocalError
        from src.services.sector_fund_service import _manual_identity_verdict
        if not (mapping.fund_name or '').strip():
            # 名字为空的行**永远通不过体检**（`arbitrate_mapping` 直接判 unknown = 不指控），
            # 于是"可服务 + 已审查"照旧落地：第 9 轮实测 `POST {"sector_name":"测试白酒",
            # "fund_code":"600519"}` 就得到一只能驱动预测改标的股票映射。
            # 这里先拿基金域的品种名补上；补不到就说明这个码在基金域根本不存在 → 拒绝创建。
            from src.fund.fund_api import FundAPI
            try:
                info = FundAPI().get_fund_domain_name(mapping.fund_code, use_roster=False)
            except Exception:
                info = None
            official = ''
            if isinstance(info, dict):
                official = (info.get('name') or '').strip() if info.get('status') == 'ok' else ''
            elif info:
                official = str(info).strip()
            if not official:
                return {
                    "success": False,
                    "message": "基金域查不到这个代码（多半是股票），且没填基金名，拒绝创建：%s"
                               % mapping.fund_code,
                    "data": None
                }
            mapping.fund_name = official
        accusation, _identity = _manual_identity_verdict(
            mapping.fund_code, mapping.fund_name, mapping.sector_name)
        new_mapping = SectorFundMapping(
            sector_name=mapping.sector_name,
            fund_code=mapping.fund_code,
            fund_name=mapping.fund_name or '',
            reviewed=not accusation,
            verify_message=accusation or None,
            is_fetchable=False if accusation else None,
        )
        db.add(new_mapping)
        db.commit()
        db.refresh(new_mapping)

        # 级联清理冲突
        if mapping.fund_code:
            try:
                service.cascade_cleanup_conflicts(
                    mapping.sector_name, mapping.fund_code, mapping.fund_name or ''
                )
            except Exception as e:
                print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")

        return {
            "success": True,
            "message": f"已创建映射: {mapping.sector_name} → {mapping.fund_name or mapping.fund_code}",
            "data": {
                "id": new_mapping.id,
                "sector_name": new_mapping.sector_name,
                "fund_code": new_mapping.fund_code,
                "fund_name": new_mapping.fund_name
            }
        }
    except Exception as e:
        db.rollback()
        return {"success": False, "message": f"创建失败: {str(e)}"}


@router.post("/sector-mappings/{mapping_id}/review")
def review_sector_mapping(mapping_id: int, reviewed: bool = True, owner_confirm: bool = False,
                          db: Session = Depends(get_db)):
    """标记 / 取消标记映射为已审查（逐行确认才给 owner 署名与体检豁免）。

    `owner_confirm` 必须显式传 true：服务层的行为是"给 `reviewed_by='owner'` +
    `owner_locked`"，而上一版路由从不转发这个参数 ⇒ "明确确认"只活在浏览器弹窗里，
    直接 POST 一次就能买到同样的免疫（第 16 轮 m-2；第 17 轮 MAJOR-1 进一步指出
    服务层对有机器证据的行仍然无条件盖 owner，等于两头都没兑现，已一并修掉）。
    `reviewed=false` 是撤销入口：页面上那句"要撤销就再点一次取消审查"以前
    根本没有对应按钮（第 17 轮 MAJOR-2），误点一次就成了永久免疫且不可撤回。
    """
    from src.models.database import SectorFundMapping
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    success = service.mark_reviewed_by_id(mapping_id, reviewed=reviewed,
                                          owner_confirm=owner_confirm)

    if not success:
        # 三种失败原因分开说。全报"映射不存在"会让老板对着一条明明存在的行
        # 反复点，而被体检拒绝的行根本点不动（第 16 轮 MINOR-1）。
        row = db.query(SectorFundMapping).filter(
            SectorFundMapping.id == mapping_id).first()
        if row is None:
            return {"success": False, "message": "映射不存在"}
        if service.is_unservable(row):
            return {
                "success": False,
                "message": ("身份体检判定该行不可服务（%s），逐行确认也不能复活 —— "
                            "先改成正确的基金标的再审查"
                            % (row.verify_message or row.verify_reason or '身份判定未通过')),
            }
        return {
            "success": False,
            "message": "这行没有机器审查证据（match_source / verified_at 为空），"
                       "需要显式 owner_confirm=true 才允许标记为已审查",
        }

    return {
        "success": True,
        "message": ("已标记为已审查" if reviewed else "已取消审查（署名与体检锁定一并撤销）")
                   + ("，并记为老板署名 + 体检锁定" if reviewed and owner_confirm else ""),
    }


@router.post("/sector-mappings/batch-review")
def batch_review_sector_mappings(req: BatchReviewRequest, db: Session = Depends(get_db)):
    """批量标记映射为已审查/未审查"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    count = service.batch_mark_reviewed(req.ids, reviewed=req.reviewed,
                                        owner_confirm=req.owner_confirm)
    rejected = getattr(service, '_last_batch_review_rejected', []) or []

    action = "已审查" if req.reviewed else "未审查"
    message = f"已将 {count} 个映射标记为{action}"
    if rejected:
        # 身份体检不通过的行不能靠"一键全部标记已审查"复活，必须让老板看见被拒了
        message += f"；{len(rejected)} 个因身份体检不通过被拒绝（{('、'.join(rejected[:3]))}…）"
    return {
        "success": True,
        "message": message,
        "data": {"count": count, "rejected": len(rejected),
                 "rejected_sectors": rejected}
    }


@router.delete("/sector-mappings/{mapping_id}")
def delete_sector_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """删除映射"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    success = service.delete_mapping(mapping_id)

    if not success:
        return {"success": False, "message": "映射不存在"}

    return {
        "success": True,
        "message": "已删除映射"
    }


@router.post("/sector-mappings/seed")
def seed_sector_mappings(db: Session = Depends(get_db)):
    """导入预置板块映射数据"""
    try:
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "scripts/seed_sector_mappings.py"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )

        # 刷新服务缓存
        from src.services.sector_fund_service import get_sector_fund_service
        service = get_sector_fund_service(db)
        service.refresh_cache()

        return {
            "success": True,
            "message": "预置数据导入完成",
            "data": {
                "stdout": result.stdout[-500:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else ""
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"导入失败: {str(e)}"
        }


# ===== 数据导入导出 =====

def _serialize_row(obj, exclude_fields=None):
    """将 SQLAlchemy 模型对象序列化为字典"""
    exclude = set(exclude_fields or [])
    d = {}
    for col in obj.__table__.columns:
        if col.name in exclude:
            continue
        val = getattr(obj, col.name)
        if isinstance(val, (date, datetime)):
            d[col.name] = val.isoformat()
        else:
            d[col.name] = val
    return d


@router.get("/export")
def export_all_data(db: Session = Depends(get_db)):
    """导出全部业务数据为 JSON 文件"""
    try:
        export_data = DataPortabilityService(db).export_data()
        json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode('utf-8')
        filename = f"fund_insight_export_{date.today().isoformat()}.json"

        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return {"success": False, "message": f"导出失败: {str(e)}"}


@router.get("/export/config")
def export_config(db: Session = Depends(get_db)):
    """导出系统配置（LLM 设置 + 别名 + 板块映射）"""
    try:
        from src.constants.sector_fund_map import SECTOR_ALIASES

        aliases_custom = [_serialize_row(a) for a in db.query(SectorAlias).all()]
        mappings = [_serialize_row(m) for m in db.query(SectorFundMapping).all()]

        config_data = {
            "export_version": "1.0",
            "export_date": datetime.now().isoformat(),
            "type": "config",
            "llm_config": {
                "llm_provider": config.LLM_PROVIDER,
                "llm_base_url": config.LLM_BASE_URL,
                "llm_model": config.LLM_MODEL,
                "llm_light_model": config.LLM_LIGHT_MODEL,
                "llm_strategy": config.LLM_STRATEGY,
                "volcengine_base_url": config.VOLCENGINE_BASE_URL,
                "volcengine_model": config.VOLCENGINE_MODEL,
                "volcengine_light_model": config.VOLCENGINE_LIGHT_MODEL,
            },
            "builtin_aliases": SECTOR_ALIASES,
            "custom_aliases": aliases_custom,
            "sector_mappings": mappings
        }

        json_bytes = json.dumps(config_data, ensure_ascii=False, indent=2).encode('utf-8')
        filename = f"fund_insight_config_{date.today().isoformat()}.json"

        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return {"success": False, "message": f"导出配置失败: {str(e)}"}


class ImportDataRequest(BaseModel):
    """导入数据请求"""
    data: dict
    replace: bool = False  # True=覆盖模式（先清空再导入），False=合并模式


def _run_import_background(payload: dict, replace: bool, session_factory=None):
    """后台线程入口：自建会话跑导入，避免占用请求会话。

    session_factory 仅供测试注入内存库会话工厂；生产默认用全局 SessionLocal。
    """
    if session_factory is None:
        from src.models.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    try:
        DataPortabilityService(db).run_import_background(payload, replace)
    finally:
        db.close()


@router.post("/import")
def import_data(req: ImportDataRequest, request: Request, background_tasks: BackgroundTasks,
                db: Session = Depends(get_db)):
    """
    导入 JSON 数据（后台异步，立即返回，前端轮询 /import/status 拿结果）。

    默认合并模式（按 natural key 跳过已存在记录）；
    replace=True 时先清空所有数据表再导入（覆盖模式，用于本地清洗后整体同步到线上）。

    `replace=True` 是全仓破坏性最强的一次操作（PG 侧先 TRUNCATE 再导，失败就是一片空表），
    以前它只要一个 HTTP 请求就够了 —— 而仓库里另外 11 个破坏性入口全都要求确认头。
    现在补齐两道闸：服务端总开关 `ENABLE_DATABASE_IMPORT=true`（默认关）
    + 确认头 `X-Danger-Confirm: replace-database-import`，两者缺一律 403。
    """
    if req.replace:
        if os.getenv("ENABLE_DATABASE_IMPORT", "false").lower() != "true":
            raise HTTPException(
                status_code=403,
                detail='覆盖式导入默认禁用：服务端需设 ENABLE_DATABASE_IMPORT=true 才开这个口')
        if request.headers.get("X-Danger-Confirm") != "replace-database-import":
            raise HTTPException(
                status_code=403,
                detail='覆盖式导入会清空全部数据表，需要确认头 '
                       'X-Danger-Confirm: replace-database-import')
    # 防重入：已有任务在跑就直接返回当前状态
    status = DataPortabilityService(db).get_import_job_status()
    if status.get("status") == "running":
        return {"success": True, "message": "导入正在进行中", "data": status, "async": True}

    background_tasks.add_task(_run_import_background, req.data, req.replace)
    return {
        "success": True,
        "message": "已开始后台导入，请稍后查看结果",
        "data": {"status": "running"},
        "async": True,
    }


@router.get("/import/status")
def get_import_status(db: Session = Depends(get_db)):
    """查询数据导入后台任务状态（前端轮询用）。"""
    return {"success": True, "data": DataPortabilityService(db).get_import_job_status()}
