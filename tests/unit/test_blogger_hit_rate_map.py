# -*- coding: utf-8 -*-
"""博主榜那一列"准确率"（`_hit_rate_map`）的用例。

第 28 轮 F-M-1：全仓对 `/api/bloggers` 只有一条集成用例断 200 + `success`，
`_hit_rate_map` 一根手指都没人碰。变异实验：把 `Prediction.is_deleted == False` 改成
`== True`（＝"存活命中率"反过来只算回收站）⇒ `pytest tests/ -q` **854 条全绿**。
这一列是老板天天看、并用来判断"哪个博主可信"的数，却是两个口径里唯一没看守卫的那个
（另一个 `src/utils/blogger_stats.py` 删掉 `verify_count > 0` 会红 3 条）。

顺带把"两个口径为什么会不一样"钉成可执行的：同一批数据，命中率与加权评分**本就该不同**
——差别在 flat 与 `verify_count=0` 那两类行、以及归档累计，不在"谁算错了"。
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.routes.bloggers import _hit_rate_map
from src.models.database import Base, Blogger, Prediction
from src.utils.blogger_stats import recalculate_blogger_stats


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _p(blogger_id, correct, *, deleted=False, kind='up', verify_count=1, score=None):
    return Prediction(
        blogger_id=blogger_id,
        post_id=1,
        sector='半导体',
        fund_code='512480',
        prediction_type=kind,
        prediction_date=date.today() - timedelta(days=1),
        target_date=date.today() + timedelta(days=7),
        is_deleted=deleted,
        is_correct=correct,
        verify_count=verify_count,
        verify_score=score if score is not None else (100 if correct else 0),
    )


def test_hit_rate_counts_only_surviving_judged_conclusions(db):
    """回收站里的结论不能进"存活命中率"——这正是 F 那条变异改的东西。"""
    b = Blogger(id=1, name='甲')
    db.add(b)
    db.add_all([
        _p(1, True), _p(1, True), _p(1, False),                 # 存活：3 判 / 2 对
        _p(1, True, deleted=True), _p(1, True, deleted=True),   # 回收站：全是"判对"
    ])
    db.commit()
    hit = _hit_rate_map(db, [1])[1]
    assert (hit['hit_verified'], hit['hit_correct']) == (3, 2), hit
    assert hit['hit_rate'] == pytest.approx(66.67, abs=0.01), hit
    # 若判据反向（只算回收站）会得到 5/4 = 80% ⇒ 这条用例就是为抓它而写
    assert hit['hit_rate'] != 80.0


def test_hit_rate_is_none_instead_of_dividing_by_zero(db):
    db.add(Blogger(id=2, name='乙'))
    db.add(_p(2, None))                       # 未判：既不进分子也不进分母
    db.commit()
    hit = _hit_rate_map(db, [2])[2]
    assert hit['hit_verified'] == 0 and hit['hit_rate'] is None, hit


def test_two_calibers_differ_on_the_same_rows_by_design(db):
    """命中率看"判没判对"，加权评分看 `verify_count>0` 且排除 flat ⇒ 同数据两个数。

    这不是 bug，是两件事；页面上必须分开写明（AGENTS.md 那条"手机没有 hover，
    关键信息不能只放 `title`"）。这里把差异钉住，改任一判据都会撞见另一条口径。
    """
    db.add(Blogger(id=3, name='丙'))
    db.add_all([
        _p(3, True), _p(3, True),                     # 两条正常判对
        _p(3, False, kind='flat'),                    # 观望：命中率算它，加权不算
        _p(3, True, verify_count=0, score=0),         # 判对但没有验证次数：加权不收
    ])
    db.commit()
    hit = _hit_rate_map(db, [3])[3]
    stats = recalculate_blogger_stats(db, 3, commit=False)
    assert (hit['hit_verified'], hit['hit_correct']) == (4, 3), hit        # 3/4 = 75%
    assert (stats['total_predictions'], stats['correct_predictions']) == (2, 2), stats
    assert stats['accuracy_rate'] == pytest.approx(100.0), stats


def test_a_blogger_with_only_recycle_bin_predictions_is_not_a_foreign_key_crash(test_db):
    """第 50 轮 A 席 MINOR-5：`safe_delete` 以前只数 `is_deleted=false` 的预测，
    于是"名下只剩回收站"的博主会过掉三道门、在 `db.delete` 时撞 FK ——
    老板看到的是一句 `删除失败: FOREIGN KEY constraint failed`，而不是"先清回收站"。
    """
    from src.models.database import Post
    from src.services.blogger_service import BloggerService

    b = Blogger(name='只有回收站', platform='weibo')
    test_db.add(b)
    test_db.flush()
    post = Post(blogger_id=b.id, content='一篇', source_url='https://x.invalid/2',
                post_date=date.today())
    test_db.add(post)
    test_db.flush()
    test_db.add(Prediction(blogger_id=b.id, post_id=post.id, sector='半导体',
                           fund_code='512480', prediction_type='up',
                           prediction_date=date.today() - timedelta(days=30),
                           is_deleted=True))
    test_db.commit()

    ok, message = BloggerService(test_db).safe_delete(b.id)
    assert ok is False, '回收站里还有预测（外键指着它）却允许硬删博主 ⇒ 必撞 FK'
    assert '回收站' in message, message
    assert test_db.query(Blogger).filter_by(id=b.id).first() is not None
