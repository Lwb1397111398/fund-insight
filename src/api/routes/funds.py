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
def get_funds(
    skip: int = 0,
    limit: int = 100,
    sector_type: Optional[str] = None,
    group_by_sector: bool = True,
    db: Session = Depends(get_db)
):
    """获取基金列表（支持按板块分组）"""
    if skip < 0 or limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="分页参数无效")
    service = FundService(db)
    funds = service.get_funds_with_grouping(
        skip=skip,
        limit=limit,
        sector_type=sector_type,
        group_by_sector=group_by_sector,
        # 平铺列表页只显示最新净值，不画走势：跳过每只基金最近 5 条净值的窗口查询。
        # 分组模式保留原返回结构，避免改动其他调用方。
        include_history=group_by_sector
    )
    
    total_query = db.query(FundInfo)
    if sector_type:
        total_query = total_query.filter(FundInfo.sector_type == sector_type)
    total = total_query.count()
    return {
        "success": True,
        "data": funds,
        "meta": {
            "total": total,
            "page": skip // limit + 1,
            "page_size": limit,
            "pages": (total + limit - 1) // limit,
        },
    }


@router.post("")
def add_fund(fund: FundAdd, db: Session = Depends(get_db)):
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


@router.get("/{fund_code}")
def get_fund_detail(fund_code: str, db: Session = Depends(get_db)):
    """获取单只基金及近期净值。"""
    detail = FundService(db).get_fund_detail(fund_code)
    if detail is None:
        raise HTTPException(status_code=404, detail="基金不存在")
    return {"success": True, "data": detail}


@router.delete("/{fund_code}")
def delete_fund(fund_code: str, db: Session = Depends(get_db)):
    """删除没有有效预测关联的基金。"""
    result = FundService(db).delete_fund(fund_code)
    if not result["success"] and result["message"] == "基金不存在":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


