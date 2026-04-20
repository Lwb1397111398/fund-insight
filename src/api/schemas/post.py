"""
帖子数据模式
"""
from typing import Optional
from datetime import date, datetime
from pydantic import BaseModel, Field


class PostBase(BaseModel):
    """帖子基础模式"""
    blogger_id: int = Field(..., description="博主ID")
    title: Optional[str] = Field(None, description="标题", max_length=500)
    content: str = Field(..., description="内容", min_length=1)
    post_date: date = Field(..., description="发帖日期")
    source_url: Optional[str] = Field(None, description="来源链接", max_length=500)


class PostCreate(PostBase):
    """创建帖子请求"""
    auto_generate_title: bool = Field(default=False, description="是否自动生成标题")


class PostUpdate(BaseModel):
    """更新帖子请求"""
    title: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = None
    post_date: Optional[date] = None
    source_url: Optional[str] = Field(None, max_length=500)


class PostResponse(BaseModel):
    """帖子响应"""
    id: int
    blogger_id: int
    title: Optional[str] = None
    content: str
    post_date: Optional[date] = None
    source_url: Optional[str] = None
    analyzed: bool = False
    auto_titled: bool = False
    created_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class PostWithPredictions(PostResponse):
    """帖子带预测信息"""
    predictions: list = []


class PostAnalysisResult(BaseModel):
    """帖子分析结果"""
    has_prediction: bool
    predictions: list = []
    viewpoint: Optional[dict] = None
    summary: Optional[str] = None
