"""
API路由装饰器和工具函数
提供统一的错误处理、日志记录等功能
"""
from functools import wraps
from typing import Callable, Any
import traceback
from fastapi import HTTPException
from src.utils.common import success_response, error_response


def api_exception_handler(func: Callable) -> Callable:
    """
    API异常处理装饰器
    统一捕获异常并返回标准格式响应
    """
    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            return error_response(f"操作失败: {str(e)}")
    
    return wrapper


def handle_service_call(func: Callable) -> Callable:
    """
    服务调用装饰器
    用于服务层方法的异常处理
    """
    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if hasattr(args[0], 'db'):
                args[0].db.rollback()
            raise e
    
    return wrapper
