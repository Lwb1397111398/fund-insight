"""
测试数据清理路由
处理测试数据清理相关的 API 请求
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.services.test_data_cleanup_service import TestDataCleanupService

router = APIRouter(prefix="/test-data", tags=["测试数据清理"])


def _test_data_cleanup_enabled() -> bool:
    """硬删除仅在维护环境显式开启，避免关键词误判删除业务数据。"""
    return os.getenv("ENABLE_TEST_DATA_CLEANUP", "false").lower() == "true"


@router.get("/find")
async def find_test_data(db: Session = Depends(get_db)):
    """查找所有测试数据"""
    service = TestDataCleanupService(db)
    test_data = service.get_all_test_data()
    test_data["cleanup_enabled"] = _test_data_cleanup_enabled()
    
    return {
        "success": True,
        "data": test_data
    }


@router.post("/cleanup")
async def cleanup_test_data(request: Request, db: Session = Depends(get_db)):
    """清理所有测试数据（硬删除）"""
    if not _test_data_cleanup_enabled():
        raise HTTPException(
            status_code=403,
            detail="测试数据清理接口已禁用。请在隔离维护环境显式设置 ENABLE_TEST_DATA_CLEANUP=true 后再使用",
        )
    if request.headers.get("X-Danger-Confirm") != "cleanup-test-data":
        raise HTTPException(status_code=403, detail="缺少测试数据清理确认头")

    service = TestDataCleanupService(db)
    result = service.cleanup_test_data()

    return result
