"""
观点数据模式
"""
from typing import Optional, List
from datetime import date
from pydantic import BaseModel, Field


class ViewpointBase(BaseModel):
    """观点基础模式"""
    blogger_id: Optional[int] = Field(None, description="博主ID")
    fund_code: Optional[str] = Field(None, description="基金代码")
    fund_name: Optional[str] = Field(None, description="基金名称")
    content: str = Field(..., description="观点内容", min_length=1)
    author: str = Field(default="网友", description="作者")
    source: str = Field(default="manual", description="来源")


class ViewpointCreate(ViewpointBase):
    """创建观点请求"""
    market_direction: str = Field(default="neutral", description="市场方向")
    confidence: int = Field(default=50, ge=0, le=100, description="置信度")
    sectors_bullish: Optional[List[str]] = Field(default=[], description="看多板块")
    sectors_bearish: Optional[List[str]] = Field(default=[], description="看空板块")
    reasoning: Optional[str] = Field(None, description="分析理由")
    time_horizon: str = Field(default="medium", description="时间周期")
    validity_period: str = Field(default="1个月", description="有效期")


class ViewpointResponse(BaseModel):
    """观点响应"""
    id: int
    blogger_id: Optional[int] = None
    fund_code: Optional[str] = None
    fund_name: Optional[str] = None
    content: str
    author: str
    source: str
    market_direction: Optional[str] = None
    confidence: Optional[int] = None
    sectors_bullish: Optional[List[str]] = None
    sectors_bearish: Optional[List[str]] = None
    reasoning: Optional[str] = None
    time_horizon: Optional[str] = None
    valid_until: Optional[date] = None
    viewpoint_date: Optional[date] = None
    
    class Config:
        from_attributes = True


class ViewpointStats(BaseModel):
    """观点统计"""
    total: int
    bullish: int
    bearish: int
    neutral: int
    crawler: int
    manual: int
