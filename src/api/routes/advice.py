"""
投资建议路由
处理投资建议相关的 API 请求
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
import traceback

from src.api.deps import get_db
from src.services.advice_service import AdviceService
from src.analyzer.llm_analyzer import get_analyzer

router = APIRouter(prefix="/advice", tags=["投资建议"])


class GenerateAdviceRequest(BaseModel):
    date: Optional[date] = None
    force: bool = False


@router.post("")
async def generate_advice(request: GenerateAdviceRequest = None, db: Session = Depends(get_db)):
    """生成投资建议（智能检测数据变化）"""
    service = AdviceService(db)
    llm_analyzer = get_analyzer()
    
    try:
        force_generate = request.force if request else False
        
        if not force_generate:
            has_changed, current_hash, latest_advice = service.check_data_changed()
            if not has_changed:
                print(f"[Advice API] 数据未变化，返回最新建议")
                return {
                    "success": True,
                    "message": "数据未变化，返回最新投资建议",
                    "data": {**latest_advice, "is_new": False}
                }
        else:
            current_hash = service._calculate_data_hash()
        
        data = service.get_data_for_advice()
        
        print(f"[Advice API] 博主: {len(data['bloggers'])}, 预测: {len(data['predictions'])}, 观点: {len(data['viewpoints'])}")
        
        advice = llm_analyzer.generate_investment_advice_three_stage(
            data["bloggers"], 
            data["predictions"],
            viewpoints=data["viewpoints"]
        )
        
        # 准备引用的预测数据摘要
        referenced_predictions = []
        for p in data["predictions"][:20]:
            referenced_predictions.append({
                "blogger_name": p.get("blogger_name", ""),
                "sector": p.get("sector", ""),
                "prediction_type": p.get("prediction_type", ""),
                "prediction_content": p.get("prediction_content", ""),
                "confidence": p.get("confidence", 50),
                "days_to_target": p.get("days_to_target", 0)
            })
        
        result = service.create_advice(
            advice_type=advice.get("advice_type"),
            advice_content=advice.get("advice_content"),
            market_sentiment=advice.get("market_sentiment"),
            confidence=advice.get("confidence"),
            referenced_bloggers=[b["name"] for b in data["bloggers"]],
            data_hash=current_hash,
            advice_date=request.date if request else None,
            reasoning=advice.get("reasoning"),
            risk_warning=advice.get("risk_warning"),
            suggested_sectors=advice.get("suggested_sectors"),
            avoid_sectors=advice.get("avoid_sectors"),
            referenced_predictions=referenced_predictions,
            short_term_advice=advice.get("short_term"),
            mid_term_advice=advice.get("mid_term"),
            avoid_reasoning=advice.get("avoid_reasoning")
        )
        
        result["viewpoint_summary"] = advice.get("viewpoint_summary")
        result["prediction_analysis"] = advice.get("prediction_analysis")
        
        return {
            "success": True,
            "message": "投资建议生成成功（三阶段分析）",
            "data": result
        }
        
    except Exception as e:
        print(f"[API] 生成投资建议失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"生成失败: {str(e)}"
        }


@router.get("")
async def get_latest_advice(db: Session = Depends(get_db)):
    """获取最新投资建议"""
    service = AdviceService(db)
    advice = service.get_latest_advice()
    
    return {
        "success": True,
        "data": advice
    }


@router.get("/history")
async def get_advice_history(
    skip: int = 0,
    limit: int = 30,
    db: Session = Depends(get_db)
):
    """获取投资建议历史"""
    service = AdviceService(db)
    history = service.get_advice_history(skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": history
    }


@router.get("/stats")
async def get_advice_stats(db: Session = Depends(get_db)):
    """获取投资建议统计"""
    service = AdviceService(db)
    stats = service.get_advice_stats()
    
    return {
        "success": True,
        "data": stats
    }
