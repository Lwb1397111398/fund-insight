"""
测试数据清理路由
处理测试数据清理相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.services.test_data_cleanup_service import TestDataCleanupService

router = APIRouter(prefix="/test-data", tags=["测试数据清理"])


@router.get("/find")
async def find_test_data(db: Session = Depends(get_db)):
    """查找所有测试数据"""
    service = TestDataCleanupService(db)
    test_data = service.get_all_test_data()
    
    return {
        "success": True,
        "data": test_data
    }


@router.post("/cleanup")
async def cleanup_test_data(request: Request, db: Session = Depends(get_db)):
    """清理所有测试数据（硬删除）"""
    if request.headers.get("X-Danger-Confirm") != "cleanup-test-data":
        raise HTTPException(status_code=403, detail="缺少测试数据清理确认头")

    service = TestDataCleanupService(db)
    result = service.cleanup_test_data()

    return result
