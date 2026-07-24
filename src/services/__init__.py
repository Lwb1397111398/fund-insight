"""
服务层模块
封装业务逻辑，提供统一的服务接口
"""
from .base import BaseService
from .blogger_service import BloggerService
from .post_service import PostService
from .prediction_service import PredictionService
from .fund_service import FundService
from .viewpoint_service import ViewpointService
from .advice_service import AdviceService
from .crawler_service import CrawlerService

__all__ = [
    'BaseService',
    'BloggerService',
    'PostService',
    'PredictionService',
    'FundService',
    'ViewpointService',
    'AdviceService',
    'CrawlerService',
]
