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
def get_stats(db: Session = Depends(get_db)):
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
def get_overall_stats(db: Session = Depends(get_db)):
    """获取整体统计数据"""
    service = StatsService(db)
    return {
        "success": True,
        "data": service.get_overall_stats()
    }


_EVIDENCE_TTL = 60.0
_evidence_cache: dict = {'at': 0.0, 'report': None}


@router.get("/evidence")
def get_evidence_report(db: Session = Depends(get_db)):
    """结论证据体检报告：页面上每个准确率都得配着这几个数才有意义。

    为什么单独一个接口、而不是塞进 `/api/stats/overall`：算一次要把全部已判结论读出来
    再查净值（镜像 1110 条 + 两条查询），首页每次刷新都付这个钱不值。这里带 60 秒缓存。
    返回里必须带 `database` 与 `as_of` —— 第 23 轮我拿**镜像库**的数当"系统的数"
    报了十几轮，生产其实是 55.91%、失效 39.6%。数字离开库名和截止日就没有意义。
    """
    import time

    from src.services.verdict_evidence import span_report

    now = time.time()
    if _evidence_cache['report'] is None or now - _evidence_cache['at'] > _EVIDENCE_TTL:
        _evidence_cache['report'] = span_report(db)
        _evidence_cache['at'] = now
    return {"success": True, "data": _evidence_cache['report']}
