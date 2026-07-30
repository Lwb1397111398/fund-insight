"""僵尸任务自愈 + 基金列表瘦身。

背景（2026-07-30 线上问题）：
1. 用户点了"验证到期预测"后直接关网页，DB 里留下 status='running' 的任务。
   原实现只在 start() 里判超时，status() 不判 → 前端轮询永远拿到
   in_progress=True，按钮永久显示"验证中"并禁用。
2. 基金列表接口为每只基金查最近 5 条净值（窗口函数），前端却从未使用
   recent_history，白等 ~3s。
"""
from datetime import date, datetime, timedelta

from src.models.database import BatchAnalysisTask, FundHistory, FundInfo
from src.services.fund_service import FundService


def test_status_marks_stale_running_task_failed(test_db):
    """status() 必须自愈超时任务，否则前端永远停在"验证中"。"""
    from src.services.prediction_verify_task import PredictionVerifyTask

    stale = BatchAnalysisTask(
        task_type="predictions",
        status="running",
        total_count=188,
        processed_count=0,
        started_at=datetime.now() - timedelta(minutes=31),
    )
    test_db.add(stale)
    test_db.commit()

    status = PredictionVerifyTask(stale_after=timedelta(minutes=30)).status(db=test_db)
    test_db.refresh(stale)

    assert status["in_progress"] is False
    assert stale.status == "failed"
    assert "超时" in (stale.error_message or "")


def test_status_keeps_fresh_running_task_in_progress(test_db):
    """未超时的任务不能被误杀。"""
    from src.services.prediction_verify_task import PredictionVerifyTask

    fresh = BatchAnalysisTask(
        task_type="predictions",
        status="running",
        total_count=10,
        processed_count=3,
        started_at=datetime.now() - timedelta(minutes=1),
    )
    test_db.add(fresh)
    test_db.commit()

    status = PredictionVerifyTask(stale_after=timedelta(minutes=30)).status(db=test_db)
    test_db.refresh(fresh)

    assert status["in_progress"] is True
    assert fresh.status == "running"


def _seed_fund_with_history(db):
    db.add(FundInfo(fund_code="000001", fund_name="测试基金", sector_type="测试", latest_nav=1.0, day_growth=0.1))
    for i in range(6):
        db.add(FundHistory(
            fund_code="000001",
            fund_name="测试基金",
            nav_date=date(2026, 6, 1) - timedelta(days=i),
            nav=1.0 + i,
            day_growth=0.1,
        ))
    db.commit()


def test_fund_list_can_skip_unused_history_payload(test_db):
    """include_history=False 时不返回 recent_history（前端不用，省一次窗口函数查询）。"""
    _seed_fund_with_history(test_db)

    rows = FundService(test_db).get_funds_with_grouping(
        group_by_sector=False, include_history=False
    )

    assert len(rows) == 1
    assert "recent_history" not in rows[0]
    assert rows[0]["fund_code"] == "000001"


def test_fund_list_still_returns_history_by_default(test_db):
    """默认行为不变，避免影响其他调用方。"""
    _seed_fund_with_history(test_db)

    rows = FundService(test_db).get_funds_with_grouping(group_by_sector=False)

    assert len(rows[0]["recent_history"]) == 5


def test_fund_route_requests_the_slim_payload():
    """平铺列表走瘦身分支；分组模式保留 history，避免改动其他调用方。"""
    import inspect

    from src.api.routes import funds

    source = inspect.getsource(funds.get_funds)
    assert "include_history=group_by_sector" in source
