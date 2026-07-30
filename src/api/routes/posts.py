"""
帖子路由
处理帖子相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date

from src.api.deps import get_db
from src.api.schemas.post import PostAnalysisJobRequest, PostCreate, PostUpdate
from src.core.safety import destructive_cleanup_enabled
from src.models.database import BatchAnalysisTask
from src.services.post_analysis_service import PostAnalysisService
from src.services.post_service import PostService

router = APIRouter(prefix="/posts", tags=["帖子"])


def _batch_analyze_background(task_id: int):
    """使用独立短会话运行持久化任务。"""
    PostAnalysisService.run_job(task_id)


@router.get("")
def get_posts(
    skip: int = 0,
    limit: int = 1000,
    blogger_id: Optional[int] = None,
    analyzed: Optional[bool] = None,
    keyword: Optional[str] = None,
    analysis_status: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    quality: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取帖子列表"""
    page = PostService(db).get_posts_page(
        skip=skip,
        limit=limit,
        blogger_id=blogger_id,
        analyzed=analyzed,
        keyword=keyword,
        analysis_status=analysis_status,
        start_date=start_date,
        end_date=end_date,
        quality=quality,
    )
    return {
        "success": True,
        "data": page["data"],
        "meta": page["meta"],
    }


@router.post("")
def create_post(post: PostCreate, db: Session = Depends(get_db)):
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
            "success": result.get("success", True),
            "message": result.get("message", "帖子添加成功"),
            "data": result
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))



@router.post("/analysis-jobs")
def start_analysis_job(
    payload: PostAnalysisJobRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """创建可恢复的帖子分析任务。"""
    task, created = PostAnalysisService.create_job(
        db,
        post_ids=payload.post_ids,
        limit=payload.limit,
    )
    task_post_ids = set((task.task_params or {}).get("post_ids") or [])
    requested_post_ids = set(payload.post_ids or [])
    if not created and requested_post_ids and not requested_post_ids.issubset(task_post_ids):
        raise HTTPException(status_code=409, detail="已有其他帖子分析任务，请完成后重试")
    if task.status == "pending":
        background_tasks.add_task(_batch_analyze_background, task.id)
    return {
        "success": True,
        "message": "帖子分析任务已创建" if created else "已有帖子分析任务",
        "data": PostAnalysisService.serialize_job(task),
    }


@router.get("/analysis-jobs/{task_id}")
def get_analysis_job(task_id: int, db: Session = Depends(get_db)):
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.id == task_id,
        BatchAnalysisTask.task_type == "posts",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "data": PostAnalysisService.serialize_job(task)}


@router.post("/analysis-jobs/{task_id}/resume")
def resume_analysis_job(
    task_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    try:
        task = PostAnalysisService.resume_job(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_batch_analyze_background, task.id)
    return {"success": True, "message": "任务已恢复", "data": PostAnalysisService.serialize_job(task)}


@router.post("/analysis-jobs/{task_id}/cancel")
def cancel_analysis_job(task_id: int, db: Session = Depends(get_db)):
    try:
        task = PostAnalysisService.cancel_job(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True, "message": "任务已取消", "data": PostAnalysisService.serialize_job(task)}


@router.post("/cleanup-low-quality")
def cleanup_low_quality_posts(
    request: Request,
    dry_run: bool = True,
    db: Session = Depends(get_db),
):
    """
    清理低质量帖子（太短、广告、闲聊）

    Args:
        dry_run: True=试运行（只检查不删除），False=正式执行删除
    """
    if not dry_run:
        if not destructive_cleanup_enabled():
            raise HTTPException(
                status_code=403,
                detail="低质量帖子清理已禁用。请在隔离维护环境显式设置 ENABLE_DATA_CLEANUP=true 后再使用",
            )
        if request.headers.get("X-Danger-Confirm") != "cleanup-data":
            raise HTTPException(status_code=403, detail="缺少数据清理确认头")
        raise HTTPException(
            status_code=409,
            detail="低质量帖子自动删除已停用。请使用 quality=low 筛选后逐条查看删除预览并确认",
        )

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

    return {
        "success": True,
        "message": f"候选检查完成：发现 {len(to_delete)} 个低质量帖子，请逐条确认",
        "data": {
            "dry_run": True,
            "count": len(to_delete),
            "deleted": 0,
            "details": to_delete[:50]
        }
    }


@router.post("/{post_id}/analyze")
def analyze_post(
    post_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """将单条帖子加入持久化分析任务。"""
    task, created = PostAnalysisService.create_job(db, post_ids=[post_id], limit=1)
    task_post_ids = list((task.task_params or {}).get("post_ids") or [])
    if not created and post_id not in task_post_ids:
        raise HTTPException(status_code=409, detail="已有其他帖子分析任务，请完成后重试")
    if task.status == "pending":
        background_tasks.add_task(_batch_analyze_background, task.id)
    return {
        "success": True,
        "message": "帖子已加入分析任务" if created else "帖子已在分析任务中",
        "data": PostAnalysisService.serialize_job(task),
    }


@router.get("/{post_id}/delete-preview")
def get_post_delete_preview(post_id: int, db: Session = Depends(get_db)):
    preview = PostService(db).get_delete_preview(post_id)
    if not preview:
        raise HTTPException(status_code=404, detail="帖子不存在")
    return {"success": True, "data": preview}


@router.get("/{post_id}")
def get_post(post_id: int, db: Session = Depends(get_db)):
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
def delete_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """经明确确认后彻底删除帖子和关联运行资料。"""
    if request.headers.get("X-Danger-Confirm") != "delete-post":
        raise HTTPException(status_code=403, detail="缺少帖子彻底删除确认头")
    try:
        result = PostService(db).delete_post_permanently(post_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"帖子删除失败: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="帖子不存在")
    return {"success": True, "message": "帖子及关联资料已彻底删除", "data": result}
