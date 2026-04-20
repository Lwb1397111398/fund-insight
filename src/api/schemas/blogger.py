"""
博主数据模式
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class BloggerBase(BaseModel):
    """博主基础模式"""
    name: str = Field(..., description="博主名称", min_length=1, max_length=100)
    platform: str = Field(default="xiaohongshu", description="平台")
    description: Optional[str] = Field(None, description="描述", max_length=500)


class BloggerCreate(BloggerBase):
    """创建博主请求"""
    pass


class BloggerUpdate(BaseModel):
    """更新博主请求"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    platform: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    is_active: Optional[bool] = None


class BloggerResponse(BaseModel):
    """博主响应"""
    id: int
    name: str
    platform: str
    description: Optional[str] = None
    accuracy_rate: Optional[float] = None
    total_predictions: Optional[int] = None
    correct_predictions: Optional[int] = None
    grade: Optional[str] = None
    ultra_short_accuracy: Optional[float] = None
    is_active: Optional[bool] = True
    created_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class BloggerWithStats(BloggerResponse):
    """博主带统计信息"""
    prediction_count: int = 0
    correct_count: int = 0
