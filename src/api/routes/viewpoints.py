"""观点管理 API：抓取、分析、查询和每日汇总。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.core.safety import destructive_cleanup_enabled
from src.models.database import BatchAnalysisTask, CrawlerArticleRecord, Viewpoint
from src.services.viewpoint_workflow_service import (
    ALLOWED_SOURCES,
    DEFAULT_SOURCES,
    ViewpointWorkflowService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/viewpoints", tags=["观点"])


class ViewpointFetchRequest(BaseModel):
    sources: List[str] = Field(default_factory=lambda: list(DEFAULT_SOURCES))
    limit_per_source: int = Field(default=15, ge=1, le=50)


def _is_analyzed(row: Viewpoint) -> bool:
    return bool(row.is_summary or (row.reasoning and row.summary and row.market_direction))


def _analysis_status(row: Viewpoint) -> str:
    if row.is_summary or _is_analyzed(row):
        return "succeeded"
    if (row.analysis_summary or "").startswith("failed:"):
        return "failed"
    return "pending"


def _serialize_list(row: Viewpoint) -> dict:
    expired = bool(row.valid_until and row.valid_until < date.today())
    return {
        "id": row.id,
        "author": row.author or "未知",
        "source": row.source,
        "summary": row.summary or ((row.content or "")[:160] if row.is_summary else ""),
        "market_direction": row.market_direction or "neutral",
        "confidence": row.confidence or 50,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "viewpoint_date": row.viewpoint_date.isoformat() if row.viewpoint_date else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "is_expired": expired,
        "is_summary": bool(row.is_summary),
        "analysis_status": _analysis_status(row),
        "sectors": list(dict.fromkeys((row.sectors_bullish or []) + (row.sectors_bearish or []))),
        "article_url": row.article_url,
    }


def _serialize_detail(row: Viewpoint) -> dict:
    data = _serialize_list(row)
    data.update({
        "blogger_id": row.blogger_id,
        "post_id": row.post_id,
        "fund_code": row.fund_code,
        "fund_name": row.fund_name,
        "content": row.content or "",
        "reasoning": row.reasoning or "",
        "credibility_score": row.credibility_score or 50,
        "weight": row.weight or 1.0,
        "risk_level": row.risk_level or "medium",
        "action_suggestion": row.action_suggestion or "观望",
        "sectors_bullish": row.sectors_bullish or [],
        "sectors_bearish": row.sectors_bearish or [],
        "time_horizon": row.time_horizon,
        "validity_period": row.validity_period,
        "original_count": row.original_count or 0,
        "original_ids": row.original_ids or [],
        "topics": row.topics or [],
        "article_id": row.article_id,
        "content_hash": row.content_hash,
        "is_deleted": bool(row.is_deleted),
    })
    return data


@router.get("")
async def get_viewpoints(
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    market_direction: Optional[str] = None,
    analysis_status: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    viewpoint_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    query = db.query(Viewpoint).filter(Viewpoint.is_deleted.is_(False))
    if keyword:
        term = f"%{keyword.strip()}%"
        query = query.filter(or_(Viewpoint.content.ilike(term), Viewpoint.summary.ilike(term), Viewpoint.author.ilike(term)))
    if source:
        query = query.filter(Viewpoint.source == source)
    if market_direction:
        query = query.filter(Viewpoint.market_direction == market_direction)
    if date_from:
        query = query.filter(Viewpoint.viewpoint_date >= date_from)
    if date_to:
        query = query.filter(Viewpoint.viewpoint_date <= date_to)
    if viewpoint_type:
        if viewpoint_type == "summary":
            query = query.filter(Viewpoint.is_summary.is_(True))
        elif viewpoint_type == "original":
            query = query.filter(Viewpoint.is_summary.is_(False))
    if analysis_status == "succeeded":
        query = query.filter(or_(
            Viewpoint.is_summary.is_(True),
            and_(Viewpoint.reasoning.isnot(None), Viewpoint.summary.isnot(None), Viewpoint.market_direction.isnot(None)),
        ))
    elif analysis_status == "failed":
        query = query.filter(Viewpoint.is_summary.is_(False), Viewpoint.analysis_summary.like("failed:%"))
    elif analysis_status == "pending":
        query = query.filter(
            Viewpoint.is_summary.is_(False),
            Viewpoint.reasoning.is_(None),
            or_(Viewpoint.analysis_summary.is_(None), ~Viewpoint.analysis_summary.like("failed:%")),
        )

    total = query.with_entities(func.count(Viewpoint.id)).scalar() or 0
    rows = query.order_by(Viewpoint.viewpoint_date.desc(), Viewpoint.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "success": True,
        "data": [_serialize_list(row) for row in rows],
        "meta": {"page": page, "page_size": page_size, "total": total, "pages": pages},
    }


def _run_fetch_task(task_id: int):
    ViewpointWorkflowService.run_fetch_task(task_id)


@router.post("/fetch")
async def fetch_viewpoints(
    payload: ViewpointFetchRequest = Body(default=ViewpointFetchRequest()),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    try:
        task, created = ViewpointWorkflowService.create_fetch_task(
            db, sources=payload.sources, limit_per_source=payload.limit_per_source
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if (created or task.status == "pending") and background_tasks is not None:
        background_tasks.add_task(_run_fetch_task, task.id)
    return {
        "success": True,
        "message": "已创建观点抓取任务" if created else "已有观点抓取任务，已恢复其进度",
        "data": ViewpointWorkflowService.serialize_task(task),
    }


@router.get("/tasks/latest")
async def latest_viewpoint_task(db: Session = Depends(get_db)):
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.task_type.in_(("viewpoint_fetch", "viewpoint_summary")),
    ).order_by(BatchAnalysisTask.created_at.desc()).first()
    return {"success": True, "data": ViewpointWorkflowService.serialize_task(task) if task else None}


@router.post("/tasks/{task_id}/retry")
async def retry_viewpoint_task(
    task_id: int,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    try:
        task = ViewpointWorkflowService.retry_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if background_tasks is not None:
        background_tasks.add_task(_run_fetch_task, task.id)
    return {"success": True, "data": ViewpointWorkflowService.serialize_task(task)}


@router.get("/insights")
async def viewpoint_insights(db: Session = Depends(get_db)):
    today = date.today()
    active = db.query(Viewpoint).filter(
        Viewpoint.is_deleted.is_(False),
        Viewpoint.is_summary.is_(False),
        Viewpoint.reasoning.isnot(None),
        or_(Viewpoint.valid_until.is_(None), Viewpoint.valid_until >= today),
        Viewpoint.viewpoint_date >= today - timedelta(days=30),
    ).all()
    directions = {"bullish": 0, "bearish": 0, "neutral": 0}
    sectors = {}
    sources = {}
    for row in active:
        direction = row.market_direction if row.market_direction in directions else "neutral"
        directions[direction] += 1
        source = row.source or "unknown"
        sources.setdefault(source, {"count": 0, "analyzed": 0})
        sources[source]["count"] += 1
        sources[source]["analyzed"] += int(_is_analyzed(row))
        for sector in (row.sectors_bullish or []) + (row.sectors_bearish or []):
            sectors[sector] = sectors.get(sector, 0) + 1
    pending = db.query(Viewpoint.viewpoint_date, func.count(Viewpoint.id)).filter(
        Viewpoint.is_deleted.is_(False),
        Viewpoint.is_summary.is_(False),
        Viewpoint.viewpoint_date < today,
    ).group_by(Viewpoint.viewpoint_date).order_by(Viewpoint.viewpoint_date.desc()).all()
    return {
        "success": True,
        "data": {
            "directions": directions,
            "direction_total": len(active),
            "sector_consensus": sorted(
                ({"sector": key, "count": value} for key, value in sectors.items()),
                key=lambda item: item["count"], reverse=True,
            )[:10],
            "source_quality": sources,
            "pending_summary": [{"date": day.isoformat(), "count": count} for day, count in pending],
        },
    }


@router.get("/{viewpoint_id}")
async def get_viewpoint_detail(viewpoint_id: int, db: Session = Depends(get_db)):
    viewpoint = db.query(Viewpoint).filter(
        Viewpoint.id == viewpoint_id,
        Viewpoint.is_deleted.is_(False),
    ).first()
    if not viewpoint:
        raise HTTPException(status_code=404, detail="观点不存在")
    return {"success": True, "data": _serialize_detail(viewpoint)}


@router.delete("/{viewpoint_id}")
async def delete_viewpoint(viewpoint_id: int, request: Request, db: Session = Depends(get_db)):
    confirmation = request.headers.get("X-Danger-Confirm") or request.headers.get("x-danger-confirm")
    if confirmation != "delete-viewpoint":
        raise HTTPException(status_code=403, detail="永久删除观点需要确认头 X-Danger-Confirm: delete-viewpoint")
    viewpoint = db.query(Viewpoint).filter(
        Viewpoint.id == viewpoint_id,
        Viewpoint.is_deleted.is_(False),
    ).first()
    if not viewpoint:
        raise HTTPException(status_code=404, detail="观点不存在")
    db.query(CrawlerArticleRecord).filter(CrawlerArticleRecord.viewpoint_id == viewpoint_id).update(
        {CrawlerArticleRecord.viewpoint_id: None, CrawlerArticleRecord.is_adopted: False},
        synchronize_session=False,
    )
    db.delete(viewpoint)
    db.commit()
    return {"success": True, "message": "观点已永久删除"}


# 保留隐藏的兼容入口，避免旧前端误调用时绕过安全开关；新页面不再展示它。
@router.post("/cleanup", include_in_schema=False)
async def deprecated_viewpoint_cleanup(db: Session = Depends(get_db)):
    if not destructive_cleanup_enabled():
        raise HTTPException(status_code=403, detail="数据清理功能已禁用")
    raise HTTPException(status_code=410, detail="观点专用清理接口已停用")
