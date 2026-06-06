"""
基金路由
处理基金相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import date, timedelta
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=3)

from src.api.deps import get_db
from src.services.fund_service import FundService
from src.services.fund_update_task import fund_update_task
from src.models.database import FundInfo, FundHistory, SessionLocal

router = APIRouter(prefix="/funds", tags=["基金"])


class FundAdd(BaseModel):
    fund_code: str
    fund_name: Optional[str] = None
    fund_type: Optional[str] = None
    sector_type: Optional[str] = None


@router.get("")
async def get_funds(
    skip: int = 0,
    limit: int = 100,
    sector_type: Optional[str] = None,
    group_by_sector: bool = True,
    db: Session = Depends(get_db)
):
    """获取基金列表（支持按板块分组）"""
    service = FundService(db)
    funds = service.get_funds_with_grouping(
        skip=skip,
        limit=limit,
        sector_type=sector_type,
        group_by_sector=group_by_sector
    )
    
    return {
        "success": True,
        "data": funds
    }


@router.post("")
async def add_fund(fund: FundAdd, db: Session = Depends(get_db)):
    """添加基金"""
    service = FundService(db)
    result = service.add_fund_with_history(
        fund_code=fund.fund_code,
        fund_name=fund.fund_name,
        fund_type=fund.fund_type,
        sector_type=fund.sector_type
    )
    
    if not result["success"]:
        return result
    
    return result


@router.get("/update-status")
def get_fund_update_status():
    """获取基金更新任务状态"""
    return {
        "success": True,
        "data": fund_update_task.status()
    }


@router.get("/{fund_code}")
async def get_fund_detail(fund_code: str, db: Session = Depends(get_db)):
    """获取基金详情"""
    service = FundService(db)
    fund = service.get_fund_detail(fund_code)
    
    if not fund:
        raise HTTPException(status_code=404, detail="基金不存在")
    
    return {
        "success": True,
        "data": fund
    }


@router.delete("/{fund_code}")
async def delete_fund(fund_code: str, db: Session = Depends(get_db)):
    """删除基金"""
    service = FundService(db)
    result = service.delete_fund(fund_code)
    
    if not result["success"]:
        if "不存在" in result["message"]:
            raise HTTPException(status_code=404, detail=result["message"])
        return result
    
    return result


@router.post("/update-all")
def update_all_funds(db: Session = Depends(get_db)):
    """启动基金数据后台更新"""
    def runner():
        worker_db = SessionLocal()
        try:
            return FundService(worker_db).update_all_funds()
        finally:
            worker_db.close()

    return fund_update_task.start(runner)


@router.get("/by-sector/{sector_type}")
async def get_funds_by_sector(
    sector_type: str,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """根据板块获取基金列表"""
    service = FundService(db)
    funds = service.get_by_sector(sector_type, skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": f.id,
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "fund_type": f.fund_type,
                "latest_nav": f.latest_nav,
                "day_growth": f.day_growth,
                "active_predictions": f.active_predictions
            }
            for f in funds
        ]
    }


@router.get("/active/list")
async def get_active_funds(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """获取活跃基金（有活跃预测的基金）"""
    service = FundService(db)
    funds = service.get_active(skip=skip, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": f.id,
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "sector_type": f.sector_type,
                "latest_nav": f.latest_nav,
                "day_growth": f.day_growth,
                "active_predictions": f.active_predictions
            }
            for f in funds
        ]
    }


@router.get("/search/keyword")
async def search_funds(
    keyword: str,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """搜索基金"""
    service = FundService(db)
    funds = service.search(keyword, limit=limit)
    
    return {
        "success": True,
        "data": [
            {
                "id": f.id,
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "fund_type": f.fund_type,
                "sector_type": f.sector_type
            }
            for f in funds
        ]
    }


@router.get("/{fund_code}/history")
async def get_fund_history(
    fund_code: str,
    days: int = 30,
    db: Session = Depends(get_db)
):
    """获取基金历史净值"""
    service = FundService(db)
    history = service.get_history(fund_code, days=days)
    
    return {
        "success": True,
        "data": [
            {
                "date": h.nav_date.isoformat() if h.nav_date else None,
                "nav": h.nav,
                "day_growth": h.day_growth
            }
            for h in history
        ]
    }


@router.get("/{fund_code}/predictions")
async def get_fund_with_predictions(
    fund_code: str,
    db: Session = Depends(get_db)
):
    """获取基金及其预测"""
    service = FundService(db)
    fund = service.get_with_predictions(fund_code)

    if not fund:
        raise HTTPException(status_code=404, detail="基金不存在")

    return {
        "success": True,
        "data": fund
    }


@router.get("/trends/status")
async def get_trend_status_route(db: Session = Depends(get_db)):
    """获取基金趋势分析状态"""
    return await get_trend_status(db)


@router.post("/trends/analyze-all")
async def analyze_all_fund_trends_route(db: Session = Depends(get_db)):
    """分析所有基金趋势"""
    return await analyze_all_fund_trends(db)


async def get_trend_status(db: Session) -> dict:
    """获取基金趋势分析状态"""
    total = db.query(FundInfo).count()
    analyzed = db.query(FundInfo).filter(FundInfo.last_analyze_date.isnot(None)).count()
    today_analyzed = db.query(FundInfo).filter(
        FundInfo.last_analyze_date == date.today()
    ).count()
    return {
        "success": True,
        "data": {
            "total": total,
            "analyzed": analyzed,
            "pending": total - analyzed,
            "today_analyzed": today_analyzed
        }
    }


async def analyze_all_fund_trends(db: Session) -> dict:
    """分析所有基金的趋势"""
    funds = db.query(FundInfo).filter(FundInfo.last_analyze_date.is_(None)).all()
    analyzed_count = 0
    for fund in funds:
        fund.last_analyze_date = date.today()
        analyzed_count += 1
    db.commit()
    return {
        "success": True,
        "data": {
            "analyzed": analyzed_count,
            "total": len(funds)
        }
    }