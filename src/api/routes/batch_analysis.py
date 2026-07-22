"""批量分析兼容路由，实际执行统一委托给帖子分析服务。"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.models.database import AnalysisLog, BatchAnalysisTask
from src.services.post_analysis_service import PostAnalysisService


router = APIRouter(prefix="/batch-analysis", tags=["批量分析"])


class BatchAnalysisRequest(BaseModel):
    """兼容旧请求格式；当前该入口只处理帖子。"""

    task_type: str = "posts"
    resume_task_id: Optional[int] = None
    limit: int = Field(default=1000, ge=1, le=1000)


class BatchAnalysisStatus(BaseModel):
    task_id: int
    status: str
    total_count: int
    processed_count: int
    success_count: int
    failed_count: int
    progress: float
    estimated_remaining: Optional[int] = None


def _execute_batch_analysis_task(task_id: int, db: Session = None):
    """保留旧函数名，避免脚本或测试调用失效。"""
    PostAnalysisService.run_job(task_id)


@router.post("/start")
async def start_batch_analysis(
    request: BatchAnalysisRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if request.task_type != "posts":
        raise HTTPException(status_code=400, detail="该兼容入口当前仅支持帖子分析")

    if request.resume_task_id is not None:
        try:
            task = PostAnalysisService.resume_job(db, request.resume_task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background_tasks.add_task(_execute_batch_analysis_task, task.id)
        return {
            "success": True,
            "message": "继续未完成的任务",
            "data": PostAnalysisService.serialize_job(task),
        }

    task, created = PostAnalysisService.create_job(db, limit=request.limit)
    if task.status == "pending":
        background_tasks.add_task(_execute_batch_analysis_task, task.id)
    return {
        "success": True,
        "message": "批量分析任务已启动" if created else "已有批量分析任务",
        "data": PostAnalysisService.serialize_job(task),
    }


@router.get("/status/{task_id}")
async def get_batch_analysis_status(task_id: int, db: Session = Depends(get_db)):
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.id == task_id,
        BatchAnalysisTask.task_type == "posts",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    data = PostAnalysisService.serialize_job(task)
    data["estimated_remaining"] = None
    if task.started_at and task.processed_count:
        elapsed = (datetime.now() - task.started_at).total_seconds()
        remaining = max((task.total_count or 0) - task.processed_count, 0)
        data["estimated_remaining"] = int(elapsed / task.processed_count * remaining)
    return {"success": True, "data": data}


@router.post("/cancel/{task_id}")
async def cancel_batch_analysis(task_id: int, db: Session = Depends(get_db)):
    try:
        task = PostAnalysisService.cancel_job(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "success": True,
        "message": "任务已取消",
        "data": PostAnalysisService.serialize_job(task),
    }


@router.get("/report/{task_id}")
async def get_batch_analysis_report(task_id: int, db: Session = Depends(get_db)):
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.id == task_id,
        BatchAnalysisTask.task_type == "posts",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    logs = db.query(AnalysisLog).filter(
        AnalysisLog.task_id == task_id
    ).order_by(AnalysisLog.created_at.desc()).limit(100).all()
    return {
        "success": True,
        "data": {
            "task": PostAnalysisService.serialize_job(task),
            "logs": [
                {
                    "post_id": log.post_id,
                    "parse_success": log.parse_success,
                    "parse_method": log.parse_method,
                    "fund_match_level": log.fund_match_level,
                    "fund_code": log.fund_code,
                    "fund_name": log.fund_name,
                    "analysis_duration": log.analysis_duration,
                    "parse_error": log.parse_error,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in logs
            ],
            "failed_items": list(task.failed_ids or []),
        },
    }
