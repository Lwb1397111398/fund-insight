"""
数据验证模式模块
使用 Pydantic 定义请求和响应数据模型
"""
from .blogger import (
    BloggerBase, BloggerCreate, BloggerUpdate, BloggerResponse
)
from .post import (
    PostBase, PostCreate, PostUpdate, PostResponse
)
from .prediction import (
    PredictionBase, PredictionCreate, PredictionVerify, PredictionResponse
)
from .fund import (
    FundBase, FundAdd, FundResponse
)
from .viewpoint import (
    ViewpointBase, ViewpointCreate, ViewpointResponse
)
from .common import (
    SuccessResponse, ErrorResponse, PaginatedResponse
)

__all__ = [
    'BloggerBase', 'BloggerCreate', 'BloggerUpdate', 'BloggerResponse',
    'PostBase', 'PostCreate', 'PostUpdate', 'PostResponse',
    'PredictionBase', 'PredictionCreate', 'PredictionVerify', 'PredictionResponse',
    'FundBase', 'FundAdd', 'FundResponse',
    'ViewpointBase', 'ViewpointCreate', 'ViewpointResponse',
    'SuccessResponse', 'ErrorResponse', 'PaginatedResponse',
]
