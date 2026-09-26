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
import ast
import json
from datetime import date, datetime, timedelta
from pathlib import Path

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
            return None if box['rows'] is None else list(box['rows'])

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
    for probe in payload.get('probes') or []:
        probe['checked_at'] = stale.isoformat(timespec='seconds')
    row.config_value = json.dumps(payload, ensure_ascii=False)
    db.commit()
    m.backfill_history_range('512680', start, end, db=db)
    assert len(calls) == 2, '凭据过期后必须重新问数据源（历史可能被补录）'


def test_re_asking_refreshes_the_proof():
    """第 13 轮 BLOCKER-1：过期后重问到的凭据**必须立刻可用**。

    上一版把相邻/相交的空答复并成一条、时间戳钉在较旧的半段上 ⇒ 今天刚问到的凭据
    从第一次问询起就计时，第 3 天起 `fresh()` 永远返回 None：
    补拉每天照旧打接口（S7-b 要消灭的行为），结构性归因第 3 天翻回"数据不足"。
    """
    db = _session()
    stale = datetime.now() - timedelta(days=backfill_proofs.EMPTY_TTL_DAYS + 1)
    backfill_proofs.record_probe(db, '003033', date(2026, 6, 1), date(2026, 6, 30), 0)
    db.commit()
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == backfill_proofs.proof_key('003033')).first()
    payload = json.loads(row.config_value)
    for probe in payload['probes']:
        probe['checked_at'] = stale.isoformat()
    row.config_value = json.dumps(payload, ensure_ascii=False)
    db.commit()

    start, end = date(2026, 6, 1), date(2026, 6, 30)
    assert backfill_proofs.fresh(db, '003033', start, end) is None, '过期凭据不该继续生效'

    # 今天重问，结果仍然是"没有" ⇒ 凭据必须重新可用（相邻/相同窗口都不许把时间戳钉回去）
    backfill_proofs.record_probe(db, '003033', start, end, 0)
    db.commit()
    fresh_proof = backfill_proofs.fresh(db, '003033', start, end)
    assert fresh_proof is not None, '刚问到的凭据当场不可用（BLOCKER-1 复现）'
    # 相邻窗口也各自计时，不互相借时间戳、也不互相顶替
    backfill_proofs.record_probe(db, '003033', date(2026, 7, 1), date(2026, 7, 31), 0)
    db.commit()
    probes = backfill_proofs.read_probes(db, '003033')
    assert len(probes) == 2, '相邻空答复被并成一条（时间戳会互相污染）'
    assert backfill_proofs.fresh(db, '003033', start, end) is not None


def test_dense_window_still_skips_without_a_proof(manager):
    """原有意图不能退化：窗口本来就有数据的，不该为了留凭据去打接口。"""
    m, calls, box = manager
    db = _session()
    have = [date(2026, 7, 1) + timedelta(days=i) for i in range(31)]
    _seed(db, '510300', [d for d in have if d.weekday() < 5])
    assert m.backfill_history_range('510300', date(2026, 7, 1), date(2026, 7, 31), db=db) == 0
    assert calls == []
    assert backfill_proofs.read_proof(db, '510300') is None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.encoding = 'utf-8'

    def json(self):
        return self._payload


class _FakeSession:
    """替掉 `FundAPI.session`：按页序吐预设响应，`None` 表示这次请求抛异常。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get(self, *a, **kw):
        page = self.pages[self.calls] if self.calls < len(self.pages) else None
        self.calls += 1
        if page is None:
            raise TimeoutError('模拟超时')
        return _FakeResp(page)


def _envelope(rows, total=None):
    return {'Data': {'LSJZList': rows}, 'ErrCode': 0,
            'TotalCount': total if total is not None else len(rows)}



@pytest.mark.parametrize('payload,expect_rows', [
    (_envelope([]), []),                                    # 实测：真·无数据长这样
    ({'Data': None, 'ErrCode': 0, 'TotalCount': 0}, None),   # 第 11 轮 BLOCKER-1
    ({'Data': {}, 'ErrCode': 0}, None),                      # 缺 LSJZList 键
    ({'Data': {'LSJZList': None}, 'ErrCode': 0}, None),      # 键在、值是 null（第 12 轮 B-1）
    ({'Data': {'LSJZList': {}}, 'ErrCode': 0}, None),        # 键在、值不是列表
    ({'Data': {'LSJZList': []}, 'ErrCode': 'E_LIMIT'}, None),  # 报错信封不许读成"没有"
    ({'ErrCode': 1}, None),                                  # 限流/报错信封
])
def test_envelope_shapes_separate_no_data_from_did_not_ask(payload, expect_rows):
    """`[]` 只能是"问过且没有"，其它都要表达成"没问到"（None）。"""
    from src.fund.fund_api import FundAPI

    api = FundAPI.__new__(FundAPI)
    api.session = _FakeSession([payload])
    api.headers = {}
    api.timeout = 5
    api.history_url = 'http://example.invalid'
    out = api.get_fund_history_range('510300', date(2026, 9, 1), date(2026, 9, 5))
    assert out == expect_rows, (payload, out)


def test_missing_total_count_still_pages_to_the_end():
    """第 12 轮 BLOCKER-2：`TotalCount` 缺失时**不许**把首页当整段问完。

    上一版把 total 回落成"当页行数"，于是 `len(results) >= total` 首页即恒真，
    翻页保护整体失效 —— 比改动前更差（东财倒序返回，丢的恰是区间最早那段）。
    """
    from src.fund.fund_api import FundAPI

    # 窗口锚在"真实今天的昨天"而不是写死日期：入库侧新加的净值门
    # （`usable_history_rows`：`nav_date > 今天（北京）` 一律不入库）会把样品里
    # "还没到的那天"滤掉 —— 写死 2026-09-01~10-20 的窗口一旦跨过今天，这条判数就会
    # 从 47 变成 26，量的就不再是"翻页有没有到底"了。
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=46)

    def page(n, offset):
        return {'Data': {'LSJZList': [
            {'FSRQ': (start + timedelta(days=offset + i)).isoformat(),
             'DWJZ': '1.0', 'JZZZL': '0'} for i in range(n)]}}

    api = FundAPI.__new__(FundAPI)
    sess = _FakeSession([page(20, 0), page(20, 20), page(7, 40)])   # 共 47 条，末页不足一页
    api.session = sess
    api.headers = {}
    api.timeout = 5
    api.history_url = 'http://example.invalid'
    rows = api.get_fund_history_range('510300', start, end)
    assert sess.calls == 3, '没 TotalCount 就只翻了一页'
    assert rows is not None and len(rows) == 47


def test_page_with_unparseable_rows_is_not_claimed_complete():
    """整页里有一行解析失败 ⇒ 丢了某一天，不能算"问完了"。"""
    from src.fund.fund_api import FundAPI

    bad = {'Data': {'LSJZList': [{'FSRQ': '2026/09/01', 'DWJZ': '1.0', 'JZZZL': '0'}],
                    }, 'TotalCount': 1}
    api = FundAPI.__new__(FundAPI)
    api.session = _FakeSession([bad])
    api.headers = {}
    api.timeout = 5
    api.history_url = 'http://example.invalid'
    assert api.get_fund_history_range('510300', date(2026, 9, 1),
                                      date(2026, 9, 5)) is None


def test_rolled_back_transaction_leaves_no_proof():
    """第 12 轮 MAJOR-5：SQLite 上"没有外层事务时 SAVEPOINT 自己就是事务"，
    RELEASE 等于提交 ⇒ 调用方回滚后凭据不该存在。上一版正是这样漏的。"""
    db = _session()
    db.query  # 只做过只读操作，没有开启写事务
    backfill_proofs.record_probe(db, '510300', date(2026, 9, 1), date(2026, 9, 5), 0)
    db.rollback()
    assert backfill_proofs.read_probes(db, '510300') == [], '回滚后凭据还留在库里'


def test_warm_and_cold_paths_agree_on_history_and_points(test_db):
    """第 12 轮 MAJOR-6：预热与不预热必须给出同样的点数与同样的净值序列。

    `_history` 切片只有 ±120 天，窗口部分越界时旧代码会采信"非空但残缺"的结果，
    而它是 `calculate_process_metrics` 的输入 ⇒ Cron 与手动按钮能判出相反结论。
    """
    from src.services.prediction_verify_service import PredictionVerifyService

    code = '510500'
    today = date(2026, 9, 21)
    days = [today - timedelta(days=i) for i in range(1, 161)]
    _seed(test_db, code, list(reversed(days)))
    svc = PredictionVerifyService(test_db)
    start = today - timedelta(days=150)
    end = today - timedelta(days=100)
    cold_points = svc._check_fund_data_availability(
        fund_code=code, nav_start_date=start, window_end=end, target_date=end,
        today=today)['data_points']
    cold_hist = len(svc.get_nav_history(code, start, end))
    svc._warm_cache([type('P', (), {'fund_code': code, 'prediction_date': start,
                                    'target_date': end})()], today)
    warm_points = svc._check_fund_data_availability(
        fund_code=code, nav_start_date=start, window_end=end, target_date=end,
        today=today)['data_points']
    warm_hist = len(svc.get_nav_history(code, start, end))
    assert warm_points == cold_points == 51, (warm_points, cold_points)
    assert warm_hist == cold_hist == 51, (warm_hist, cold_hist)


def test_truncated_pagination_is_not_claimed_as_complete():
    """翻页中途遇到坏信封 ⇒ 尾巴没问到，整段都算"没问到"，不许记凭据。"""
    from src.fund.fund_api import FundAPI

    full_page = [{'FSRQ': (date(2026, 9, 1) + timedelta(days=i)).isoformat(),
                  'DWJZ': '1.0', 'JZZZL': '0'} for i in range(20)]
    api = FundAPI.__new__(FundAPI)
    api.session = _FakeSession([_envelope(full_page, total=40), {'Data': None}])
    api.headers = {}
    api.timeout = 5
    api.history_url = 'http://example.invalid'
    assert api.get_fund_history_range('510300', date(2026, 9, 1),
                                      date(2026, 10, 20)) is None


def test_non_empty_probes_never_merge(manager):
    """第 11 轮 M-B：只有两边都是空答复才允许并段。

    `[07-01,08-31] 21 条` + `[08-31,09-30] 21 条` 并成一条 21 条的宽凭据，
    等于把 BLOCKER-2 的洞留一半，而且条数是编出来的。
    """
    db = _session()
    backfill_proofs.record_probe(db, '515000', date(2026, 7, 1), date(2026, 8, 31), 21)
    backfill_proofs.record_probe(db, '515000', date(2026, 8, 31), date(2026, 9, 30), 21)
    db.commit()
    probes = backfill_proofs.read_probes(db, '515000')
    assert len(probes) == 2, '非空答复被并成了一段'
    assert backfill_proofs.covering_probe(
        probes, date(2026, 7, 1), date(2026, 9, 30)) is None, '更宽的窗口被拼出来的包络盖住了'


def test_probe_cap_keeps_the_most_recent_probes(manager):
    """第 11 轮 M-C：超上限要丢**最久没问**的那些，不是"起点最老"的那些。

    按 start 排序截尾会把老窗口的探测丢掉，而那些窗口正是每天要重问的
    （实测单只 515000 有 77 个互不相交的窗口），永远不收敛。
    """
    db = _session()
    old_start = date(2025, 5, 5)
    backfill_proofs.record_probe(db, '512480', old_start, old_start + timedelta(days=2), 0)
    db.commit()
    # 互不相交、也不首尾相接（隔 90 天），否则会被 M-B 的合并规则并成一段，测不到上限
    for i in range(backfill_proofs.MAX_PROBES + 5):
        day = date(2026, 1, 5) + timedelta(days=i * 90)
        backfill_proofs.record_probe(db, '512480', day, day + timedelta(days=2), 0)
    db.commit()
    probes = backfill_proofs.read_probes(db, '512480')
    assert len(probes) <= backfill_proofs.MAX_PROBES
    newest = date(2026, 1, 5) + timedelta(days=(backfill_proofs.MAX_PROBES + 4) * 90)
    assert any(p['start'] == newest for p in probes), '刚花请求换来的新探测被丢了'
    assert not any(p['start'] == old_start for p in probes), '最久没问的老探测该被挤掉'


def test_record_probe_reports_its_own_write_to_the_caller():
    """第 11 轮 M-A：flush 之后 `Session.new/dirty` 都空，调用方得有别的方式知道写了。"""
    db = _session()
    assert not db.new and not db.dirty
    backfill_proofs.record_probe(db, '159915', date(2026, 9, 1), date(2026, 9, 5), 0)
    assert not db.new and not db.dirty, 'flush 后集合里已经没有它了（这正是 M-A 的坑）'
    assert db.info.get('proof_writes') == 1


def test_inverted_window_can_never_be_covered():
    """第 11 轮 MINOR-4：`target < prediction_date` 的脏数据不许被任何凭据判成不可验。"""
    db = _session()
    backfill_proofs.record_probe(db, '588200', date(2026, 1, 1), date(2026, 12, 31), 0)
    db.commit()
    assert backfill_proofs.fresh(db, '588200', date(2026, 6, 10), date(2026, 6, 1)) is None


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


def test_a_failed_request_records_nothing(manager):
    """第 10 轮 BLOCKER-1：一次抖动不能被写成"源端确实没有"，否则真数据永远回不来。

    数据源调用现在用 `None` 表达"这次没问到"（超时/限流页/翻页触顶），
    与"问过且答案是 0 条"（空列表）严格分开。
    """
    m, calls, box = manager
    db = _session()
    _seed(db, '003033', [date(2020, 12, 8)])
    box['rows'] = None                       # 模拟请求失败
    assert m.backfill_history_range('003033', date(2026, 9, 3), date(2026, 9, 10), db=db) == 0
    assert backfill_proofs.read_probes(db, '003033') == [], '请求失败却记了凭据'
    assert backfill_proofs.fresh(db, '003033', date(2026, 9, 3), date(2026, 9, 10)) is None


def test_disjoint_probes_do_not_cover_the_gap(manager):
    """第 10 轮 BLOCKER-2：两次互不相交的探测不许并成一个包络，把中间空洞谎称问过。"""
    m, calls, box = manager
    db = _session()
    _seed(db, '515440', [date(2026, 9, 2)])
    box['rows'] = []
    m.backfill_history_range('515440', date(2026, 7, 1), date(2026, 7, 3), db=db)
    db.commit()
    m.backfill_history_range('515440', date(2026, 9, 15), date(2026, 9, 17), db=db)
    db.commit()
    assert len(calls) == 2
    probes = backfill_proofs.read_probes(db, '515440')
    assert len(probes) == 2, '不相交的区间被并成了一段'
    # 中间那段从没问过 ⇒ 必须再问，不能拿包络当证据
    assert backfill_proofs.fresh(db, '515440', date(2026, 8, 10), date(2026, 8, 14)) is None
    m.backfill_history_range('515440', date(2026, 8, 10), date(2026, 8, 14), db=db)
    assert len(calls) == 3


def test_adjacent_empty_probes_stay_separate(manager):
    """第 13 轮 BLOCKER-1：相邻的空答复**不再合并**。

    合并要么让今天刚问到的那条继承旧时间戳（当场过期 ⇒ 每天重问、归因天天翻脸），
    要么让旧半段借新时间续命（谎称最近问过）。两个都不接受 ⇒ 一个窗口一条记录、
    各自计 TTL。代价是相邻窗口各问各的，换来的是凭据语义可证伪。
    """
    m, calls, box = manager
    db = _session()
    _seed(db, '158038', [date(2026, 10, 1)])
    box['rows'] = []
    m.backfill_history_range('158038', date(2026, 7, 1), date(2026, 7, 10), db=db)
    db.commit()
    m.backfill_history_range('158038', date(2026, 7, 11), date(2026, 7, 20), db=db)
    db.commit()
    probes = backfill_proofs.read_probes(db, '158038')
    assert len(probes) == 2, '两个窗口被并成一条，时间戳会互相污染'
    assert backfill_proofs.fresh(db, '158038', date(2026, 7, 1), date(2026, 7, 10)) is not None
    assert backfill_proofs.fresh(db, '158038', date(2026, 7, 1), date(2026, 7, 20)) is None, \
        '拼出来的宽窗口不许被当作"问过"'


def test_recent_window_proof_expires_faster_than_old_one():
    """窗口终点还在近 30 天内时只信 1 天：那天的净值当晚就会发布（第 13 轮 MINOR-10）。"""
    db = _session()
    today = date.today()
    backfill_proofs.record_probe(db, '588000', today - timedelta(days=5),
                                 today - timedelta(days=1), 3)
    db.commit()
    recent = backfill_proofs.read_probes(db, '588000')[0]
    assert backfill_proofs._ttl_for(recent) == 1
    old_start = today - timedelta(days=200)
    backfill_proofs.record_probe(db, '588001', old_start, old_start + timedelta(days=10), 3)
    db.commit()
    old = backfill_proofs.read_probes(db, '588001')[0]
    assert backfill_proofs._ttl_for(old) == backfill_proofs.TTL_DAYS


def test_availability_only_claims_unverifiable_when_proven(test_db):
    from src.services.prediction_verify_service import PredictionVerifyService

    code, start, end = '003033', date(2026, 9, 3), date(2026, 9, 10)
    _seed(test_db, code, [date(2020, 12, 8)])
    svc = PredictionVerifyService(test_db)
    r = svc._check_fund_data_availability(fund_code=code, nav_start_date=start,
                                          window_end=end, target_date=end,
                                          today=date(2026, 9, 21))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r

    # 写侧 `now=` 与读侧 `today=` 必须同一天，否则跨过北京零点就用例自己变红
    backfill_proofs.record_probe(test_db, code, start - timedelta(days=2),
                                 end + timedelta(days=2), 0,
                                 now=datetime(2026, 9, 21, 9, 30))
    test_db.commit()
    r2 = svc._check_fund_data_availability(fund_code=code, nav_start_date=start,
                                           window_end=end, target_date=end,
                                           today=date(2026, 9, 21))
    assert r2['reason'] == 'no_source_history', r2
    assert '结构性不可验' in r2['message'] and '数据源在' in r2['message'], r2


def test_ttl_does_not_depend_on_the_wall_clock():
    """第 14 轮 MAJOR-5：`covering_probe/fresh` 支持注入"今天是哪天"，
    而 TTL 分档以前自己调 `date.today()` ⇒ 同入参、不同墙上时钟给出相反答案，
    历史回放会凭一个当时并不存在的宽限判"这段问过"。

    同一条凭据（窗口终点 06-04、07-01 问过的、给了几条）：
    "今天"= 07-04 时终点距今 30 天 ⇒ 落进"近期只信 1 天"档 ⇒ 已过期；
    "今天"= 07-05 时距今 31 天 ⇒ 回到 7 天档 ⇒ 仍新鲜。
    """
    probes = [{'start': date(2026, 5, 1), 'end': date(2026, 6, 4),
               'source_rows': 5, 'checked_at': '2026-07-01T10:00:00'}]
    assert backfill_proofs.covering_probe(
        probes, date(2026, 5, 10), date(2026, 6, 1), today=date(2026, 7, 4)) is None, \
        '近期窗口该只信 1 天，注入的 today 被忽略就会判成"问过"'
    assert backfill_proofs.covering_probe(
        probes, date(2026, 5, 10), date(2026, 6, 1), today=date(2026, 7, 5)) is not None, \
        '久远窗口按 7 天档，4 天前问过应当算新鲜'


def test_future_timestamped_proof_is_not_believed():
    """第 14 轮 MINOR-1：`checked_at` 来自未来的凭据不能永不过期。"""
    db = _session()
    future = (datetime.now() + timedelta(days=400)).isoformat()
    backfill_proofs.record_probe(db, '510300', date(2026, 6, 1), date(2026, 6, 30), 0,
                                 now=datetime(2026, 9, 22, 9, 30))
    db.commit()
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == backfill_proofs.proof_key('510300')).first()
    payload = json.loads(row.config_value)
    payload['probes'][0]['checked_at'] = future
    row.config_value = json.dumps(payload, ensure_ascii=False)
    db.commit()
    assert backfill_proofs.fresh(db, '510300', date(2026, 6, 5), date(2026, 6, 20),
                                 today=date(2026, 9, 22)) is None, '未来时间戳 = 永久免检'


def _is_fixed_date(node, fixed_names):
    """字面 `date(2026, 9, 21)`，或被赋成这种字面值的变量名。"""
    if isinstance(node, ast.Name):
        return node.id in fixed_names
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'date'
            and all(isinstance(a, ast.Constant) for a in node.args))


def _clock_mismatch_lines(path):
    """同一函数里"读侧钉死 today"与"写侧凭据打墙上时钟"并存的文件行号。"""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    hits = []
    for fn in (n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        fixed_names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _is_fixed_date(node.value, set()):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        fixed_names.add(tgt.id)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        reads_fixed = any(
            kw.arg == 'today' and _is_fixed_date(kw.value, fixed_names)
            for c in calls for kw in c.keywords
        )
        if not reads_fixed:
            continue
        for c in calls:
            func = c.func
            if getattr(func, 'attr', None) != 'record_probe':
                continue
            if not any(kw.arg == 'now' for kw in c.keywords):
                hits.append((path.name, fn.name, c.lineno))
    return hits


def test_fixed_today_reads_never_stamp_proofs_with_the_wall_clock():
    """凭据的 `checked_at` 默认取墙上时钟，而 `_fresh` 把"来自未来"的时间戳判为不可信
    （age < -1）。所以只要读侧注入了固定 `today=`，写侧就必须同时注入 `now=`——
    否则这条用例的**通过与否取决于哪天跑它**：第 27 轮 C 实测 09-22 23:39 全绿的
    `tests/unit`，跨过北京零点后有 2 条与代码无关地变红
    （`test_availability_only_claims_unverifiable_when_proven`、
    `test_dead_fund_needs_evidence_before_being_called_unverifiable`）。
    第 17 轮 MINOR-1 加 `now=` 参数时就为躲这个坑写过说明，同一个坑在它自己的用例上复发了。
    """
    unit_dir = Path(__file__).resolve().parent
    offenders = []
    for py in sorted(unit_dir.glob('*.py')):
        offenders.extend(_clock_mismatch_lines(py))
    assert not offenders, (
        '这些 record_probe 打了墙上时钟，却与固定 today= 的读同一函数共存 ⇒ 跨过零点会自己变红，'
        '请补 now=（与读侧同一天）：' + '; '.join(f'{f}:{fn}:{ln}' for f, fn, ln in offenders))

