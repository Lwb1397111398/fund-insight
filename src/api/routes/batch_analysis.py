"""
批量分析路由 - 支持断点续传和日志记录
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Tuple
from datetime import date, datetime
import time
import traceback

from src.api.deps import get_db
from src.models.database import Blogger, Post, Prediction, BatchAnalysisTask, AnalysisLog
from src.analyzer.llm_analyzer import get_analyzer
from src.fund.fund_auto_manager import fund_auto_manager
from src.services.post_service import PostService

router = APIRouter(prefix="/batch-analysis", tags=["批量分析"])


def _match_fund_with_fallback(
    pred: dict,
    sector: str,
    fund_auto_manager,
    llm_analyzer,
    db: Session
) -> Tuple[Optional[str], Optional[str]]:
    """
    三级降级基金匹配机制
    
    优先级（按可靠性排序）：
    1. 使用 fund_auto_manager 自动匹配（优先查本地映射表）
    2. 使用 LLM 分析器的板块映射表（经过验证的映射）
    3. 使用本地默认映射表
    4. 使用LLM返回的fund_code（作为最后补充，需严格验证）
    
    Args:
        pred: 预测字典
        sector: 板块名称
        fund_auto_manager: 基金自动管理器
        llm_analyzer: LLM分析器
        db: 数据库会话
        
    Returns:
        (fund_code, fund_name)
    """
    from src.models.database import FundInfo
    
    # 第一级：使用 fund_auto_manager 自动匹配（优先查本地映射表）
    try:
        success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, db)
        if success and fund:
            print(f"[Fund Match] Level 1 (Auto Manager): {message}")
            return fund.fund_code, fund.fund_name
    except Exception as e:
        print(f"[Fund Match] Level 1 failed: {e}")
    
    # 第二级：使用 LLM 分析器的板块映射表（经过验证的映射）
    try:
        fund_info = llm_analyzer.get_fund_for_sector(sector)
        if fund_info:
            fund_code = fund_info.get("code")
            fund_name = fund_info.get("name")
            print(f"[Fund Match] Level 2 (LLM Mapper): {fund_name} ({fund_code})")
            return fund_code, fund_name
    except Exception as e:
        print(f"[Fund Match] Level 2 failed: {e}")
    
    # 第三级：使用本地默认映射表
    DEFAULT_FUND_MAP = {
        '白酒': ('161725', '招商中证白酒指数'),
        '医药': ('001017', '华夏医疗健康混合A'),
        '半导体': ('512480', '国泰CES半导体芯片ETF'),
        '新能源': ('516790', '国泰中证新能源汽车ETF'),
        '军工': ('005633', '华夏军工安全混合A'),
        '银行': ('001594', '易方达中证银行指数LOF'),
        '券商': ('512880', '证券ETF'),
        '房地产': ('160218', '国泰国证房地产指数'),
        '有色金属': ('160221', '国泰国证有色金属行业指数'),
        '煤炭': ('161724', '招商中证煤炭等权指数LOF'),
        '黄金': ('000218', '易方达黄金ETF联接A'),
        '港股': ('513180', '恒生科技ETF'),
        '恒生科技': ('513180', '恒生科技ETF'),
        '互联网': ('515000', '互联网ETF'),
        '人工智能': ('015719', '华夏中证人工智能主题ETF联接A'),
        '消费': ('000083', '汇添富消费行业混合'),
        '电力': ('561170', '广发中证全指电力公用事业ETF'),
        '化工': ('159870', '鹏华中证细分化工产业ETF'),
        '石油': ('501017', '石油LOF'),
        '机器人': ('159770', '机器人ETF'),
        '通信': ('515880', '通信ETF'),
        '创新药': ('159858', '创新药ETF'),
        '消费电子': ('159732', '消费电子ETF'),
    }
    
    if sector in DEFAULT_FUND_MAP:
        fund_code, fund_name = DEFAULT_FUND_MAP[sector]
        print(f"[Fund Match] Level 3 (Default Mapper): {fund_name} ({fund_code})")
        return fund_code, fund_name
    
    # 第四级：使用LLM返回的fund_code（作为最后补充，需严格验证）
    llm_fund_code = pred.get("fund_code")
    llm_fund_name = pred.get("fund_name")
    
    if llm_fund_code and str(llm_fund_code).strip():
        # 严格验证：必须是6位数字
        if len(str(llm_fund_code)) == 6 and str(llm_fund_code).isdigit():
            # 检查基金是否已存在于数据库（更可靠）
            existing_fund = db.query(FundInfo).filter(
                FundInfo.fund_code == str(llm_fund_code)
            ).first()
            
            if existing_fund:
                print(f"[Fund Match] Level 4 (LLM Result - Verified): {existing_fund.fund_name} ({llm_fund_code})")
                return existing_fund.fund_code, existing_fund.fund_name
            else:
                # 基金不存在于数据库，LLM返回的代码可能不可靠
                print(f"[Fund Match] Level 4 (LLM Result - Unverified): {llm_fund_name} ({llm_fund_code}) - 基金不存在，跳过")
    
    # 最终降级：返回None
    print(f"[Fund Match] All levels failed, using sector name: {sector}")
    return None, None


class BatchAnalysisRequest(BaseModel):
    """批量分析请求"""
    task_type: str = "posts"  # posts/predictions/viewpoints
    resume_task_id: Optional[int] = None  # 继续未完成的任务
    limit: int = 1000


class BatchAnalysisStatus(BaseModel):
    """批量分析状态"""
    task_id: int
    status: str
    total_count: int
    processed_count: int
    success_count: int
    failed_count: int
    progress: float


    estimated_remaining: Optional[int] = None


@router.post("/start")
async def start_batch_analysis(
    request: BatchAnalysisRequest,
    db: Session = Depends(get_db)
):
    """
    启动批量分析任务
    
    Args:
        request: 批量分析请求
    
    Returns:
        任务信息
    """
    # 检查是否要继续未完成的任务
    if request.resume_task_id:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == request.resume_task_id,
            BatchAnalysisTask.status == 'failed'
        ).first()
        
        if task:
            # 继续未完成的任务
            return {
                "success": True,
                "message": "继续未完成的任务",
                "data": {
                    "task_id": task.id,
                    "status": task.status,
                    "processed_count": task.processed_count,
                    "total_count": task.total_count,
                    "progress": (task.processed_count / task.total_count * 100) if task.total_count > 0 else 0
                }
            }
    
    # 创建新任务
    task = BatchAnalysisTask(
        task_type=request.task_type,
        status='pending',
        task_params={"limit": request.limit}
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    
    # 异步执行任务
    _execute_batch_analysis_task(task.id, db)
    
    return {
        "success": True,
        "message": "批量分析任务已启动",
        "data": {
            "task_id": task.id,
            "status": task.status,
            "total_count": 0,
            "processed_count": 0,
            "progress": 0
        }
    }


@router.get("/status/{task_id}")
async def get_batch_analysis_status(task_id: int, db: Session = Depends(get_db)):
    """
    获取批量分析任务状态
    
    Args:
        task_id: 任务 ID
    
    Returns:
        任务状态
    """
    task = db.query(BatchAnalysisTask).filter(BatchAnalysisTask.id == task_id).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    progress = (task.processed_count / task.total_count * 100) if task.total_count > 0 else 0
    estimated_remaining = None
    if task.started_at and task.processed_count > 0:
        elapsed = (datetime.now() - task.started_at).total_seconds()
        avg_time_per_item = elapsed / task.processed_count
        remaining_items = task.total_count - task.processed_count
        estimated_remaining = int(avg_time_per_item * remaining_items)
    
    return {
        "success": True,
        "data": {
            "task_id": task.id,
            "status": task.status,
            "total_count": task.total_count,
            "processed_count": task.processed_count,
            "success_count": task.success_count,
            "failed_count": task.failed_count,
            "progress": progress,
            "estimated_remaining": estimated_remaining,
            "error_message": task.error_message,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None
        }
    }


@router.post("/cancel/{task_id}")
async def cancel_batch_analysis(task_id: int, db: Session = Depends(get_db)):
    """
    取消批量分析任务
    
    Args:
        task_id: 任务 ID
    
    Returns:
        取消结果
    """
    task = db.query(BatchAnalysisTask).filter(BatchAnalysisTask.id == task_id).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status == 'completed':
        return {
            "success": False,
            "message": "任务已完成，无法取消"
        }
    
    task.status = 'cancelled'
    task.completed_at = datetime.now()
    db.commit()
    
    return {
        "success": True,
        "message": "任务已取消",
        "data": {
            "task_id": task.id,
            "status": task.status,
            "processed_count": task.processed_count
        }
    }


@router.get("/report/{task_id}")
async def get_batch_analysis_report(task_id: int, db: Session = Depends(get_db)):
    """
    获取批量分析报告
    
    Args:
        task_id: 任务 ID
    
    Returns:
        详细报告
    """
    task = db.query(BatchAnalysisTask).filter(BatchAnalysisTask.id == task_id).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 获取分析日志
    logs = db.query(AnalysisLog).filter(
        AnalysisLog.task_id == task_id
    ).order_by(AnalysisLog.created_at.desc()).limit(100).all()
    
    return {
        "success": True,
        "data": {
            "task": {
                "id": task.id,
                "task_type": task.task_type,
                "status": task.status,
                "total_count": task.total_count,
                "success_count": task.success_count,
                "failed_count": task.failed_count,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "duration": (task.completed_at - task.started_at).total_seconds() if task.completed_at and task.started_at else None
            },
            "logs": [
                {
                    "post_id": log.post_id,
                    "parse_success": log.parse_success,
                    "parse_method": log.parse_method,
                    "fund_match_level": log.fund_match_level,
                    "fund_code": log.fund_code,
                    "fund_name": log.fund_name,
                    "analysis_duration": log.analysis_duration,
                    "created_at": log.created_at.isoformat()
 if log.created_at else None
                }
                for log in logs
            ],
            "failed_items": task.failed_ids if task.failed_ids else []
        }
    }


def _execute_batch_analysis_task(task_id: int, db: Session):
    """
    执行批量分析任务（后台任务）
    
    Args:
        task_id: 任务 ID
        db: 数据库会话
    """
    llm_analyzer = get_analyzer()
    
    # 获取任务
    task = db.query(BatchAnalysisTask).filter(BatchAnalysisTask.id == task_id).first()
    if not task:
        return
    
    # 更新任务状态
    task.status = 'running'
    task.started_at = datetime.now()
    db.commit()
    
    try:
        # 获取未分析的帖子
        query = db.query(Post).filter(Post.analyzed == False)
        
        # 应用限制
        limit = task.task_params.get("limit", 100) if task.task_params else 100
        unanalyzed_posts = query.limit(limit).all()
        
        # 更新总数
        task.total_count = len(unanalyzed_posts)
        db.commit()
        
        if not unanalyzed_posts:
            task.status = 'completed'
            task.completed_at = datetime.now()
            task.result_summary = {"message": "没有需要分析的帖子"}
            db.commit()
            return
        
        # 初始化处理记录
        if not task.processed_ids:
            task.processed_ids = []
        if not task.failed_ids:
            task.failed_ids = []
        
        # 过滤已处理的帖子
        processed_ids_set = set(task.processed_ids)
        posts_to_process = [p for p in unanalyzed_posts if p.id not in processed_ids_set]
        
        # 处理每个帖子
        for post in posts_to_process:
            # 检查任务是否被取消
            db.refresh(task)
            if task.status == 'cancelled':
                print(f"[Batch Analysis] Task {task_id} cancelled")
                return
            
            start_time = time.time()
            
            try:
                # 分析帖子
                result = llm_analyzer.analyze_post(
                    title=post.title or "",
                    content=post.content,
                    post_date=post.post_date.isoformat() if post.post_date else None
                )
                
                # 标记帖子已分析
                post.analyzed = True
                post.analysis_result = result
                
                # 创建预测记录
                for pred in result.get("predictions", []):
                    sector = pred.get("sector", "")
                    
                    # 三级降级基金匹配
                    fund_code, fund_name = _match_fund_with_fallback(
                        pred=pred,
                        sector=sector,
                        fund_auto_manager=fund_auto_manager,
                        llm_analyzer=llm_analyzer,
                        db=db
                    )
                    
                    prediction = Prediction(
                        post_id=post.id,
                        blogger_id=post.blogger_id,
                        fund_code=fund_code,
                        fund_name=fund_name,
                        sector=sector,
                        sector_type=pred.get("sector_type", fund_auto_manager.get_category_for_sector(sector) if sector else "其他"),
                        prediction_type=pred.get("prediction_type"),
                        prediction_content=pred.get("prediction_content"),
                        confidence=pred.get("confidence", 50),
                        prediction_date=post.post_date,
                        prediction_period=pred.get("prediction_period", "1个月"),
                        target_date=llm_analyzer.calculate_target_date(
                            post.post_date,
                            pred.get("prediction_period", "1个月")
                        ),
                        next_verify_date=llm_analyzer.calculate_next_verify_date(
                            post.post_date,
                            llm_analyzer.calculate_target_date(
                                post.post_date,
                                pred.get("prediction_period", "1个月")
                            )
                        )
                    )
                    db.add(prediction)
                
                # 记录处理成功
                task.processed_ids.append(post.id)
                task.processed_count += 1
                task.success_count += 1
                
                # 记录分析日志
                analysis_duration = time.time() - start_time
                log_entry = AnalysisLog(
                    task_id=task_id,
                    post_id=post.id,
                    llm_model=llm_analyzer.model,
                    parse_success=True,
                    parse_method="standard",
                    fund_match_level=1,
                    analysis_duration=analysis_duration
                )
                db.add(log_entry)
                
                # 定期提交进度
                if task.processed_count % 5 == 0:
                    db.commit()
                    print(f"[Batch Analysis] Progress: {task.processed_count}/{task.total_count}")
                
            except Exception as e:
                # 记录失败
                task.failed_ids.append({
                    "id": post.id,
                    "error": str(e)
                })
                task.failed_count += 1
                task.processed_count += 1
                
                # 记录失败日志
                analysis_duration = time.time() - start_time
                log_entry = AnalysisLog(
                    task_id=task_id,
                    post_id=post.id,
                    parse_success=False,
                    parse_error=str(e),
                    analysis_duration=analysis_duration
                )
                db.add(log_entry)
                
                print(f"[Batch Analysis] Failed to analyze post {post.id}: {e}")
        
        # 任务完成
        task.status = 'completed'
        task.completed_at = datetime.now()
        task.result_summary = {
            "analyzed": task.success_count,
            "failed": task.failed_count,
            "total": task.total_count
        }
        db.commit()
        
        print(f"[Batch Analysis] Task {task_id} completed: {task.success_count} success, {task.failed_count} failed")
        
    except Exception as e:
        # 任务失败
        task.status = 'failed'
        task.error_message = str(e)
        task.error_stack = traceback.format_exc()
        task.completed_at = datetime.now()
        db.commit()
        
        print(f"[Batch Analysis] Task {task_id} failed: {e}")
        print(traceback.format_exc())
