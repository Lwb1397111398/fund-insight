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
async def get_posts(
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
            "success": result.get("success", True),
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

    # 使用分页查询避免一次性加载大量数据到内存
    predictions_query = db.query(Prediction).filter(
        Prediction.is_deleted == False
    ).yield_per(100)

    service = get_sector_fund_service(db)

    # 缓存 API 搜索结果，避免重复调用
    _api_cache = {}

    def find_correct_fund(standard_sector: str):
        """6层匹配：硬编码表(无需审查) > 已审查DB > 未审查DB > FundInfo > API"""
        # 第1层：硬编码表（ETF，无需审查，最可靠）
        fund = get_fund_for_sector(standard_sector)
        if fund:
            return fund, "硬编码表"

        # 第2层：已审查的数据库映射（用户编辑/审查过的）
        fund = service.get_fund_by_sector(standard_sector)
        if fund and fund.get('reviewed'):
            return fund, "已审查DB"

        # 第3层：未审查的数据库映射（自动学习的）
        if fund:
            return fund, "未审查DB"

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

    for pred in predictions_query:
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
async def get_batch_analyze_status(db: Session = Depends(get_db)):
    """兼容入口：从数据库读取最近一次帖子分析任务。"""
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.task_type == "posts"
    ).order_by(BatchAnalysisTask.created_at.desc()).first()
    if not task:
        return {
            "success": True,
            "data": {"in_progress": False, "status": "idle"},
        }

    data = PostAnalysisService.serialize_job(task)
    data["in_progress"] = task.status in ("pending", "running")
    return {
        "success": True,
        "data": data,
    }


@router.post("/analysis-jobs")
async def start_analysis_job(
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
async def get_analysis_job(task_id: int, db: Session = Depends(get_db)):
    task = db.query(BatchAnalysisTask).filter(
        BatchAnalysisTask.id == task_id,
        BatchAnalysisTask.task_type == "posts",
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "data": PostAnalysisService.serialize_job(task)}


@router.post("/analysis-jobs/{task_id}/resume")
async def resume_analysis_job(
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
async def cancel_analysis_job(task_id: int, db: Session = Depends(get_db)):
    try:
        task = PostAnalysisService.cancel_job(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True, "message": "任务已取消", "data": PostAnalysisService.serialize_job(task)}


@router.post("/cleanup-low-quality")
async def cleanup_low_quality_posts(
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


@router.post("/batch-analyze")
async def batch_analyze_posts(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """兼容入口：创建数据库持久化任务并立即返回。"""
    task, created = PostAnalysisService.create_job(db, limit=100)
    if task.status == "pending":
        background_tasks.add_task(_batch_analyze_background, task.id)
    data = PostAnalysisService.serialize_job(task)
    data.update({
        "analyzed": task.success_count or 0,
        "failed": task.failed_count or 0,
        "in_progress": task.status in ("pending", "running"),
        "total": task.total_count or 0,
    })
    return {
        "success": True,
        "message": (
            f"已开始后台分析 {task.total_count or 0} 个帖子，请稍后查看进度"
            if created else "批量分析任务已存在，请查看当前进度"
        ),
        "data": data,
    }


@router.post("/{post_id}/analyze")
async def analyze_post(
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
async def get_post_delete_preview(post_id: int, db: Session = Depends(get_db)):
    preview = PostService(db).get_delete_preview(post_id)
    if not preview:
        raise HTTPException(status_code=404, detail="帖子不存在")
    return {"success": True, "data": preview}


@router.patch("/{post_id}")
async def update_post(
    post_id: int,
    update: PostUpdate,
    db: Session = Depends(get_db),
):
    service = PostService(db)
    try:
        post = service.update_post_fields(
            post_id,
            update.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not post:
        raise HTTPException(status_code=404, detail="帖子不存在")
    return {
        "success": True,
        "message": "帖子已更新",
        "data": service.get_post_detail(post_id),
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
async def delete_post(
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
