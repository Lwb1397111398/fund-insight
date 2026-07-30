"""
预测路由
处理预测相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Annotated, Optional, List
from datetime import date, datetime, timedelta
import logging

from src.api.deps import get_db
from src.models.database import Prediction
from src.services.prediction_service import PredictionService
from src.services.prediction_verify_service import PredictionVerifyService
from src.services.prediction_verify_task import prediction_verify_task
from src.api.schemas.prediction import PredictionUpdate
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from src.services.prediction_query_service import PredictionQueryService

router = APIRouter(prefix="/predictions", tags=["预测"])
logger = logging.getLogger(__name__)


class PredictionVerify(BaseModel):
    actual_change: float
    is_correct: bool
    ai_judgment: Optional[str] = None


@router.get("")
def get_predictions(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Optional[str] = None,
    blogger_id: Optional[int] = None,
    fund_code: Optional[str] = None,
    sector: Optional[str] = None,
    prediction_type: Optional[str] = None,
    status: Optional[str] = None,
    result: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    archive: str = "active",
    is_expired: Optional[bool] = None,
    skip: Annotated[Optional[int], Query(ge=0)] = None,
    limit: Annotated[Optional[int], Query(ge=1)] = None,
    db: Session = Depends(get_db)
):
    """分页查询预测列表；保留 skip/limit 作为兼容参数。"""
    if limit is not None:
        page_size = min(limit, 200)
    if skip is not None:
        page = skip // page_size + 1

    result_page = PredictionQueryService(db).search(
        page=page,
        page_size=page_size,
        search=search,
        blogger_id=blogger_id,
        fund_code=fund_code,
        sector=sector,
        prediction_type=prediction_type,
        status=status,
        result=result,
        start_date=start_date,
        end_date=end_date,
        archive=archive,
        is_expired=is_expired,
    )
    
    return {
        "success": True,
        "data": result_page["data"],
        "meta": result_page["meta"],
    }


@router.get("/{prediction_id}")
def get_prediction_detail(prediction_id: int, db: Session = Depends(get_db)):
    """获取预测详情"""
    prediction = PredictionQueryService(db).get_detail(prediction_id)
    
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {
        "success": True,
        "data": prediction
    }


@router.delete("/{prediction_id}")
def delete_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """删除预测"""
    service = PredictionService(db)
    success = service.delete_prediction(prediction_id)

    if not success:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {"success": True, "message": "预测已归档，可在回收站恢复"}


@router.post("/{prediction_id}/restore")
def restore_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """恢复归档预测。"""
    if not PredictionService(db).restore_prediction(prediction_id):
        raise HTTPException(status_code=404, detail="未找到可恢复的预测")
    return {"success": True, "message": "预测已恢复"}


@router.post("/rollback-invalid")
def rollback_invalid_verifications(
    request: Request,
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
):
    """预览或回溯数据不足的已验证预测。"""
    if not dry_run and request.headers.get("X-Danger-Confirm") != "rollback-predictions":
        raise HTTPException(
            status_code=403,
            detail="执行回溯需要确认头 X-Danger-Confirm: rollback-predictions",
        )
    service = PredictionVerifyService(db)
    result = service.rollback_invalid_verifications(min_data_points=2, dry_run=dry_run)

    return result


@router.post("/sync-sector-mapping")
def sync_sector_mapping(
    request: Request,
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
):
    """使用已审核映射预览或同步预测基金关联。"""
    if not dry_run and request.headers.get("X-Danger-Confirm") != "sync-prediction-mapping":
        raise HTTPException(
            status_code=403,
            detail="执行同步需要确认头 X-Danger-Confirm: sync-prediction-mapping",
        )
    try:
        result = PredictionMaintenanceService(db).sync_sector_mappings(dry_run=dry_run)

        # 构建详细消息
        parts = []
        if result['predictions_updated'] > 0:
            parts.append(f"更新 {result['predictions_updated']} 个预测")
        if result['verified_reset'] > 0:
            parts.append(f"重置 {result['verified_reset']} 个已验证预测")
        if result['funds_added'] > 0:
            parts.append(f"新增 {result['funds_added']} 个基金")
        if result['funds_sector_updated'] > 0:
            parts.append(f"更新 {result['funds_sector_updated']} 个基金板块")

        if parts:
            message = f"{'预览' if dry_run else '同步'}完成：" + "，".join(parts)
        else:
            pending = result.get("would_update", 0)
            if dry_run and pending:
                message = f"预览完成：将更新 {pending} 个预测"
            else:
                message = f"{'预览' if dry_run else '同步'}完成：无需更新（{result['predictions_unchanged']} 个预测未变，{result['predictions_no_mapping']} 个无映射）"

        return {
            "success": True,
            "message": message,
            "data": result
        }
    except Exception as e:
        logger.error(f"同步板块映射失败: {e}")
        return {
            "success": False,
            "message": f"同步失败: {str(e)}",
            "data": None
        }


def _count_due_predictions(db: Session, today: date) -> int:
    """统计真正可批量验证的预测：只包含预测周期已结束的记录。"""
    return db.query(Prediction).filter(
        Prediction.status == 'pending',
        Prediction.is_deleted == False,
        Prediction.prediction_type != 'flat',
        Prediction.target_date <= today
    ).count()


def _verify_all_background(task_id: int):
    """后台验证所有待验证预测"""
    from src.models.database import SessionLocal
    db = SessionLocal()
    result = None
    try:
        service = PredictionVerifyService(db)
        result = service.verify_all_pending()
        print(f"[Verify All] 后台验证完成: {result.get('message')}")
    except Exception as e:
        result = {"success": False, "message": f"后台验证失败: {e}"}
        print(f"[Verify All] {result['message']}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            prediction_verify_task.finish(result, db=db, task_id=task_id)
        finally:
            db.close()


@router.post("/verify-all")
def verify_all_predictions(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """验证所有待验证的预测（异步模式，跳过通道未开放的）"""
    status = prediction_verify_task.status(db=db)
    if status["in_progress"]:
        return {"success": True, "message": "验证正在进行中，请稍候...", "data": status}

    today = date.today()
    pending_count = _count_due_predictions(db, today)
    
    if pending_count == 0:
        return {"success": True, "message": "没有需要验证的预测", "data": {"total": 0}}
    
    start_result = prediction_verify_task.start(pending_count, db=db)
    if not start_result["success"]:
        return start_result

    task_id = start_result["data"]["task_id"]
    background_tasks.add_task(_verify_all_background, task_id)
    
    return {"success": True, "message": f"已开始后台验证 {pending_count} 个预测，请稍后等待完成", "data": start_result["data"]}


@router.get("/verify-all/status")
def get_verify_all_status(db: Session = Depends(get_db)):
    """获取批量预测验证后台任务状态"""
    return {
        "success": True,
        "data": prediction_verify_task.status(db=db)
    }




@router.post("/merge-similar")
def merge_similar_predictions(db: Session = Depends(get_db)):
    """兼容旧入口：只扫描重复候选，不再删除原始预测。"""
    result = PredictionMaintenanceService(db).scan_duplicate_groups()
    return {
        "success": True,
        "message": f"重复检查完成：发现 {result['duplicate_groups']} 组候选，未修改任何预测",
        "data": result,
    }


