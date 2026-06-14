"""
预测路由
处理预测相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, timedelta
import logging

from src.api.deps import get_db
from src.models.database import Prediction
from src.services.prediction_service import PredictionService
from src.services.prediction_verify_service import PredictionVerifyService

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


class PredictionUpdate(BaseModel):
    sector: Optional[str] = None
    fund_code: Optional[str] = None
    fund_name: Optional[str] = None
    prediction_type: Optional[str] = None
    confidence: Optional[int] = None
    prediction_period: Optional[str] = None


@router.put("/{prediction_id}")
async def update_prediction(
    prediction_id: int,
    update_data: PredictionUpdate,
    db: Session = Depends(get_db)
):
    """更新预测（人工干预板块和基金）"""
    from src.models.database import Prediction as PredictionModel
    from src.constants.sector_fund_map import get_fund_for_sector, get_category_for_sector, normalize_sector_name
    from src.services.sector_fund_service import get_sector_fund_service

    prediction = db.query(PredictionModel).filter(PredictionModel.id == prediction_id).first()
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")

    # 更新板块（自动标准化：黑话/别名 → 标准名称）
    sector_changed = False
    if update_data.sector is not None and update_data.sector != prediction.sector:
        # 标准化板块名称（如 "酒" → "白酒"，"药" → "创新药"）
        standard_sector = normalize_sector_name(update_data.sector)
        prediction.sector = standard_sector
        prediction.sector_type = get_category_for_sector(standard_sector)
        sector_changed = True
        if standard_sector != update_data.sector:
            logger.info(f"[人工干预] 板块标准化: '{update_data.sector}' → '{standard_sector}'")

    # 更新基金
    fund_changed = False
    if update_data.fund_code is not None and update_data.fund_code != prediction.fund_code:
        prediction.fund_code = update_data.fund_code
        fund_changed = True
    if update_data.fund_name is not None and update_data.fund_name != prediction.fund_name:
        prediction.fund_name = update_data.fund_name
        fund_changed = True

    # 如果只修改了板块，自动匹配基金
    if sector_changed and not fund_changed:
        correct_fund = get_fund_for_sector(prediction.sector)
        if correct_fund:
            prediction.fund_code = correct_fund.get("code", "")
            prediction.fund_name = correct_fund.get("name", "")
            fund_changed = True

    # 【关键】如果基金修改了（且板块有值），保存到映射表并级联清理冲突
    if fund_changed and prediction.sector and prediction.fund_code:
        try:
            service = get_sector_fund_service(db)
            service.add_mapping(
                sector_name=prediction.sector,
                fund_code=prediction.fund_code,
                fund_name=prediction.fund_name
            )
            # 级联清理：删除低优先级层中同板块不同基金的冲突数据
            cleanup = service.cascade_cleanup_conflicts(
                sector_name=prediction.sector,
                fund_code=prediction.fund_code,
                fund_name=prediction.fund_name
            )
            if any(v > 0 for v in cleanup.values()):
                logger.info(f"[人工干预] 已清理冲突数据: {cleanup}")
            logger.info(f"[人工干预] 已保存板块映射: {prediction.sector} → {prediction.fund_name} ({prediction.fund_code})")
        except Exception as e:
            logger.warning(f"[人工干预] 保存板块映射失败: {e}")

    # 更新其他字段
    if update_data.prediction_type is not None:
        prediction.prediction_type = update_data.prediction_type
    if update_data.confidence is not None:
        prediction.confidence = max(0, min(100, update_data.confidence))
    if update_data.prediction_period is not None:
        prediction.prediction_period = update_data.prediction_period

    db.commit()

    return {
        "success": True,
        "message": "预测更新成功",
        "data": {
            "id": prediction.id,
            "sector": prediction.sector,
            "fund_code": prediction.fund_code,
            "fund_name": prediction.fund_name
        }
    }


@router.delete("/{prediction_id}")
async def delete_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """删除预测"""
    service = PredictionService(db)
    success = service.delete_prediction(prediction_id)

    if not success:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {"success": True, "message": "预测删除成功"}


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
async def rollback_invalid_verifications(db: Session = Depends(get_db)):
    """回溯数据不足的已验证预测"""
    service = PredictionVerifyService(db)
    result = service.rollback_invalid_verifications(min_data_points=2)

    return result


@router.post("/sync-sector-mapping")
async def sync_sector_mapping(db: Session = Depends(get_db)):
    """根据板块-基金映射同步预测关联和基金数据"""
    try:
        from src.fund.fund_sync_manager import fund_sync_manager
        result = fund_sync_manager.sync_predictions_by_sector_mapping(db)

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
            message = "同步完成：" + "，".join(parts)
        else:
            message = f"同步完成：无需更新（{result['predictions_unchanged']} 个预测未变，{result['predictions_no_mapping']} 个无映射）"

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


_verify_batch_running = False


def _verify_all_background():
    """后台验证所有待验证预测"""
    global _verify_batch_running
    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        service = PredictionVerifyService(db)
        result = service.verify_all_pending()
        print(f"[Verify All] 后台验证完成: {result.get('message')}")
    except Exception as e:
        print(f"[Verify All] 后台验证失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
        _verify_batch_running = False


@router.post("/verify-all")
async def verify_all_predictions(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """验证所有待验证的预测（异步模式，跳过通道未开放的）"""
    global _verify_batch_running
    
    if _verify_batch_running:
        return {"success": True, "message": "验证正在进行中，请稍候...", "data": {"in_progress": True}}
    
    service = PredictionVerifyService(db)
    today = date.today()
    pending_count = db.query(Prediction).filter(
        Prediction.status == 'pending',
        Prediction.is_deleted == False,
        Prediction.prediction_type != 'flat',
        Prediction.target_date <= today + timedelta(days=7)
    ).count()
    
    if pending_count == 0:
        return {"success": True, "message": "没有需要验证的预测", "data": {"total": 0}}
    
    _verify_batch_running = True
    background_tasks.add_task(_verify_all_background)
    
    return {"success": True, "message": f"已开始后台验证 {pending_count} 个预测，请稍后刷新查看结果", "data": {"total": pending_count, "in_progress": True}}


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
    """合并同类预测（相同基金、相同判断、相同目标日期，不同博主也可合并）
    
    合并规则：
    1. 相同 fund_code + prediction_type + target_date 的预测会被合并
    2. 保留置信度最高的预测
    3. 删除其他预测时，会正确更新博主的统计数据
    """
    from src.models.database import Prediction, Blogger
    from src.services.prediction_verify_service import PredictionVerifyService
    from src.utils.blogger_stats import update_blogger_stats_incremental
    from collections import defaultdict
    
    verify_service = PredictionVerifyService(db)
    
    predictions = db.query(Prediction).filter(
        Prediction.is_deleted == False
    ).all()
    
    logger.info(f"总预测数: {len(predictions)}")
    
    groups = defaultdict(list)
    for p in predictions:
        target_date_str = p.target_date.isoformat() if isinstance(p.target_date, (date, datetime)) else str(p.target_date)
        key = (p.fund_code, p.prediction_type, target_date_str)
        groups[key].append(p)
    
    logger.info(f"分组数: {len(groups)}")
    
    merged_count = 0
    deleted_count = 0
    blogger_updates = defaultdict(lambda: {"score": 0, "correct": 0, "verified": 0})
    
    for key, group in groups.items():
        if len(group) > 1:
            avg_confidence = sum(p.confidence for p in group) / len(group)
            
            main_prediction = max(group, key=lambda p: p.confidence)
            main_prediction.confidence = int(round(avg_confidence))
            
            for p in group:
                if p.id != main_prediction.id:
                    if p.verify_count and p.verify_count > 0:
                        score_to_deduct = p.verify_score if p.verify_score is not None else (100 if p.is_correct else 0)
                        blogger_updates[p.blogger_id]["score"] -= score_to_deduct
                        blogger_updates[p.blogger_id]["verified"] -= 1
                        if p.is_correct:
                            blogger_updates[p.blogger_id]["correct"] -= 1
                    
                    p.is_deleted = True
                    p.deleted_at = datetime.now()
                    p.delete_reason = f"合并到预测ID {main_prediction.id}（置信度最高）"
                    p.restore_before = (datetime.now() + timedelta(days=7)).date()
                    deleted_count += 1
            
            merged_count += 1
            logger.info(f"已合并: {key}, 保留ID: {main_prediction.id}, 删除: {len(group)-1}条")
    
    for blogger_id, updates in blogger_updates.items():
        if blogger_id:
            update_blogger_stats_incremental(
                db, blogger_id,
                score_delta=updates["score"],
                correct_delta=updates["correct"],
                verified_delta=updates["verified"]
            )
            blogger = db.query(Blogger).filter(Blogger.id == blogger_id).first()
            if blogger:
                logger.info(f"更新博主 {blogger.name}: 分数变化{updates['score']}, 正确数变化{updates['correct']}, 已验证数变化{updates['verified']}")
    
    db.commit()
    
    return {
        "success": True,
        "message": f"合并完成：合并了 {merged_count} 组预测，删除了 {deleted_count} 条重复预测",
        "data": {
            "merged_groups": merged_count,
            "deleted_predictions": deleted_count,
            "bloggers_updated": len(blogger_updates)
        }
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