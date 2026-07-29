"""
博主路由
处理博主相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct, case, and_
from typing import Optional, Dict, List
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


def _hit_rate_map(db: Session, blogger_ids: List[int]) -> Dict[int, dict]:
    """存活命中率：is_deleted=false 且 is_correct 非空。"""
    if not blogger_ids:
        return {}
    rows = (
        db.query(
            Prediction.blogger_id,
            func.count(case((Prediction.is_correct.isnot(None), 1))).label("verified"),
            func.count(case((Prediction.is_correct.is_(True), 1))).label("correct"),
        )
        .filter(
            Prediction.blogger_id.in_(blogger_ids),
            Prediction.is_deleted == False,
        )
        .group_by(Prediction.blogger_id)
        .all()
    )
    out = {}
    for bid, verified, correct in rows:
        verified = int(verified or 0)
        correct = int(correct or 0)
        out[bid] = {
            "hit_verified": verified,
            "hit_correct": correct,
            "hit_rate": round(correct / verified * 100, 2) if verified > 0 else None,
        }
    return out


@router.get("")
async def get_bloggers(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    platform: Optional[str] = None,
    active_only: bool = False,
    db: Session = Depends(get_db)
):
    """获取博主列表（含存活命中率 hit_rate + 加权评分 accuracy_rate）"""
    service = BloggerService(db)

    if active_only:
        bloggers = service.get_active_bloggers(skip=skip, limit=limit)
    elif platform:
        bloggers = service.get_by_platform(platform, skip=skip, limit=limit)
    else:
        bloggers = service.get_all(skip=skip, limit=limit)

    cutoff_date = date.today() - timedelta(days=7)

    blogger_ids = [b.id for b in bloggers]
    active_count_map = {}
    if blogger_ids:
        active_counts = db.query(
            Prediction.blogger_id,
            func.count(distinct(Prediction.post_id))
        ).filter(
            Prediction.blogger_id.in_(blogger_ids),
            Prediction.target_date >= cutoff_date,
            Prediction.is_deleted == False,
        ).group_by(Prediction.blogger_id).all()
        active_count_map = {bid: cnt for bid, cnt in active_counts}

    hit_map = _hit_rate_map(db, blogger_ids)

    result = []
    for b in bloggers:
        hit = hit_map.get(b.id, {})
        result.append({
            "id": b.id,
            "name": b.name,
            "platform": b.platform,
            "description": b.description,
            # 规范「准确率」= 存活命中率
            "hit_rate": hit.get("hit_rate"),
            "hit_correct": hit.get("hit_correct", 0),
            "hit_verified": hit.get("hit_verified", 0),
            "accuracy_rate": b.accuracy_rate,  # 加权评分（verify_score 公式），次列
            "weighted_score": b.accuracy_rate,  # 别名，便于前端
            "total_predictions": b.total_predictions,
            "correct_predictions": b.correct_predictions,
            "grade": b.grade,
            "ultra_short_accuracy": b.ultra_short_accuracy,
            "is_active": b.is_active,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "active_posts_count": active_count_map.get(b.id, 0),
        })

    # 默认按命中率降序（无结论的排后）
    result.sort(
        key=lambda row: (
            row["hit_rate"] is not None,
            row["hit_rate"] if row["hit_rate"] is not None else -1,
            row["hit_verified"] or 0,
        ),
        reverse=True,
    )

    return {
        "success": True,
        "data": result,
        "total": service.count(),
        "metric_note": "hit_rate=存活命中率(is_correct)；accuracy_rate/weighted_score=加权评分",
    }


@router.get("/top")
async def get_top_bloggers(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db)
):
    """获取命中率最高的博主（至少 5 条已验证结论）"""
    service = BloggerService(db)
    # 先取足够候选再按 hit_rate 排
    bloggers = service.get_all(skip=0, limit=500)
    hit_map = _hit_rate_map(db, [b.id for b in bloggers])
    ranked = []
    for b in bloggers:
        hit = hit_map.get(b.id, {})
        verified = hit.get("hit_verified") or 0
        if verified < 5:
            continue
        ranked.append({
            "id": b.id,
            "name": b.name,
            "platform": b.platform,
            "hit_rate": hit.get("hit_rate"),
            "hit_correct": hit.get("hit_correct", 0),
            "hit_verified": verified,
            "accuracy_rate": b.accuracy_rate,
            "weighted_score": b.accuracy_rate,
            "total_predictions": b.total_predictions,
            "grade": b.grade,
        })
    ranked.sort(key=lambda r: (r["hit_rate"] or 0, r["hit_verified"]), reverse=True)

    return {
        "success": True,
        "data": ranked[:limit],
        "metric_note": "按存活命中率 hit_rate 排序",
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


