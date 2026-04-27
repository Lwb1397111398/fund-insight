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


def analyze_post_background(blogger_id: int, post_id: int, content: str, title: str, post_date: date):
    """后台分析帖子的任务"""
    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        service = PostService(db)
        service.analyze_post_async(post_id)
        print(f"[Background] 帖子 {post_id} 分析完成")
    except Exception as e:
        print(f"[Background] 帖子 {post_id} 分析失败: {e}")
    finally:
        db.close()


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
async def create_post(post: PostCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """添加帖子并自动分析（支持异步模式）"""
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
        
        if post.async_mode and result.get("id"):
            background_tasks.add_task(
                analyze_post_background,
                post.blogger_id,
                result["id"],
                post.content,
                result["title"],
                post.post_date
            )
        
        return {
            "success": True,
            "message": result.get("message", "帖子添加成功"),
            "data": result
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/batch-analyze")
async def batch_analyze_posts(
    db: Session = Depends(get_db)
):
    """
    批量分析帖子（一键分析未分析的帖子）
    """
    service = PostService(db)
    result = service.batch_analyze_posts()
    
    return {
        "success": True,
        "message": result["message"],
        "data": {
            "analyzed": result["analyzed"],
            "failed": result["failed"]
        }
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
    success = service.delete_post(post_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="帖子不存在")
    
    return {"success": True, "message": "帖子删除成功"}