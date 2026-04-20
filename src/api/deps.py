"""
依赖注入模块
提供 FastAPI 依赖注入函数
"""
from typing import Generator, Tuple
from contextlib import contextmanager
from sqlalchemy.orm import Session

from src.models.database import SessionLocal
from src.services import (
    BloggerService,
    PostService,
    PredictionService,
    FundService,
    ViewpointService,
)


def get_db() -> Generator[Session, None, None]:
    """
    获取数据库会话
    
    Yields:
        数据库会话
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def _get_service_with_db(service_class):
    """
    上下文管理器：创建服务实例并自动管理数据库会话
    
    Usage:
        with _get_service_with_db(BloggerService) as service:
            service.do_something()
    """
    db = SessionLocal()
    try:
        yield service_class(db)
    finally:
        db.close()


def get_blogger_service(db: Session = None) -> BloggerService:
    """
    获取博主服务实例
    
    Args:
        db: 数据库会话（必须提供，避免会话泄漏）
        
    Returns:
        博主服务实例
        
    Note:
        如果需要在非 FastAPI 上下文中使用，请使用 blogger_service_context() 上下文管理器
    """
    if db is None:
        raise ValueError("db 参数不能为 None，请使用 blogger_service_context() 上下文管理器或提供数据库会话")
    return BloggerService(db)


def get_post_service(db: Session = None) -> PostService:
    """
    获取帖子服务实例
    
    Args:
        db: 数据库会话（必须提供，避免会话泄漏）
        
    Returns:
        帖子服务实例
    """
    if db is None:
        raise ValueError("db 参数不能为 None，请使用 post_service_context() 上下文管理器或提供数据库会话")
    return PostService(db)


def get_prediction_service(db: Session = None) -> PredictionService:
    """
    获取预测服务实例
    
    Args:
        db: 数据库会话（必须提供，避免会话泄漏）
        
    Returns:
        预测服务实例
    """
    if db is None:
        raise ValueError("db 参数不能为 None，请使用 prediction_service_context() 上下文管理器或提供数据库会话")
    return PredictionService(db)


def get_fund_service(db: Session = None) -> FundService:
    """
    获取基金服务实例
    
    Args:
        db: 数据库会话（必须提供，避免会话泄漏）
        
    Returns:
        基金服务实例
    """
    if db is None:
        raise ValueError("db 参数不能为 None，请使用 fund_service_context() 上下文管理器或提供数据库会话")
    return FundService(db)


def get_viewpoint_service(db: Session = None) -> ViewpointService:
    """
    获取观点服务实例
    
    Args:
        db: 数据库会话（必须提供，避免会话泄漏）
        
    Returns:
        观点服务实例
    """
    if db is None:
        raise ValueError("db 参数不能为 None，请使用 viewpoint_service_context() 上下文管理器或提供数据库会话")
    return ViewpointService(db)


@contextmanager
def blogger_service_context() -> Generator[BloggerService, None, None]:
    """
    博主服务上下文管理器，自动管理数据库会话
    
    Usage:
        with blogger_service_context() as service:
            bloggers = service.get_all_bloggers()
    """
    with _get_service_with_db(BloggerService) as service:
        yield service


@contextmanager
def post_service_context() -> Generator[PostService, None, None]:
    """帖子服务上下文管理器，自动管理数据库会话"""
    with _get_service_with_db(PostService) as service:
        yield service


@contextmanager
def prediction_service_context() -> Generator[PredictionService, None, None]:
    """预测服务上下文管理器，自动管理数据库会话"""
    with _get_service_with_db(PredictionService) as service:
        yield service


@contextmanager
def fund_service_context() -> Generator[FundService, None, None]:
    """基金服务上下文管理器，自动管理数据库会话"""
    with _get_service_with_db(FundService) as service:
        yield service


@contextmanager
def viewpoint_service_context() -> Generator[ViewpointService, None, None]:
    """观点服务上下文管理器，自动管理数据库会话"""
    with _get_service_with_db(ViewpointService) as service:
        yield service
