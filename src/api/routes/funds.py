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


