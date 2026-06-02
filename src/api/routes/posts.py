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
    修复板块和基金不匹配的预测数据（5层匹配，与分析时逻辑一致）

    Args:
        dry_run: True=试运行（只检查不修改），False=正式执行修复
    """
    from src.models.database import Prediction, FundInfo, SectorFundMapping
    from src.constants.sector_fund_map import get_fund_for_sector, normalize_sector_name
    from src.services.sector_fund_service import get_sector_fund_service

    # 确保 session 干净（连接池可能返回脏 session）
    try:
        db.rollback()
    except Exception:
        pass

    predictions = db.query(Prediction).filter(
        Prediction.is_deleted == False
    ).all()

    service = get_sector_fund_service(db)

    # 缓存 API 搜索结果，避免重复调用
    _api_cache = {}

    def find_correct_fund(standard_sector: str):
        """5层匹配，与 llm_analyzer._fill_fund_from_sector 逻辑一致"""
        # 第1层：数据库映射（用户编辑优先）
        fund = service.get_fund_by_sector(standard_sector)
        if fund:
            return fund, "数据库映射"

        # 第2层：硬编码表
        fund = get_fund_for_sector(standard_sector)
        if fund:
            return fund, "硬编码表"

        # 第3层：FundInfo 表（按 sector_type 搜索）
        fund_info = db.query(FundInfo).filter(FundInfo.sector_type == standard_sector).first()
        if fund_info:
            return {'code': fund_info.fund_code, 'name': fund_info.fund_name}, "FundInfo精确"

        fund_info = db.query(FundInfo).filter(FundInfo.sector_type.contains(standard_sector)).first()
        if fund_info:
            return {'code': fund_info.fund_code, 'name': fund_info.fund_name}, "FundInfo模糊"

        funds = db.query(FundInfo).filter(FundInfo.sector_type != None).all()
        for f in funds:
            if f.sector_type and (f.sector_type in standard_sector or standard_sector in f.sector_type):
                return {'code': f.fund_code, 'name': f.fund_name}, "FundInfo反向"

        # 第4层：天天基金 API 搜索
        if standard_sector in _api_cache:
            return _api_cache[standard_sector], "API缓存"

        try:
            from src.fund.fund_api import FundAPI
            api = FundAPI()
            results = api.search_fund(standard_sector)
            if results:
                r = results[0]
                api_fund = {'code': r.get('fund_code', ''), 'name': r.get('fund_name', '')}
                _api_cache[standard_sector] = api_fund

                # 自动学习：保存到数据库
                if not dry_run:
                    try:
                        existing = db.query(SectorFundMapping).filter(
                            SectorFundMapping.sector_name == standard_sector
                        ).first()
                        if not existing:
                            mapping = SectorFundMapping(
                                sector_name=standard_sector,
                                fund_code=api_fund['code'],
                                fund_name=api_fund['name']
                            )
                            db.add(mapping)
                    except Exception:
                        db.rollback()

                return api_fund, "API搜索"
        except Exception as e:
            pass

        return None, None

    fixed_count = 0
    mismatch_details = []
    no_match_details = []

    for pred in predictions:
        sector = pred.sector or ''
        current_fund_code = pred.fund_code or ''

        if not sector:
            continue

        # 标准化板块名
        standard_sector = normalize_sector_name(sector)

        # 5层匹配
        correct_fund, match_source = find_correct_fund(standard_sector)

        if not correct_fund:
            if current_fund_code:
                no_match_details.append({
                    "id": pred.id,
                    "sector": sector,
                    "current_fund_code": current_fund_code,
                    "current_fund_name": pred.fund_name or ""
                })
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
                "correct_fund_name": correct_name,
                "match_source": match_source
            })

            if not dry_run:
                pred.fund_code = correct_code
                pred.fund_name = correct_name
                fixed_count += 1
                # 级联清理：删除低优先级层中同板块不同基金的冲突数据
                service.cascade_cleanup_conflicts(standard_sector, correct_code, correct_name)

    if not dry_run:
        db.commit()

    total_issues = len(mismatch_details) + len(no_match_details)
    msg_parts = []
    if mismatch_details:
        msg_parts.append(f"{len(mismatch_details)} 个基金不匹配")
    if no_match_details:
        msg_parts.append(f"{len(no_match_details)} 个无法匹配基金")

    if not msg_parts:
        message = f"{'检查' if dry_run else '修复'}完成：所有预测的板块和基金匹配正确"
    else:
        action = '将' if dry_run else '已'
        message = f"{'试运行' if dry_run else '已修复'}: 发现 {'、'.join(msg_parts)}，{action}修复 {len(mismatch_details) if not dry_run else fixed_count} 个"

    return {
        "success": True,
        "message": message,
        "data": {
            "dry_run": dry_run,
            "total_mismatch": len(mismatch_details),
            "total_no_match": len(no_match_details),
            "fixed_count": fixed_count if not dry_run else 0,
            "details": mismatch_details[:50],
            "no_match_details": no_match_details[:20]
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