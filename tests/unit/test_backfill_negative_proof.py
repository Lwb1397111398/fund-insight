# -*- coding: utf-8 -*-
"""S7-2 / S7-b：结构性不可验必须先"问过数据源"，且负结果要能被记住。

两件事一起钉住：
1. `backfill_history_range` 向数据源要过而它返回空 ⇒ 记一条负凭据，
   下次同样的窗口不再重复打接口（Cron 不再为补不到的历史天天白跑）；
2. 验证侧只有拿到**盖得住本窗口、且没过期**的负凭据，才敢说
   "这段历史源端也没有 ⇒ 结构性不可验"；没问过就照旧报"数据不足"。

顺带一条实测纠偏：短窗口的免补拉判据原来只看"起点是否被覆盖"，
于是"本地净值停在 2020-12-08、预测窗口在 2026 年 9 月"这类行（实测 003033）
**永远进不到补拉分支**，也就永远无法被证明 —— 现在要求窗口内至少 1 条才允许免打。
"""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.fund import backfill_proofs
from src.models.database import Base, FundHistory, SystemConfig


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def manager():
    """数据源桩：返回什么由用例决定，并记录每一次请求。"""
    from src.fund.fund_api import FundDataManager

    m = FundDataManager.__new__(FundDataManager)     # 不建真实 API 客户端
    calls = []
    box = {'rows': []}

    class _Api:
        def get_fund_history_range(self, code, start, end):
            calls.append((code, start, end))
            return list(box['rows'])

    m.api = _Api()
    return m, calls, box


def _seed(db, code, days):
    for d in days:
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0))
    db.commit()


def test_empty_source_answer_becomes_a_proof(manager):
    m, calls, box = manager
    db = _session()
    _seed(db, '003033', [date(2020, 12, 8)])          # 本地净值只到 2020 年
    box['rows'] = []                                  # 源端对 2026 年那段什么也没有
    assert m.backfill_history_range('003033', date(2026, 9, 3), date(2026, 9, 10), db=db) == 0
    assert calls, '窗口整段空着却压根没问数据源'
    proof = backfill_proofs.read_proof(db, '003033')
    assert proof and proof['start'] <= date(2026, 9, 3) and proof['end'] >= date(2026, 9, 10)


def test_second_request_for_the_same_window_skips_the_network(manager):
    m, calls, box = manager
    db = _session()
    _seed(db, '515440', [date(2026, 9, 2)])
    start, end = date(2026, 7, 23), date(2026, 7, 30)
    m.backfill_history_range('515440', start, end, db=db)
    db.commit()
    assert len(calls) == 1
    m.backfill_history_range('515440', date(2026, 7, 27), date(2026, 7, 28), db=db)
    assert len(calls) == 1, '被已有凭据盖住的窗口又打了一次接口'


def test_wider_window_still_probes_and_widens_the_proof(manager):
    m, calls, box = manager
    db = _session()
    _seed(db, '158038', [date(2026, 9, 7)])
    m.backfill_history_range('158038', date(2026, 8, 28), date(2026, 9, 4), db=db)
    db.commit()
    assert len(calls) == 1
    m.backfill_history_range('158038', date(2026, 8, 20), date(2026, 9, 4), db=db)
    db.commit()
    assert len(calls) == 2, '更宽的窗口不该被旧凭据放过'
    assert backfill_proofs.read_proof(db, '158038')['start'] == date(2026, 8, 20)


def test_proof_expires_and_source_is_asked_again(manager):
    m, calls, box = manager
    db = _session()
    _seed(db, '512680', [date(2026, 7, 9)])
    start, end = date(2026, 6, 1), date(2026, 6, 30)
    m.backfill_history_range('512680', start, end, db=db)
    db.commit()
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == backfill_proofs.proof_key('512680')).first()
    stale = datetime.now() - timedelta(days=backfill_proofs.TTL_DAYS + 3)
    payload = json.loads(row.config_value)
    payload['checked_at'] = stale.isoformat(timespec='seconds')
    row.config_value = json.dumps(payload, ensure_ascii=False)
    db.commit()
    m.backfill_history_range('512680', start, end, db=db)
    assert len(calls) == 2, '凭据过期后必须重新问数据源（历史可能被补录）'


def test_dense_window_still_skips_without_a_proof(manager):
    """原有意图不能退化：窗口本来就有数据的，不该为了留凭据去打接口。"""
    m, calls, box = manager
    db = _session()
    have = [date(2026, 7, 1) + timedelta(days=i) for i in range(31)]
    _seed(db, '510300', [d for d in have if d.weekday() < 5])
    assert m.backfill_history_range('510300', date(2026, 7, 1), date(2026, 7, 31), db=db) == 0
    assert calls == []
    assert backfill_proofs.read_proof(db, '510300') is None


def test_partial_answer_is_also_remembered(manager):
    """源端给了 1 条但窗口要 2 条：再问一次还是那 1 条，所以第二次不该再打接口。

    158038 实测就是这样（源端只提供 09-07 那一条），不记部分结果的话，
    这类预测会每天重问一遍、每天照旧报"数据不足"。
    """
    m, calls, box = manager
    db = _session()
    _seed(db, '158038', [date(2026, 9, 7)])
    box['rows'] = [{'date': date(2026, 9, 7), 'nav': 1.2, 'growth': 0.0}]
    m.backfill_history_range('158038', date(2026, 9, 1), date(2026, 9, 8), db=db)
    db.commit()
    assert len(calls) == 1
    assert backfill_proofs.read_proof(db, '158038')['source_rows'] == 1
    box['rows'] = []
    m.backfill_history_range('158038', date(2026, 9, 2), date(2026, 9, 6), db=db)
    assert len(calls) == 1, '源端给过的部分结果没被记住，又打了一次接口'


def test_availability_only_claims_unverifiable_when_proven(test_db):
    from src.services.prediction_verify_service import PredictionVerifyService

    code, start, end = '003033', date(2026, 9, 3), date(2026, 9, 10)
    _seed(test_db, code, [date(2020, 12, 8)])
    svc = PredictionVerifyService(test_db)
    r = svc._check_fund_data_availability(fund_code=code, nav_start_date=start,
                                          window_end=end, target_date=end,
                                          today=date(2026, 9, 21))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r

    backfill_proofs.record_probe(test_db, code, start - timedelta(days=2),
                                 end + timedelta(days=2), 0)
    test_db.commit()
    r2 = svc._check_fund_data_availability(fund_code=code, nav_start_date=start,
                                           window_end=end, target_date=end,
                                           today=date(2026, 9, 21))
    assert r2['reason'] == 'no_source_history', r2
    assert '结构性不可验' in r2['message'] and '数据源在' in r2['message'], r2
