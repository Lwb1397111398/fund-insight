# -*- coding: utf-8 -*-
"""第 13 轮 MAJOR-1 / MAJOR-2：验证历史的读取顺序，与预热缓存在 commit 后的行为。

两条都是"看起来在优化、实际把正确性/性能反着改"的典型：
- MAJOR-1：详情抽屉 19 处绑定都读 `verify_history[0]`，而后端按时间**正序**追加 ⇒
  有多轮验证的预测打开看到的是最早那一轮的结论。实测本地镜像 1168 条已判定预测里
  451 条有 ≥2 轮，100% 的 `[0]` 是最早那条。
- MAJOR-2：`_warm_cache` 把 `FundHistory` **ORM 实例**塞进缓存，而 `SessionLocal` 没设
  `expire_on_commit=False`，批量路径每条预测都会 commit（进度上报）⇒ 实例被 expire 后
  每次读属性各发一条 SELECT，预热从"省查询"翻转成 N+1 放大器（实测 0 → 720 条）。
"""
from datetime import date, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

from src.models.database import FundHistory
from src.services.prediction_query_service import PredictionQueryService
from src.services.prediction_verify_service import PredictionVerifyService


def _mk_prediction(db, code, history):
    from src.models.database import Blogger, Post, Prediction
    blogger = Blogger(name='历史顺序博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, title='t', content='c', post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                   fund_name='测试基金', sector='测试', prediction_type='up',
                   prediction_date=date(2026, 6, 1), prediction_period='1周',
                   target_date=date(2026, 6, 8), status='success', is_correct=True,
                   verify_count=2, verify_score=80, is_deleted=False,
                   verify_history=history)
    db.add(p)
    db.commit()
    return p


def test_api_returns_newest_verify_history_first(test_db):
    """序列化出口必须倒序：UI 的 `[0]` 语义就是"最近一次验证"。"""
    history = [{'date': '2026-07-01', 'score': 30, 'verify_type': 'process'},
               {'date': '2026-08-01', 'score': 90, 'verify_type': 'final'}]
    p = _mk_prediction(test_db, '510301', history)

    dto = PredictionQueryService(test_db).get_detail(p.id)
    assert dto['verify_history'][0]['date'] == '2026-08-01', dto['verify_history']
    assert dto['verify_history'][-1]['date'] == '2026-07-01', dto['verify_history']
    # 库里仍然是正序（追加语义没动），倒序只发生在出口
    test_db.refresh(p)
    assert [h['date'] for h in p.verify_history] == ['2026-07-01', '2026-08-01']


def test_warm_cache_survives_a_commit_without_extra_queries(test_db):
    """预热之后哪怕会话被 commit 过，读净值日历也不该逐条回查数据库。"""
    code = '510300'
    today = date(2026, 9, 21)
    for i in range(1, 121):
        test_db.add(FundHistory(fund_code=code, nav_date=today - timedelta(days=i), nav=1.0 + i))
    test_db.commit()

    svc = PredictionVerifyService(test_db)
    svc._warm_cache([type('P', (), {'fund_code': code})()], today)
    assert svc._nav_cache.get('_history', {}).get(code), '预热没装进任何东西'

    queries = {'n': 0}

    def _count(*args, **kwargs):
        queries['n'] += 1

    event.listen(test_db.bind, 'before_cursor_execute', _count)
    try:
        test_db.commit()          # 模拟 update_progress 每轮一次的提交
        before = queries['n']
        for _ in range(30):
            svc._real_nav_date(code, today - timedelta(days=30))
        after = queries['n']
    finally:
        event.remove(test_db.bind, 'before_cursor_execute', _count)

    # 缓存存的是轻量元组，不会被 expire ⇒ 30 次查询应当一条 SQL 都不发
    assert after - before == 0, (
        '预热缓存在 commit 后仍逐条回查（N+1 放大器）：30 次调用发了 %d 条 SQL'
        % (after - before))
