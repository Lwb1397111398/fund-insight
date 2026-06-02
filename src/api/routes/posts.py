"""
帖子路由
处理帖子相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import date

from src.api.deps import get_db
from src.services.post_service import PostService

router = APIRouter(prefix="/posts", tags=["帖子"])


class PostCreate(BaseModel):
    blogger_id: int
    title: Optional[str] = None
    content: str
    post_date: date
    source_url: Optional[str] = None
    async_mode: bool = True


_batch_analyzing = False


def _batch_analyze_background():
    """后台批量分析任务（batch_analyze_posts内部自行管理会话）"""
    global _batch_analyzing
    try:
        from src.models.database import SessionLocal
        db = SessionLocal()
        try:
            service = PostService(db)
            result = service.batch_analyze_posts()
            print(f"[Batch Analyze] 后台批量分析完成: {result['message']}")
        finally:
            db.close()
    except Exception as e:
        print(f"[Batch Analyze] 后台批量分析失败: {e}")
    finally:
        _batch_analyzing = False


@router.get("")
async def get_posts(
    skip: int = 0,
    limit: int = 1000,
    blogger_id: Optional[int] = None,
    analyzed: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """获取帖子列表"""
    service = PostService(db)
    posts = service.get_posts_with_blogger_info(
        skip=skip,
        limit=limit,
        blogger_id=blogger_id,
        analyzed=analyzed
    )
    
    return {
        "success": True,
        "data": posts
    }


@router.post("")
async def create_post(post: PostCreate, db: Session = Depends(get_db)):
    """添加帖子（async_mode=True 时不自动分析，需手动触发）"""
    service = PostService(db)
    
    try:
        result = service.create_post_with_analysis(
            blogger_id=post.blogger_id,
            content=post.content,
            post_date=post.post_date,
            title=post.title,
            source_url=post.source_url,
            async_mode=post.async_mode
        )
        
        return {
            "success": True,
            "message": result.get("message", "帖子添加成功"),
            "data": result
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/reset-failed")
async def reset_failed_analyses(db: Session = Depends(get_db)):
    """重置分析失败的帖子（标记为已分析但无有效预测的帖子）"""
    import json
    from src.models.database import Post, Prediction

    # 查找标记为已分析但分析结果为空的帖子
    analyzed_posts = db.query(Post).filter(Post.analyzed == True).all()
    reset_count = 0

    for post in analyzed_posts:
        should_reset = False
        if not post.analysis_result:
            should_reset = True
        else:
            try:
                result = json.loads(post.analysis_result) if isinstance(post.analysis_result, str) else post.analysis_result
                if not result.get("predictions"):
                    should_reset = True
            except Exception:
                should_reset = True

        if should_reset:
            # 检查是否有关联的预测记录
            pred_count = db.query(Prediction).filter(Prediction.post_id == post.id).count()
            if pred_count == 0:
                post.analyzed = False
                reset_count += 1

    db.commit()
    return {
        "success": True,
        "message": f"已重置 {reset_count} 个分析失败的帖子为未分析状态",
        "data": {"reset_count": reset_count}
    }


@router.post("/fix-sector-mismatch")
async def fix_sector_mismatch(dry_run: bool = True, db: Session = Depends(get_db)):
    """
    修复板块和基金不匹配的预测数据

    Args:
        dry_run: True=试运行（只检查不修改），False=正式执行修复
    """
    from src.models.database import Prediction
    from src.constants.sector_fund_map import get_fund_for_sector, normalize_sector_name
    from src.services.sector_fund_service import get_sector_fund_service

    predictions = db.query(Prediction).filter(
        Prediction.is_deleted == False
    ).all()

    # 多层匹配：数据库映射 > 硬编码表
    service = get_sector_fund_service(db)

    fixed_count = 0
    mismatch_details = []

    for pred in predictions:
        sector = pred.sector or ''
        current_fund_code = pred.fund_code or ''

        if not sector:
            continue

        # 标准化板块名
        standard_sector = normalize_sector_name(sector)

        # 第1层：数据库映射（用户编辑优先）
        correct_fund = service.get_fund_by_sector(standard_sector)
        if not correct_fund:
            # 第2层：硬编码表
            correct_fund = get_fund_for_sector(standard_sector)
        if not correct_fund:
            continue

        correct_code = correct_fund.get("code", "")
        correct_name = correct_fund.get("name", "")

        # 检查是否匹配
        if current_fund_code and current_fund_code != correct_code:
            mismatch_details.append({
                "id": pred.id,
                "sector": sector,
                "current_fund_code": current_fund_code,
                "current_fund_name": pred.fund_name or "",
                "correct_fund_code": correct_code,
                "correct_fund_name": correct_name
            })

            if not dry_run:
                pred.fund_code = correct_code
                pred.fund_name = correct_name
                fixed_count += 1

    if not dry_run:
        db.commit()

    return {
        "success": True,
        "message": f"{'试运行' if dry_run else '已修复'}: 发现 {len(mismatch_details)} 个不匹配，{'将' if dry_run else '已'}修复 {fixed_count if not dry_run else len(mismatch_details)} 个",
        "data": {
            "dry_run": dry_run,
            "total_mismatch": len(mismatch_details),
            "fixed_count": fixed_count if not dry_run else 0,
            "details": mismatch_details[:50]  # 最多返回50条详情
        }
    }


@router.get("/batch-analyze/status")
async def get_batch_analyze_status():
    """获取批量分析状态"""
    return {
        "success": True,
        "data": {
            "in_progress": _batch_analyzing
        }
    }


@router.post("/cleanup-low-quality")
async def cleanup_low_quality_posts(dry_run: bool = True, db: Session = Depends(get_db)):
    """
    清理低质量帖子（太短、广告、闲聊）

    Args:
        dry_run: True=试运行（只检查不删除），False=正式执行删除
    """
    from src.models.database import Post

    service = PostService(db)
    posts = db.query(Post).filter(Post.analyzed == False).all()

    to_delete = []
    for post in posts:
        is_low, reason = service._is_low_quality_post(post.title or "", post.content or "")
        if is_low:
            to_delete.append({
                "id": post.id,
                "title": (post.title or "")[:50],
                "content_preview": (post.content or "")[:50],
                "reason": reason
            })

    if not dry_run:
        for item in to_delete:
            post = db.query(Post).filter(Post.id == item["id"]).first()
            if post:
                db.delete(post)
        db.commit()

    return {
        "success": True,
        "message": f"{'试运行' if dry_run else '已清理'}: 发现 {len(to_delete)} 个低质量帖子",
        "data": {
            "dry_run": dry_run,
            "count": len(to_delete),
            "deleted": len(to_delete) if not dry_run else 0,
            "details": to_delete[:50]
        }
    }


@router.post("/batch-analyze")
async def batch_analyze_posts(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    批量分析帖子（异步模式，立即返回，后台逐个分析）
    避免同步分析超时导致重复触发
    """
    global _batch_analyzing
    
    if _batch_analyzing:
        return {
            "success": True,
            "message": "批量分析正在进行中，请稍候...",
            "data": {"analyzed": 0, "failed": 0, "in_progress": True}
        }
    
    service = PostService(db)
    unanalyzed_count = len(service.get_unanalyzed(limit=100))
    
    if unanalyzed_count == 0:
        return {
            "success": True,
            "message": "没有需要分析的帖子",
            "data": {"analyzed": 0, "failed": 0}
        }
    
    _batch_analyzing = True
    background_tasks.add_task(_batch_analyze_background)
    
    return {
        "success": True,
        "message": f"已开始后台分析 {unanalyzed_count} 个帖子，请稍后刷新查看结果",
        "data": {"analyzed": 0, "failed": 0, "in_progress": True, "total": unanalyzed_count}
    }


@router.get("/{post_id}")
async def get_post(post_id: int, db: Session = Depends(get_db)):
    """获取帖子详情"""
    service = PostService(db)
    post = service.get_post_detail(post_id)
    
    if not post:
        raise HTTPException(status_code=404, detail="帖子不存在")
    
    return {
        "success": True,
        "data": post
    }


@router.delete("/{post_id}")
async def delete_post(post_id: int, db: Session = Depends(get_db)):
    """删除帖子"""
    service = PostService(db)
    try:
        success = service.delete_post(post_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not success:
        raise HTTPException(status_code=404, detail="帖子不存在")

    return {"success": True, "message": "帖子删除成功"}