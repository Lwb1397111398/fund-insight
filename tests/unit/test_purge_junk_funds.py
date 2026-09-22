# -*- coding: utf-8 -*-
"""`scripts/purge_junk_funds.py` 的安全阀。

批量删除在这个仓库里已经付过账，所以"什么情况下必须拒绝"要有会红的用例，
而不是靠脚本作者（我）记得看输出。
"""
import importlib.util
import os
from datetime import date

import pytest

from src.models.database import FundHistory, FundInfo, Prediction

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    'purge_junk', os.path.join(ROOT, 'scripts', 'purge_junk_funds.py'))
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)


def _add_fund(db, code, name='垃圾码测试'):
    if not db.query(FundInfo).filter_by(fund_code=code).first():
        db.add(FundInfo(fund_code=code, fund_name=name))
    db.commit()


def test_a_code_with_live_predictions_is_a_blocker(test_db):
    """老板批准的是"垃圾码"，不是"还有人在用的码"：有活预测就整批不动。"""
    from src.models.database import Blogger, Post
    blogger = Blogger(name='垃圾码博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, content='垃圾码帖子',
                post_date=date(2026, 1, 5))
    test_db.add(post)
    test_db.flush()
    _add_fund(test_db, 'ZZZ001')
    test_db.add(Prediction(post_id=post.id, blogger_id=blogger.id, fund_code='ZZZ001',
                           fund_name='X', prediction_type='up', sector='测试',
                           prediction_date=date(2026, 1, 5), prediction_period='1周',
                           target_date=date(2026, 1, 12)))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ001',))
    assert rows[0]['live_predictions'] == 1
    actions, blockers = purge.plan(rows)
    assert blockers and not any(a[0] == 'fund_info' for a in actions), \
        '有活预测的码被放行删除 ⇒ 预测的标的会指向空'


def test_nav_rows_also_block_the_delete(test_db):
    """净值不为零就不是"垃圾"：这类"股票名挂在别人基金码"的行只能改名不能删。"""
    _add_fund(test_db, 'ZZZ002')
    test_db.add(FundHistory(fund_code='ZZZ002', nav_date=date(2026, 1, 5), nav=1.0))
    test_db.commit()
    actions, blockers = purge.plan(purge.inspect(test_db, ('ZZZ002',)))
    assert blockers, '有净值历史却照样删 ⇒ 同码真基金的数据被毁'


def test_truly_junk_code_is_planned_with_its_mapping_rows_first(test_db):
    """零预测零净值才允许删，且必须先删映射行（外键指向 fund_info）。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ003', '某股票名挂在基金码')
    test_db.add(SectorFundMapping(sector_name='ZZZ测试板块', fund_code='ZZZ003',
                                  fund_name='某股票名挂在基金码', is_active=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ003',))
    actions, blockers = purge.plan(rows)
    assert not blockers
    kinds = [a[0] for a in actions]
    assert kinds == ['mapping', 'fund_info'], \
        '顺序必须是先映射后档案：反过来的话外键会拒（镜像开了 foreign_keys）'
    assert actions[0][4] == 0, '同板块没有别的可服务行时要说出来（删完这个板块就没标的了）'


def test_missing_code_is_skipped_not_fatal(test_db):
    """镜像与生产现状不同（生产每个垃圾码还挂 1 行映射）⇒ 这个库里没有就跳过。"""
    actions, blockers = purge.plan(purge.inspect(test_db, ('不存在的码',)))
    assert actions == [] and blockers == []
