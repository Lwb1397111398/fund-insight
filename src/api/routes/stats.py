"""
统计路由
处理数据统计相关的 API 请求
"""
import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.services.stats_service import StatsService

router = APIRouter(prefix="/stats", tags=["统计"])


@router.get("")
async def get_stats(db: Session = Depends(get_db)):
    """获取统计数据"""
    try:
        service = StatsService(db)
        return service.get_all_stats()
    except Exception as e:
        if os.getenv("APP_ENV", "development").lower() == "production":
            return {"success": False, "error": "统计数据获取失败"}
        import traceback
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


@router.get("/overall")
async def get_overall_stats(db: Session = Depends(get_db)):
    """获取整体统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_overall_stats()
    }


@router.get("/bloggers")
async def get_blogger_stats(db: Session = Depends(get_db)):
    """获取博主统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_blogger_stats()
    }


@router.get("/predictions")
async def get_prediction_stats(db: Session = Depends(get_db)):
    """获取预测统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_prediction_stats()
    }


@router.get("/content")
async def get_content_stats(db: Session = Depends(get_db)):
    """获取内容统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_content_stats()
    }


@router.get("/funds")
async def get_fund_stats(db: Session = Depends(get_db)):
    """获取基金统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_fund_stats()
    }
