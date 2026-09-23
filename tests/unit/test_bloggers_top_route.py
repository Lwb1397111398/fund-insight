# -*- coding: utf-8 -*-
"""`GET /api/bloggers/top` 的契约用例 —— TOP 弹窗每一格读的键都得有出处。

第 33 轮 B-MAJOR-8 指出：这条路由**整条零用例**（`grep -rn "bloggers/top" tests/` 空），
而弹窗新加的口径（命中率带 `判对/已验证`、加权评分、接口自己的 `metric_note`）全靠手抄字段名。
后端改名的当天页面会印出 `undefined/10`，而 898 条用例照样全绿 —— 所以钉的是**响应形状**，
不是"能不能返回 200"。
"""
from datetime import date, timedelta

import pytest

from src.api.routes.bloggers import get_top_bloggers
from src.models.database import Base, Blogger, Prediction
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 弹窗模板实际读的键（`web/index.html` 里 `v-for="(b, idx) in topBloggers"` 那一段）
KEYS_THE_PAGE_READES = ('id', 'name', 'hit_rate', 'hit_correct', 'hit_verified',
                        'accuracy_rate', 'weighted_score', 'total_predictions', 'grade')


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db, name, correct, wrong, deleted_correct=0):
    b = Blogger(name=name, platform='weibo')
    db.add(b)
    db.flush()
    for i in range(correct):
        db.add(Prediction(blogger_id=b.id, post_id=1, sector='半导体', fund_code='512480',
                         prediction_type='up', prediction_date=date.today() - timedelta(days=30),
                         target_date=date.today() - timedelta(days=2), is_correct=True,
                         verify_score=90))
    for i in range(wrong):
        db.add(Prediction(blogger_id=b.id, post_id=1, sector='半导体', fund_code='512480',
                         prediction_type='up', prediction_date=date.today() - timedelta(days=30),
                         target_date=date.today() - timedelta(days=2), is_correct=False,
                         verify_score=10))
    for i in range(deleted_correct):
        db.add(Prediction(blogger_id=b.id, post_id=1, sector='半导体', fund_code='512480',
                         prediction_type='up', prediction_date=date.today() - timedelta(days=30),
                         target_date=date.today() - timedelta(days=2), is_correct=True,
                         is_deleted=True, verify_score=90))
    db.flush()
    return b


def test_the_top_response_carries_every_key_the_modal_renders(db):
    _seed(db, '榜一大哥', correct=4, wrong=1)
    out = get_top_bloggers(limit=10, db=db)
    assert out['success'] is True and out['data'], '弹窗会显示"暂无数据"而不是报错'
    row = out['data'][0]
    missing = [k for k in KEYS_THE_PAGE_READES if k not in row]
    assert not missing, '弹窗读的是这些键，接口没给：%s' % missing
    assert row['hit_verified'] == 5 and row['hit_correct'] == 4, \
        '弹窗那一格印的是 判对/已验证，分子分母得是真的对得上'
    assert abs(row['hit_rate'] - 80.0) < 0.01
    assert row['weighted_score'] == row['accuracy_rate'], '两列同名不同数会让老板以为页面坏了'
    # 接口自己的口径说明：页面把它渲染成"接口自己的口径：…"，改名不等于没这回事
    assert 'metric_note' in out and 'hit_rate' in out['metric_note']


def test_the_top_list_only_admits_five_or_more_live_verdicts(db):
    """门槛（≥5 条**存活**已验证结论）是这条路由的全部意义，必须钉住两头。"""
    _seed(db, '刚够格', correct=3, wrong=2)              # 5 条存活 ⇒ 上榜
    _seed(db, '差一条', correct=2, wrong=2)              # 4 条 ⇒ 不上榜
    _seed(db, '靠回收站凑数', correct=2, wrong=2, deleted_correct=9)
    names = [r['name'] for r in get_top_bloggers(limit=10, db=db)['data']]
    assert '刚够格' in names, '够数的被挡在外面'
    assert '差一条' not in names, '不够数的混进来了'
    assert '靠回收站凑数' not in names, '软删的结论被当成"已验证"凑够了门槛'
    assert names.index('刚够格') == 0, '排序应按存活命中率降序：%s' % names


def test_limit_is_honoured_and_zero_verified_bloggers_never_divide_by_zero(db):
    for i in range(3):
        _seed(db, '博主%d' % i, correct=5, wrong=0)
    _seed(db, '零结论', correct=0, wrong=0)
    out = get_top_bloggers(limit=2, db=db)
    assert len(out['data']) == 2
    assert all(r['hit_verified'] >= 5 for r in out['data'])
    assert '零结论' not in [r['name'] for r in out['data']], '0 分母不该上榜（更不该除零）'
