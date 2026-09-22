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
_evidence_cache: dict = {'at': 0.0, 'report': None, 'url': None, 'bind': None}


@router.get("/evidence")
def get_evidence_report(db: Session = Depends(get_db)):
    """结论证据体检报告：页面上每个准确率都得配着这几个数才有意义。

    为什么单独一个接口、而不是塞进 `/api/stats/overall`：算一次要把全部已判结论读出来
    再查净值（镜像 1110 条 + 两条查询），首页每次刷新都付这个钱不值。这里带 60 秒缓存
    ——**代价是跑批之后最长 60 秒页面还是旧区间**，我不假装它实时。
    缓存键必须带上"这条会话连的是哪个库"：第 24 轮实测，同一进程内换绑到另一个库
    （测试、`--db`、脚本复用 app）时，第二个人拿到的还是上一个人的数，而 `database`
    字段照样印"本地镜像库" —— 那正是第 23 轮要防的事的进程内版本。
    返回里必须带 `database` 与 `as_of` —— 第 23 轮我拿**镜像库**的数当"系统的数"
    报了十几轮，生产其实是 55.91%、失效 39.6%。数字离开库名和截止日就没有意义。
    """
    import time

    from src.services.verdict_evidence import span_report

    # 分键要**同时**认 URL 和"是不是同一个 bind 对象"：
    # `sqlite:///:memory:` 这类 URL 字符串完全相同（第 25 轮 A 的 MINOR-9），
    # 而只存 `id(bind)` 又会被回收后复用 —— 第 26 轮 A 实测 300 次建/销 Engine 里
    # 172 次撞同一个 id ⇒ 串数会以更难查的方式回来。存对象本身（缓存只有一格，
    # 不会无界增长；代价是多留一个旧 Engine 活着，比读错库便宜）。
    bind = db.get_bind()
    now = time.monotonic()
    cached = _evidence_cache
    if (cached.get('bind') is bind and str(cached.get('url')) == str(bind.url)
            and cached.get('report') is not None
            and now - cached['at'] <= _EVIDENCE_TTL):
        return {"success": True, "data": cached['report']}
    report = span_report(db)            # 先算成功再改缓存：异常时不留"键=B、报告=A"
    cached['bind'] = bind
    cached['url'] = str(bind.url)
    cached['report'] = report
    cached['at'] = now
    return {"success": True, "data": report}
