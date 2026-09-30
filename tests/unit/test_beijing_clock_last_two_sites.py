# -*- coding: utf-8 -*-
"""剩下两处"墙钟 vs 北京钟"的收口（任务 #145 / #146 的最后一档）。

这两处以前各自都有一种"同一天被算成两天"的形状：

① `GET /api/bloggers` 那个"近 7 天"（`active_posts_count`）拿 `date.today()` 减 7，
   而被筛的 `Prediction.target_date` 是按**北京日**排出来的 ⇒ 容器在 UTC 时，
   北京时间 00:00~08:00 之间那一周的日子少算一天（页面上博主的"近 7 天发帖数"会莫名变少）。
② 「每日汇总」的幂等闸比的是 `BatchAnalysisTask.created_at.date()`，而 `created_at`
   是朴素 `datetime.now()`（容器里就是 UTC）⇒ 同一个北京日会被判成"昨天那条，不算今天"，
   于是**凌晨点一次、早上再点一次**，跑出两份汇总。

两条都是"两把钟"这一族（第 27 / 52 / 61 轮反复扣分那一族）的最后两处活路。
判据把**来路**钉住：不是"看起来日期对得上"，而是"换了北京钟这个数字/这条分支必须跟着动"。
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.routes import bloggers as bloggers_route
from src.api.routes.bloggers import get_bloggers
from src.models.database import Base, BatchAnalysisTask, Blogger, Prediction
from src.services import viewpoint_workflow_service as vws

# 故意挑一个**不等于本机今天**的日期：夹具里所有判断都锚在它身上，
# 这样"代码到底问的是哪把钟"当场就能量出来（拿 date.today() 的旧写法会偏一天）。
BEIJING_TODAY = date(2026, 3, 14)


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed_blogger_with_two_posts(db, near: date, far: date) -> Blogger:
    b = Blogger(name='某博主', platform='weibo')
    db.add(b)
    db.flush()
    for day, post in ((near, 11), (far, 12)):
        db.add(Prediction(blogger_id=b.id, post_id=post, sector='半导体', fund_code='512480',
                          prediction_type='up', prediction_date=day - timedelta(days=30),
                          target_date=day, is_deleted=False))
    db.commit()
    return b


def test_the_seven_day_blogger_window_is_counted_on_the_beijing_clock(db, monkeypatch):
    """边界那一行必须按北京日算：`北京今天 - 7` 在内、`- 8` 在外。

    旧写法在这里会少算一天 —— 被裁掉的正是 `near` 那一行，于是 `active_posts_count` 变 1。
    """
    monkeypatch.setattr(bloggers_route, 'current_as_of', lambda: BEIJING_TODAY)
    b = _seed_blogger_with_two_posts(db, BEIJING_TODAY - timedelta(days=7),
                                     BEIJING_TODAY - timedelta(days=8))
    res = get_bloggers(skip=0, limit=100, platform=None, active_only=False, db=db)
    assert res['success'] is True, res
    row = [x for x in res['data'] if x['id'] == b.id][0]
    assert row['active_posts_count'] == 1, row


def test_the_summary_gate_reads_its_own_beijing_run_date_not_the_wall_clock(db, monkeypatch):
    """凌晨（容器 UTC 还是"昨天"）点过一次，早上再点必须说"今天已经汇总过"。

    夹具摆的是这个形状：任务行的 `created_at` 落在**墙钟的前一天**，
    而它自己写的 `task_params['run_date']` 是**北京今天** ⇒ 幂等闸必须认后者。
    旧写法比 `created_at.date()` ⇒ 认不出今天这条，会再跑一次汇总。
    """
    monkeypatch.setattr(vws, 'beijing_today', lambda: BEIJING_TODAY)
    ran = []
    monkeypatch.setattr(vws.ViewpointWorkflowService, 'summarize_pending_dates',
                        classmethod(lambda cls, session: ran.append(1) or {'completed': [], 'skipped': []}))
    db.add(BatchAnalysisTask(
        task_type='viewpoint_summary', status='succeeded',
        total_count=1, processed_count=1, success_count=1, failed_count=0,
        processed_ids=[], failed_ids=[],
        created_at=datetime.combine(BEIJING_TODAY - timedelta(days=1), datetime.min.time()),
        task_params={'run_date': BEIJING_TODAY.isoformat()},
        result_summary={'completed': [{'date': BEIJING_TODAY.isoformat()}], 'skipped': []},
    ))
    db.commit()

    res = vws.ViewpointWorkflowService.run_daily_summary_task(session_factory=lambda: db)

    assert res.get('already_completed') is True, res
    assert ran == [], '幂等闸认错了日期 ⇒ 同一天又跑了一次汇总'


def test_a_summary_stamped_yesterday_is_free_to_run_again(db, monkeypatch):
    """反面对照：`run_date` 是昨天的那条**不许**被当成"今天已经做过"。

    少了这一格，上一条会从"跟着北京钟走"退化成"永远说已经做过"（过宽的闸活不过一轮）。
    """
    monkeypatch.setattr(vws, 'beijing_today', lambda: BEIJING_TODAY)
    ran = []
    monkeypatch.setattr(vws.ViewpointWorkflowService, 'summarize_pending_dates',
                        classmethod(lambda cls, session: ran.append(1) or {'completed': [], 'skipped': []}))
    db.add(BatchAnalysisTask(
        task_type='viewpoint_summary', status='succeeded',
        total_count=1, processed_count=1, success_count=1, failed_count=0,
        processed_ids=[], failed_ids=[],
        created_at=datetime.combine(BEIJING_TODAY - timedelta(days=1), datetime.min.time()),
        task_params={'run_date': (BEIJING_TODAY - timedelta(days=1)).isoformat()},
        result_summary={'completed': [{'date': 'x'}], 'skipped': []},
    ))
    db.commit()

    res = vws.ViewpointWorkflowService.run_daily_summary_task(session_factory=lambda: db)

    assert res.get('already_completed') is None, res
    assert ran == [1], '昨天那条不该拦住今天'
    fresh = db.query(BatchAnalysisTask).order_by(BatchAnalysisTask.id.desc()).first()
    assert (fresh.task_params or {}).get('run_date') == BEIJING_TODAY.isoformat(), fresh.task_params
