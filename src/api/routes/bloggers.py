"""
博主路由
处理博主相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
from typing import Optional
from pydantic import BaseModel
from datetime import date, timedelta

from src.api.deps import get_db
from src.models.database import Prediction
from src.services import BloggerService

router = APIRouter(prefix="/bloggers", tags=["博主"])


class BloggerCreate(BaseModel):
    name: str
    platform: str = "xiaohongshu"
    description: Optional[str] = None


@router.get("")
async def get_bloggers(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    platform: Optional[str] = None,
    active_only: bool = False,
    db: Session = Depends(get_db)
):
    """获取博主列表"""
    service = BloggerService(db)
    
    if active_only:
        bloggers = service.get_active_bloggers(skip=skip, limit=limit)
    elif platform:
        bloggers = service.get_by_platform(platform, skip=skip, limit=limit)
    else:
        bloggers = service.get_all(skip=skip, limit=limit)
    
    cutoff_date = date.today() - timedelta(days=7)

    # 批量查询活跃帖子数，避免 N+1 问题
    blogger_ids = [b.id for b in bloggers]
    active_count_map = {}
    if blogger_ids:
        active_counts = db.query(
            Prediction.blogger_id,
            func.count(distinct(Prediction.post_id))
        ).filter(
            Prediction.blogger_id.in_(blogger_ids),
            Prediction.target_date >= cutoff_date
        ).group_by(Prediction.blogger_id).all()
        active_count_map = {bid: cnt for bid, cnt in active_counts}

    result = []
    for b in bloggers:
        active_posts_count = active_count_map.get(b.id, 0)

        result.append({
            "id": b.id,
            "name": b.name,
            "platform": b.platform,
            "description": b.description,
            "accuracy_rate": b.accuracy_rate,
            "total_predictions": b.total_predictions,
            "correct_predictions": b.correct_predictions,
            "grade": b.grade,
            "ultra_short_accuracy": b.ultra_short_accuracy,
            "is_active": b.is_active,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "active_posts_count": active_posts_count
        })
    
    return {
        "success": True,
        "data": result,
        "total": service.count()
    }


@router.get("/top")
async def get_top_bloggers(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db)
):
    """获取准确率最高的博主"""
    service = BloggerService(db)
    bloggers = service.get_top_bloggers(limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": b.id,
                "name": b.name,
                "platform": b.platform,
                "accuracy_rate": b.accuracy_rate,
                "total_predictions": b.total_predictions,
                "grade": b.grade
            }
            for b in bloggers
        ]
    }


@router.post("")
async def create_blogger(
    data: BloggerCreate,
    db: Session = Depends(get_db)
):
    """创建博主"""
    service = BloggerService(db)
    
    existing = service.get_by_name(data.name)
    if existing:
        raise HTTPException(status_code=400, detail="博主名称已存在")
    
    blogger = service.create({
        "name": data.name,
        "platform": data.platform,
        "description": data.description
    })
    
    return {
        "success": True,
        "data": {
            "id": blogger.id,
            "name": blogger.name,
            "platform": blogger.platform
        }
    }


