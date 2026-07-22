"""
预测路由
处理预测相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, timedelta
import logging

from src.api.deps import get_db
from src.models.database import Prediction
from src.services.prediction_service import PredictionService
from src.services.prediction_verify_service import PredictionVerifyService
from src.services.prediction_verify_task import prediction_verify_task
from src.api.schemas.prediction import PredictionUpdate
from src.services.prediction_maintenance_service import PredictionMaintenanceService

router = APIRouter(prefix="/predictions", tags=["预测"])
logger = logging.getLogger(__name__)


class PredictionVerify(BaseModel):
    actual_change: float
    is_correct: bool
    ai_judgment: Optional[str] = None


@router.get("")
async def get_predictions(
    skip: int = 0,
    limit: int = 1000,
    blogger_id: Optional[int] = None,
    status: Optional[str] = None,
    is_expired: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """获取预测列表"""
    service = PredictionService(db)
    predictions = service.get_predictions_with_filters(
        skip=skip,
        limit=limit,
        blogger_id=blogger_id,
        status=status,
        is_expired=is_expired
    )
    
    return {
        "success": True,
        "data": predictions
    }


@router.get("/{prediction_id}")
async def get_prediction_detail(prediction_id: int, db: Session = Depends(get_db)):
    """获取预测详情"""
    service = PredictionService(db)
    prediction = service.get_prediction_detail(prediction_id)
    
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {
        "success": True,
        "data": prediction
    }


@router.put("/{prediction_id}")
async def update_prediction(
    prediction_id: int,
    update_data: PredictionUpdate,
    db: Session = Depends(get_db)
):
    """安全更新待验证预测。"""
    service = PredictionService(db)
    try:
        prediction = service.update_prediction_fields(
            prediction_id,
            update_data.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {
        "success": True,
        "message": "预测更新成功",
        "data": {
            "id": prediction.id,
            "sector": prediction.sector,
            "fund_code": prediction.fund_code,
            "fund_name": prediction.fund_name,
            "prediction_type": prediction.prediction_type,
            "confidence": prediction.confidence,
            "prediction_period": prediction.prediction_period,
            "target_date": prediction.target_date.isoformat() if prediction.target_date else None,
        }
    }


@router.delete("/{prediction_id}")
async def delete_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """删除预测"""
    service = PredictionService(db)
    success = service.delete_prediction(prediction_id)

    if not success:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {"success": True, "message": "预测已归档，可在回收站恢复"}


@router.post("/{prediction_id}/restore")
async def restore_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """恢复归档预测。"""
    if not PredictionService(db).restore_prediction(prediction_id):
        raise HTTPException(status_code=404, detail="未找到可恢复的预测")
    return {"success": True, "message": "预测已恢复"}


@router.post("/{prediction_id}/verify")
async def verify_prediction(
    prediction_id: int,
    verify_data: PredictionVerify,
    db: Session = Depends(get_db)
):
    """验证预测"""
    service = PredictionService(db)
    
    prediction = service.verify(
        prediction_id=prediction_id,
        actual_change=verify_data.actual_change,
        is_correct=verify_data.is_correct,
        ai_judgment=verify_data.ai_judgment
    )
    
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")
    
    return {
        "success": True,
        "message": "预测验证成功",
        "data": {
            "id": prediction.id,
            "status": prediction.status,
            "is_correct": prediction.is_correct,
            "actual_change": prediction.actual_change
        }
    }


@router.post("/{prediction_id}/auto-verify")
async def auto_verify_prediction(
    prediction_id: int,
    db: Session = Depends(get_db)
):
    """自动验证预测（支持所有周期，包括超短期）"""
    service = PredictionVerifyService(db)
    result = service.verify_prediction(prediction_id)
    
    return result


@router.get("/{prediction_id}/verify-status")
async def get_prediction_verify_status(
    prediction_id: int,
    db: Session = Depends(get_db)
):
    """获取预测的验证状态（是否可以验证、数据是否充足）"""
    service = PredictionVerifyService(db)
    result = service.get_verification_status(prediction_id)
    
    return {
        "success": True,
        "data": result
    }


@router.post("/rollback-invalid")
async def rollback_invalid_verifications(
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
async def sync_sector_mapping(
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
async def verify_all_predictions(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
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
async def get_verify_all_status(db: Session = Depends(get_db)):
    """获取批量预测验证后台任务状态"""
    return {
        "success": True,
        "data": prediction_verify_task.status(db=db)
    }


@router.post("/verify-expired")
async def verify_expired_predictions(db: Session = Depends(get_db)):
    """补救验证所有已过期但尚未验证的预测"""
    service = PredictionVerifyService(db)
    result = service.verify_expired_pending()
    
    return result


@router.get("/stats/overview")
async def get_prediction_stats(
    blogger_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """获取预测统计"""
    service = PredictionService(db)
    stats = service.get_stats(blogger_id=blogger_id)
    
    return {
        "success": True,
        "data": stats
    }


@router.get("/verify/progress")
async def get_verify_progress(db: Session = Depends(get_db)):
    """获取验证进度"""
    service = PredictionService(db)
    progress = service.get_verify_progress()
    
    return {
        "success": True,
        "data": progress
    }


@router.get("/verify/failed")
async def get_failed_predictions(db: Session = Depends(get_db)):
    """获取验证失败的预测"""
    service = PredictionService(db)
    failed = service.get_failed_predictions()
    
    return {
        "success": True,
        "data": failed
    }


@router.get("/verify/expiring")
async def get_expiring_predictions(
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db)
):
    """获取即将到期的预测"""
    service = PredictionService(db)
    expiring = service.get_expiring_predictions(days=days)
    
    return {
        "success": True,
        "data": expiring
    }


@router.get("/analysis/anomalies")
async def get_anomaly_predictions(db: Session = Depends(get_db)):
    """异常预测检测"""
    service = PredictionService(db)
    anomalies = service.get_anomaly_predictions()
    
    return {
        "success": True,
        "data": anomalies
    }


@router.get("/export/data")
async def export_predictions(
    blogger_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """导出预测数据"""
    service = PredictionService(db)
    predictions = service.get_predictions_for_export(
        blogger_id=blogger_id,
        start_date=start_date,
        end_date=end_date
    )
    
    return {
        "success": True,
        "data": predictions
    }


@router.get("/history/lookup")
async def history_lookup(
    fund_code: Optional[str] = None,
    sector: Optional[str] = None,
    days: int = Query(30, ge=7, le=365),
    db: Session = Depends(get_db)
):
    """历史回溯查询"""
    service = PredictionService(db)
    history = service.get_history_lookup(
        fund_code=fund_code,
        sector=sector,
        days=days
    )
    
    return {
        "success": True,
        "data": history
    }


@router.get("/by-blogger/{blogger_id}")
async def get_predictions_by_blogger(
    blogger_id: int,
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """获取博主的预测列表"""
    service = PredictionService(db)
    predictions = service.get_by_blogger(blogger_id, skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "status": p.status,
                "is_correct": p.is_correct
            }
            for p in predictions
        ]
    }


@router.get("/by-fund/{fund_code}")
async def get_predictions_by_fund(
    fund_code: str,
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """获取基金的预测列表"""
    service = PredictionService(db)
    predictions = service.get_by_fund(fund_code, skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "status": p.status,
                "is_correct": p.is_correct
            }
            for p in predictions
        ]
    }


@router.get("/active/list")
async def get_active_predictions(
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """获取活跃预测（未过期且未验证）"""
    service = PredictionService(db)
    predictions = service.get_active(skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "target_date": p.target_date.isoformat() if p.target_date else None
            }
            for p in predictions
        ]
    }


@router.get("/pending-verification/list")
async def get_pending_verification(
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db)
):
    """获取待验证的预测"""
    service = PredictionService(db)
    predictions = service.get_pending_verification(days=days)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "target_date": p.target_date.isoformat() if p.target_date else None
            }
            for p in predictions
        ]
    }


@router.get("/expired/list")
async def get_expired_predictions(db: Session = Depends(get_db)):
    """获取已过期的预测"""
    service = PredictionService(db)
    predictions = service.get_expired()
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "is_correct": p.is_correct,
                "actual_change": p.actual_change,
                "target_date": p.target_date.isoformat() if p.target_date else None
            }
            for p in predictions
        ]
    }


@router.get("/by-sector/{sector}")
async def get_predictions_by_sector(
    sector: str,
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """根据板块获取预测"""
    service = PredictionService(db)
    predictions = service.get_by_sector(sector, skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None
            }
            for p in predictions
        ]
    }


@router.post("/merge-similar")
async def merge_similar_predictions(db: Session = Depends(get_db)):
    """兼容旧入口：只扫描重复候选，不再删除原始预测。"""
    result = PredictionMaintenanceService(db).scan_duplicate_groups()
    return {
        "success": True,
        "message": f"重复检查完成：发现 {result['duplicate_groups']} 组候选，未修改任何预测",
        "data": result,
    }


@router.get("/by-type/{prediction_type}")
async def get_predictions_by_type(
    prediction_type: str,
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """根据预测类型获取预测"""
    service = PredictionService(db)
    predictions = service.get_by_type(prediction_type, skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": p.id,
                "blogger_id": p.blogger_id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None
            }
            for p in predictions
        ]
    }
