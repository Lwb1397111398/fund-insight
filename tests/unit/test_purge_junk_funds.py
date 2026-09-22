# -*- coding: utf-8 -*-
"""`scripts/purge_junk_funds.py` 的安全阀。

批量删除在这个仓库里已经付过账，所以"什么情况下必须拒绝"要有会红的用例，
而不是靠脚本作者（我）记得看输出。
"""
import importlib.util
import os
from datetime import date, datetime

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


def test_soft_deleted_prediction_blocks_until_explicitly_allowed(test_db):
    """回收站里那条预测没有外键挡着：删了档案就留下一条"打开即 500"的可恢复行。

    第 24 轮 B 复现：镜像 `predictions.id=3009`（603758、is_deleted=1）在旧版预检里
    根本数不到（只数 `is_deleted==0`），脚本照样报告"零预测，可以删"。
    """
    from src.models.database import Blogger, Post
    blogger = Blogger(name='回收站博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, content='回收站帖子', post_date=date(2026, 1, 5))
    test_db.add(post)
    test_db.flush()
    _add_fund(test_db, 'ZZZ004')
    test_db.add(Prediction(post_id=post.id, blogger_id=blogger.id, fund_code='ZZZ004',
                           fund_name='X', prediction_type='up', sector='测试',
                           prediction_date=date(2026, 1, 5), prediction_period='1周',
                           target_date=date(2026, 1, 12), is_deleted=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ004',))
    assert rows[0]['live_predictions'] == 0 and rows[0]['dead_predictions'] == 1, \
        '预检看不见软删行'
    actions, blockers = purge.plan(rows)
    assert blockers and not actions, '有软删预测却静默放行 ⇒ 留下指向空档案的行'
    actions, blockers = purge.plan(rows, allow_dead_predictions=True)
    assert not blockers and [a[0] for a in actions] == ['fund_info'], \
        '显式允许之后要能过，否则这个开关是假的'


def test_owner_locked_mapping_row_blocks_the_delete(test_db):
    """老板逐行确认过的有意代理不能被机器"顺手删掉"，要删必须明说。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ005')
    test_db.add(SectorFundMapping(sector_name='ZZZ老板板块', fund_code='ZZZ005',
                                  fund_name='有意代理', is_active=True, reviewed=True,
                                  reviewed_by='owner', owner_locked=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ005',))
    actions, blockers = purge.plan(rows)
    assert blockers and not actions, '老板锁定的映射行被机器删了'
    actions, blockers = purge.plan(rows, allow_owner_rows=True)
    assert [a[0] for a in actions] == ['mapping', 'fund_info']


def test_retry_rows_are_planned_before_the_archive(test_db):
    """`fund_sync_retry` 也对外键指向 fund_info：上一版只认 sector_fund_mapping。"""
    from src.models.database import FundSyncRetry
    _add_fund(test_db, 'ZZZ006')
    test_db.add(FundSyncRetry(fund_code='ZZZ006', retry_count=1,
                              next_retry_time=datetime(2026, 1, 6, 9, 0)))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ006',))
    assert rows[0]['retry_rows'] == 1
    actions, blockers = purge.plan(rows)
    assert not blockers
    assert [a[0] for a in actions] == ['fund_sync_retry', 'fund_info'], \
        '顺序必须先把引用表清干净，再删档案'


def test_backup_then_restore_puts_everything_back(test_db, tmp_path):
    """"可回滚"不是一个形容词：备份 JSON 写出来、删干净、再按它逐列还原回来。

    第 24 轮 A 的 MINOR-8 第一条：上一版只写着"按备份 JSON 重新 insert"，
    仓库里根本没有那段代码 ⇒ 承诺不成立。现在它是 `purge.restore()`。
    """
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ007', '可回滚测试')
    mapping = SectorFundMapping(sector_name='ZZZ回滚板块', fund_code='ZZZ007',
                                fund_name='可回滚测试', is_active=True, confidence=0.77)
    test_db.add(mapping)
    test_db.commit()
    mapping_id = mapping.id

    dump = tmp_path / 'purge-backup.json'
    dump.write_text(__import__('json').dumps(
        purge._dump_rows(test_db, ('ZZZ007',)), ensure_ascii=False, default=str), encoding='utf-8')
    test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').delete()
    test_db.query(FundInfo).filter_by(fund_code='ZZZ007').delete()
    test_db.commit()
    assert test_db.query(FundInfo).filter_by(fund_code='ZZZ007').first() is None

    purge.restore(test_db, str(dump))
    back = test_db.query(FundInfo).filter_by(fund_code='ZZZ007').first()
    assert back is not None and back.fund_name == '可回滚测试'
    m = test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').first()
    assert m is not None and m.id == mapping_id and abs(m.confidence - 0.77) < 1e-6, \
        '还原没按原 id/逐列回来：备份等于没备'

    # 幂等：同一个备份再跑一次不该翻倍
    purge.restore(test_db, str(dump))
    assert test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').count() == 1
