"""
统一响应格式工具
提供标准化的成功/错误响应格式
"""
from fastapi.responses import JSONResponse


def error_response(status_code: int, message: str, detail: str = None):
    """统一错误响应格式"""
    content = {"success": False, "message": message}
    if detail:
        content["detail"] = detail
    return JSONResponse(status_code=status_code, content=content)


def success_response(data=None, message: str = "操作成功"):
    """统一成功响应格式"""
    return {"success": True, "message": message, "data": data}
