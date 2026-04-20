"""
路由模块
按业务领域拆分的 API 路由
"""
from .bloggers import router as bloggers_router
from .posts import router as posts_router
from .predictions import router as predictions_router
from .funds import router as funds_router
from .viewpoints import router as viewpoints_router
from .crawler import router as crawler_router
from .advice import router as advice_router
from .stats import router as stats_router
from .config import router as config_router

__all__ = [
    'bloggers_router',
    'posts_router',
    'predictions_router',
    'funds_router',
    'viewpoints_router',
    'crawler_router',
    'advice_router',
    'stats_router',
    'config_router',
]
