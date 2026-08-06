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
from typing import Optional
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


class BatchReviewRequest(BaseModel):
    """批量审查请求"""
    ids: list[int]
    reviewed: bool = True


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
                'source': 'builtin'
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


@router.put("/sector-mappings/{mapping_id}")
def update_sector_mapping(mapping_id: int, update: MappingUpdate, db: Session = Depends(get_db)):
    """更新映射（自动标记为已审查）"""
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
            fund_name=update.fund_name
        )

        if not result:
            return {"success": False, "message": "映射不存在"}

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
            "message": f"已更新映射: {result['sector_name']} → {result['fund_name']}（自动标记为已审查）",
            "data": result
        }
    except Exception as e:
        return {"success": False, "message": f"保存失败: {str(e)}"}


@router.post("/sector-mappings")
def create_sector_mapping(mapping: MappingCreate, db: Session = Depends(get_db)):
    """创建新的板块映射（覆盖内置映射或新增）"""
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
                fund_name=mapping.fund_name
            )
            if result and result.get('sector_name') and result.get('fund_code'):
                try:
                    service.cascade_cleanup_conflicts(
                        result['sector_name'], result['fund_code'], result.get('fund_name', '')
                    )
                except Exception as e:
                    print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")
            return {
                "success": True,
                "message": f"已更新映射: {mapping.sector_name} → {mapping.fund_name or mapping.fund_code}",
                "data": result
            }

        # 不存在，创建新记录
        # 注意：SectorFundMapping 使用模块顶部导入（第 21 行），
        # 不要在此函数内局部 import，否则会把整个函数内的同名变量变成局部变量，
        # 上面 db.query(SectorFundMapping) 会抛 UnboundLocalError
        new_mapping = SectorFundMapping(
            sector_name=mapping.sector_name,
            fund_code=mapping.fund_code,
            fund_name=mapping.fund_name or '',
            reviewed=True
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
def review_sector_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """标记映射为已审查"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    success = service.mark_reviewed_by_id(mapping_id, reviewed=True)

    if not success:
        return {"success": False, "message": "映射不存在"}

    return {
        "success": True,
        "message": "已标记为已审查"
    }


@router.post("/sector-mappings/batch-review")
def batch_review_sector_mappings(req: BatchReviewRequest, db: Session = Depends(get_db)):
    """批量标记映射为已审查/未审查"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    count = service.batch_mark_reviewed(req.ids, reviewed=req.reviewed)

    action = "已审查" if req.reviewed else "未审查"
    return {
        "success": True,
        "message": f"已将 {count} 个映射标记为{action}",
        "data": {"count": count}
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


@router.post("/import")
def import_data(req: ImportDataRequest, db: Session = Depends(get_db)):
    """
    导入 JSON 数据。

    默认合并模式（按 natural key 跳过已存在记录）；
    replace=True 时先清空所有数据表再导入（覆盖模式，用于本地清洗后整体同步到线上）。
    """
    return DataPortabilityService(db).import_data(req.data, replace=req.replace)
