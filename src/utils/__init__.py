"""
公共工具函数模块
提供日期格式化、响应格式化等通用功能
"""
from datetime import date, datetime
from typing import Any, Optional, Dict


def format_date(d: Optional[date]) -> Optional[str]:
    """格式化日期对象为ISO格式字符串"""
    return d.isoformat() if d else None


def format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """格式化日期时间对象为ISO格式字符串"""
    return dt.isoformat() if dt else None


def success_response(data: Any = None, message: str = "操作成功") -> Dict:
    """
    构建成功响应
    
    Args:
        data: 响应数据
        message: 成功消息
    
    Returns:
        标准成功响应字典
    """
    response = {"success": True, "message": message}
    if data is not None:
        response["data"] = data
    return response


def error_response(message: str, data: Any = None) -> Dict:
    """
    构建错误响应
    
    Args:
        message: 错误消息
        data: 附加数据
    
    Returns:
        标准错误响应字典
    """
    response = {"success": False, "message": message}
    if data is not None:
        response["data"] = data
    return response
