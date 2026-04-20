"""
基金数据模式
"""
from typing import Optional
from datetime import date
from pydantic import BaseModel, Field


class FundBase(BaseModel):
    """基金基础模式"""
    fund_code: str = Field(..., description="基金代码", min_length=6, max_length=10)
    fund_name: Optional[str] = Field(None, description="基金名称", max_length=100)


class FundAdd(FundBase):
    """添加基金请求"""
    fund_type: Optional[str] = Field(None, description="基金类型")
    sector_type: Optional[str] = Field(None, description="板块类型")


class FundResponse(BaseModel):
    """基金响应"""
    id: int
    fund_code: str
    fund_name: Optional[str] = None
    fund_type: Optional[str] = None
    sector_type: Optional[str] = None
    latest_nav: Optional[float] = None
    nav_date: Optional[date] = None
    day_growth: Optional[float] = None
    week_growth: Optional[float] = None
    month_growth: Optional[float] = None
    active_predictions: int = 0
    last_analyze_date: Optional[date] = None
    
    class Config:
        from_attributes = True


class FundHistoryItem(BaseModel):
    """基金历史净值项"""
    nav_date: date
    nav: float
    day_growth: Optional[float] = None


class FundWithPredictions(FundResponse):
    """基金带预测信息"""
    predictions: list = []
    history: list = []
