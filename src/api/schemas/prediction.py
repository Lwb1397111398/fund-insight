"""
预测数据模式
"""
from typing import Optional
from datetime import date, datetime
from pydantic import BaseModel, Field


class PredictionBase(BaseModel):
    """预测基础模式"""
    post_id: int = Field(..., description="帖子ID")
    blogger_id: int = Field(..., description="博主ID")
    fund_code: str = Field(..., description="基金代码", max_length=20)
    fund_name: Optional[str] = Field(None, description="基金名称", max_length=100)
    sector: Optional[str] = Field(None, description="板块", max_length=100)
    prediction_type: str = Field(..., description="预测类型", pattern="^(bullish|bearish|neutral)$")
    prediction_content: str = Field(..., description="预测内容")
    confidence: int = Field(default=50, ge=0, le=100, description="置信度")
    prediction_date: date = Field(..., description="预测日期")
    target_date: Optional[date] = Field(None, description="目标日期")


class PredictionCreate(PredictionBase):
    """创建预测请求"""
    prediction_period: Optional[str] = Field(default="medium", description="预测周期")


class PredictionVerify(BaseModel):
    """验证预测请求"""
    actual_change: float = Field(..., description="实际涨跌幅")
    is_correct: bool = Field(..., description="是否正确")
    ai_judgment: Optional[str] = Field(None, description="AI判断说明")


class PredictionResponse(BaseModel):
    """预测响应"""
    id: int
    post_id: int
    blogger_id: int
    fund_code: str
    fund_name: Optional[str] = None
    sector: Optional[str] = None
    prediction_type: str
    prediction_content: str
    confidence: int
    prediction_date: date
    target_date: Optional[date] = None
    status: str = "pending"
    is_correct: Optional[bool] = None
    actual_change: Optional[float] = None
    verified_at: Optional[date] = None
    
    class Config:
        from_attributes = True


class PredictionStats(BaseModel):
    """预测统计"""
    total: int
    verified: int
    correct: int
    pending: int
    accuracy: float
