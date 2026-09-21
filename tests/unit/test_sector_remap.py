# -*- coding: utf-8 -*-
"""板块映射 → 预测纠偏的置信度门槛、别名命中、run_id 回滚。

这是 S4 第 4 步的安全网：批量改预测的 fund_code 会牵动准确率台账，
没有门槛与可回滚就等于拿老板的历史数据冒险。
"""
from datetime import date, timedelta

import pytest

from src.models.database import (
    Blogger, FundInfo, Post, Prediction, PredictionChangeLog,
    SectorAlias, SectorFundMapping,
)
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from scripts.restore_prediction_batch import apply_before_state


def _seed(db, *, sector='RMAP白酒', mapping_fund='RMAP01', confidence=None,
          reviewed_by=None, owner_locked=None, pred_fund='999999',
          verified=False, alias=None):
    blogger = Blogger(name='回滚测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='测试帖子', post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    for code in (mapping_fund, pred_fund):
        if not db.query(FundInfo).filter_by(fund_code=code).first():
            db.add(FundInfo(fund_code=code, fund_name='测试基金' + code))
    db.add(SectorFundMapping(sector_name=sector, fund_code=mapping_fund,
                             fund_name='测试基金' + mapping_fund, reviewed=True,
                             is_active=True, confidence=confidence,
                             reviewed_by=reviewed_by, owner_locked=owner_locked))
    if alias:
        if not db.query(SectorAlias).filter_by(alias_name=alias).first():
            db.add(SectorAlias(alias_name=alias, sector_name=sector, source='agent'))
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code=pred_fund,
        fund_name='测试基金' + pred_fund, sector=alias or sector, prediction_type='up',
        prediction_content='上涨', prediction_date=date(2026, 6, 1), prediction_period='1周',
        target_date=date(2026, 6, 8),
        status='success' if verified else 'pending',
        is_correct=True if verified else None,
        verify_count=1 if verified else 0,
        verify_score=80 if verified else None,
        start_nav=1.0 if verified else None,
        end_nav=1.2 if verified else None,
    )
    db.add(prediction)
    db.commit()
    return prediction


@pytest.fixture(autouse=True)
def _clean_remap_rows(db_session):
    """这些用例会 commit，测试之间会互相看见，用 RMAP 前缀隔离并跑完清理。"""
    yield
    for model in (PredictionChangeLog, Prediction, SectorFundMapping, SectorAlias, Post, Blogger):
        db_session.query(model).delete()
    db_session.commit()


def _service(db):
    return PredictionMaintenanceService(db)


def test_low_confidence_mapping_is_not_applied(db_session):
    _seed(db_session, confidence=0.5)
    result = _service(db_session).sync_sector_mappings(dry_run=False,
                                                       min_confidence=0.85,
                                                       run_id='t-low')
    assert result['predictions_updated'] == 0
    assert result['mappings_skipped_low_confidence'] == 1


def test_high_confidence_mapping_rewrites_and_resets(db_session):
    prediction = _seed(db_session, confidence=0.95, verified=True)
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-high')
    assert result['predictions_updated'] == 1
    assert result['verified_reset'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'
    assert prediction.status == 'pending'
    assert prediction.is_correct is None
    assert prediction.start_nav is None


def test_owner_locked_mapping_bypasses_confidence(db_session):
    prediction = _seed(db_session, confidence=None, owner_locked=True)
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-owner')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'


def test_alias_lets_synonym_sector_match(db_session):
    prediction = _seed(db_session, sector='RMAP绿电', confidence=0.95, alias='RMAP绿色电力')
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-alias')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'


def test_run_id_written_and_rollback_restores_previous_state(db_session):
    from src.utils.blogger_stats import recalculate_blogger_stats

    prediction = _seed(db_session, confidence=0.95, verified=True)
    pid, old_fund, old_status = prediction.id, prediction.fund_code, prediction.status
    _service(db_session).sync_sector_mappings(dry_run=False, min_confidence=0.85,
                                              run_id='t-rollback')
    logs = db_session.query(PredictionChangeLog).filter_by(run_id='t-rollback').all()
    assert len(logs) == 1, 'run_id 必须写进变更日志，否则无法整批回滚'
    assert logs[0].before_state['fund_code'] == old_fund

    # 回滚：取该 run 最早一条 before_state 全量还原（走脚本里那份真代码，
    # 别在测试里抄一遍遍历 —— 抄的那份永远测不出清单少字段）
    log = min(logs, key=lambda x: x.id)
    apply_before_state(prediction, log.before_state)
    db_session.flush()
    recalculate_blogger_stats(db_session, prediction.blogger_id, commit=False)
    db_session.commit()
    db_session.refresh(prediction)
    assert prediction.fund_code == old_fund
    assert prediction.status == old_status
    assert prediction.is_correct is True
    assert prediction.start_nav == 1.0
