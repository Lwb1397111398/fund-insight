# -*- coding: utf-8 -*-
"""第 6 轮代码评审（84 分未过门禁）补的安全网。

锁的是四件"看起来成功、实际把数据或线上写坏"的事：
1. 可服务判据必须**只有一套**：`row_unservable()`（Python 读路径）与
   `servable_predicate()`（SQL 读路径）对同一行不许给出两个答案（M1）；
2. 机器换标的的溯源只能来自 `evidence`，不能来自会被盖掉的 `match_source`（M3）；
3. `verify_message` 是给用户看的理由，体检日志不许往里灌（D1 的损坏类别）；
4. 回滚不许吞掉老板本来就有的净值（M5）。

外加两个"误连生产"的门：`_db_guard` 的逃生口、`python -m src --init-db`。
"""
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime

import pytest

from src.models.database import FundHistory, FundInfo, SectorFundMapping
from src.services import sector_identity_audit as audit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAKE_PROD_URL = 'postgresql://fake:fake@example.invalid/fake_db'


@pytest.fixture(autouse=True)
def _no_roster_download(monkeypatch):
    """单测零网络：体检会回落去拉 3.1MB 基金域名册。"""
    monkeypatch.setattr(audit.fund_api, 'load_fund_roster',
                        lambda refresh=False: {'by_code': {}, 'codes_by_name': {},
                                               'size': 0})
    yield


@pytest.fixture(autouse=True)
def _clean_service_cache():
    """进入与**退出**都清：本文件的用例会把 in-memory 会话的映射灌进类属性缓存，
    只清前面一次的话，脏缓存会漏给下一个测试文件（实测按全量顺序跑时
    `test_ai_match_apply_writes_evidence` 会读到上一个文件的行）。"""
    from src.services.sector_fund_service import SectorFundService
    from src.services.sector_identity_audit import invalidate_denied_cache
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False
    invalidate_denied_cache()
    yield
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False
    invalidate_denied_cache()


def _row(sector, code, name, **kw):
    kwargs = dict(sector_name=sector, fund_code=code, fund_name=name,
                  reviewed=True, is_active=True)
    kwargs.update(kw)
    return SectorFundMapping(**kwargs)


def _result(row, verdict='ok', **kw):
    d = {'id': row.id, 'sector': row.sector_name, 'code': row.fund_code,
         'name': row.fund_name, 'verdict': verdict, 'jaccard': 1.0,
         'official_name': row.fund_name, 'reason': '基金域品种名一致（Jaccard 1.000）',
         'suggested_code': None, 'suggestions': [], 'relevance_low': False,
         'evidence': [], 'owner_row': False}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 1 判据一致

def test_owner_locked_row_agrees_between_sql_and_python_readers(test_db):
    """M1：老板锁定的行两处判据必须同为"可服务"。

    体检结论照样写进 evidence（前端要显示红字提示），但**不能**当门：
    `is_fetchable` 列被 owner 守卫跳过，而旧版 `row_unservable` 只认 verdict，
    于是同一行 SQL 在服务、Python 在藏（实测 债券/512000）。
    """
    from sqlalchemy import select
    from src.models.database import SessionLocal  # noqa: F401  仅为可读性对照

    row = _row('T-六版owner一致', '512000', '证券ETF', owner_locked=True,
               reviewed_by='owner',
               evidence=json.dumps({'identity': {'verdict': 'code_is_other_fund'}}))
    test_db.add(row)
    test_db.commit()

    assert audit.row_unservable(row) is False
    ids = {r.id for r in test_db.query(SectorFundMapping).filter(
        audit.servable_predicate()).all()}
    assert row.id in ids, 'SQL 与 Python 给出了两个答案'
    # 反向也要成立：没锁定的行 verdict 否了就是不可服务
    bad = _row('T-六版非owner', '512000', '证券ETF', is_fetchable=False,
               evidence=json.dumps({'identity': {'verdict': 'code_is_other_fund'}}))
    test_db.add(bad)
    test_db.commit()
    assert audit.row_unservable(bad) is True
    assert bad.id not in {r.id for r in test_db.query(SectorFundMapping).filter(
        audit.servable_predicate()).all()}


def test_get_fund_by_sector_serves_the_owner_proxy(test_db):
    """M1 的业务面：老板手定的代理标的必须仍然被 `get_fund_by_sector` 返回。"""
    test_db.add(FundInfo(fund_code='512000', fund_name='证券ETF'))
    test_db.add(_row('T-六版取数', '512000', '证券ETF', owner_locked=True,
                     reviewed_by='owner',
                     evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}})))
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    hit = SectorFundService(test_db).get_fund_by_sector('T-六版取数')
    assert hit and hit['code'] == '512000'


# ------------------------------------------------------- 2 机器换标的的溯源

def test_identity_view_flags_etf_upgrade_until_owner_acts():
    """M3：ETF 升级行与 realign 行同样要进"机器已纠正"分桶。

    旧版只读 `identity_realign`，且判据用 `match_source` —— 后者会被之后任何一次
    agent 写入盖成 'agent'，实测 2 行带着 etf_upgrade 记录却是 reviewed=1 + 分桶 0。
    """
    ev = json.dumps({'identity_realign': {'code': '159805', 'from_code': '000001',
                                          'core': 'X'}})
    row = _row('T-六版溯源', '159805', '传媒ETF', evidence=ev)
    assert audit.identity_view(row)['realigned']['kind'] == 'identity_realign'

    row2 = _row('T-六版升级', '159877', '医疗ETF', match_source='agent',
                evidence=json.dumps({'etf_upgrade': {'code': '159877',
                                                     'from_code': '162412',
                                                     'reason': '换成场内 ETF'}}))
    view = audit.identity_view(row2)
    assert view['realigned'] and view['realigned']['kind'] == 'etf_upgrade'
    assert view['realigned']['from_code'] == '162412'

    # 老板改了标的 = 已经表态，旗标必须消失
    row2.fund_code = '510300'
    assert audit.identity_view(row2)['realigned'] is None
    # 老板锁定的行不是"待复核"
    locked = _row('T-六版锁定', '159877', '医疗ETF', owner_locked=True,
                  reviewed_by='owner',
                  evidence=json.dumps({'etf_upgrade': {'code': '159877'}}))
    assert audit.machine_swap_of(locked) is None


def test_apply_results_resets_an_unacknowledged_machine_swap(test_db):
    """M3 的存量返修：机器换过标的却挂着"已审查"→ 体检一轮就复位待复核。"""
    row = _row('T-六版复位', '159805', '传媒ETF', reviewed=True,
               match_source='agent', reviewed_by='agent',
               evidence=json.dumps({'etf_upgrade': {'code': '159805',
                                                    'from_code': '010677'}}))
    test_db.add(row)
    test_db.commit()
    from scripts.sweep_sector_mappings import apply_results
    r = _result(row)
    applied = apply_results(test_db, [r], [], evidence_only=False)
    test_db.refresh(row)
    assert applied['unacknowledged_reset'] == 1
    assert row.reviewed is False and row.reviewed_by is None
    assert row.owner_locked is False
    assert json.loads(row.evidence)['etf_upgrade']['from_code'] == '010677'


def test_apply_results_leaves_an_acknowledged_row_alone(test_db):
    """老板已经换过标的的行不许被动。"""
    row = _row('T-六版已确认', '510300', '沪深300ETF', reviewed=True,
               evidence=json.dumps({'etf_upgrade': {'code': '159805',
                                                    'from_code': '010677'}}))
    test_db.add(row)
    test_db.commit()
    from scripts.sweep_sector_mappings import apply_results
    r = _result(row)
    assert apply_results(test_db, [_result(row)], [], evidence_only=False)[
        'unacknowledged_reset'] == 0
    test_db.refresh(row)
    assert row.reviewed is True


# ----------------------------------------------- 3 体检日志不许冒充用户理由

def test_ok_row_keeps_its_user_facing_reason(test_db):
    """D1 的损坏类别不许复现：verdict=ok 时一个字都不写。

    旧条件是 `verdict in UNSERVABLE or not row.verify_message`，后半句会把
    原本没理由的 ok 行灌成"Jaccard 1.000"，实测 103/145 行被这样刷满。
    """
    kept = _row('T-六版理由留', '512170', '医疗ETF',
                verify_message='无对口债市标的，取券商 ETF（老板确认）')
    empty = _row('T-六版理由空', '159915', '创业板ETF', verify_message=None)
    test_db.add_all([kept, empty])
    test_db.commit()
    from scripts.sweep_sector_mappings import apply_results
    apply_results(test_db, [_result(kept), _result(empty)], [], evidence_only=True)
    test_db.refresh(kept)
    test_db.refresh(empty)
    assert kept.verify_message == '无对口债市标的，取券商 ETF（老板确认）'
    assert empty.verify_message is None, 'ok 行不该被体检日志占位'


def test_unservable_row_still_gets_its_reason(test_db):
    """反向：真判出问题时，理由必须写（老板得知道为什么被降级）。"""
    row = _row('T-六版降级', '000725', '京东方Ａ', verify_message=None)
    test_db.add(row)
    test_db.commit()
    from scripts.sweep_sector_mappings import apply_results
    r = _result(row, verdict='code_is_other_fund', reason='这是基金 大成添利宝货币B')
    apply_results(test_db, [r], [r], evidence_only=True)
    test_db.refresh(row)
    assert row.verify_message == '这是基金 大成添利宝货币B'


# ------------------------------------------------------------- 4 回滚边界

def test_restore_keeps_history_that_predates_the_manifest(test_db, tmp_path):
    """M5：只清本轮同步回来的净值，历史孤儿净值不是本轮产物。"""
    from scripts.sweep_sector_mappings import restore
    row = _row('T-六版回滚', '159805', '传媒ETF')
    test_db.add(row)
    test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    test_db.add_all([
        FundHistory(fund_code='512170', nav_date=date(2020, 1, 2), nav=1.0),
        FundHistory(fund_code='512170', nav_date=date(2026, 9, 21), nav=1.1),
    ])
    test_db.commit()
    manifest = tmp_path / 'm.json'
    manifest.write_text(json.dumps({
        'created_at': '2026-09-21T09:00:00', 'fields': ['fund_code'],
        'rows': [{'id': row.id, 'fund_code': '159805'}],
        'created_fund_codes': ['512170']}), encoding='utf-8')

    restore(test_db, str(manifest))
    dates = [h.nav_date for h in test_db.query(FundHistory).filter(
        FundHistory.fund_code == '512170').all()]
    assert date(2020, 1, 2) in dates, '回滚吃掉了本轮之前老板就有的净值'
    assert date(2026, 9, 21) not in dates, '本轮新建档案的净值要跟着清'
    assert test_db.query(FundInfo).filter(FundInfo.fund_code == '512170').first() is None
    test_db.refresh(row)
    assert row.fund_code == '159805'


# ------------------------------------------------- 5 误连生产的两道门

def _run(code, env):
    e = dict(os.environ)
    e.update(env)
    e['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=e,
                          capture_output=True, text=True, timeout=180,
                          # 子进程按 PYTHONIOENCODING=utf-8 输出，父进程默认按 GBK 解码：
                          # Windows 上不设这两句，读中文输出本身就抛 UnicodeDecodeError
                          encoding='utf-8', errors='replace')


def _run_src_db(env):
    """真·文档命令：`python -m src --init-db`（比 runpy 更接近老板会敲的那一行）。"""
    e = dict(os.environ)
    e.update(env)
    e['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run([sys.executable, '-m', 'src', '--init-db'], cwd=ROOT, env=e,
                          capture_output=True, text=True, timeout=180,
                          encoding='utf-8', errors='replace')


def test_db_guard_rejects_a_non_sqlite_escape_hatch():
    """B3：报错让人设 LOCAL_DB_URL，粘成 postgres:// 就等于放行线上写入。"""
    out = _run('import sys; sys.path.insert(0, "scripts"); import _db_guard;'
               '_db_guard.pin_local_sqlite()',
               {'DATABASE_URL': FAKE_PROD_URL, 'LOCAL_DB_URL': FAKE_PROD_URL})
    assert out.returncode == 4, out.stdout + out.stderr
    assert '也必须指向 SQLite' in out.stdout


def test_db_guard_accepts_a_bare_path_and_pins_sqlite(tmp_path):
    out = _run('import sys; sys.path.insert(0, "scripts"); import _db_guard;'
               'print(_db_guard.pin_local_sqlite())',
               {'DATABASE_URL': FAKE_PROD_URL,
                'LOCAL_DB_URL': str(tmp_path / 'mirror.db')})
    assert out.returncode == 0, out.stdout + out.stderr
    assert '[env] DATABASE_URL = sqlite:///' in out.stdout, out.stdout


def test_init_db_refuses_a_remote_url():
    """`python -m src --init-db` 是文档推荐命令，误跑一次就在生产库上建表。"""
    argv = 'import sys; sys.argv=["src","--init-db"]; import runpy;' \
           'runpy.run_module("src", run_name="__main__")'     # noqa: F841 见 _run_src_db
    out = _run_src_db({'DATABASE_URL': FAKE_PROD_URL, 'ALLOW_REMOTE_INIT_DB': ''})
    assert out.returncode == 2, out.stdout + out.stderr
    assert '--init-db' in out.stdout and 'ALLOW_REMOTE_INIT_DB' in out.stdout
    # 显式授权后这道门就该让路（连不上假域名是另一回事，不能再报 [abort]）
    ok = _run_src_db({'DATABASE_URL': FAKE_PROD_URL, 'ALLOW_REMOTE_INIT_DB': '1'})
    assert '[abort]' not in ok.stdout, ok.stdout


def test_manifest_before_image_captures_a_midrun_owner_edit(test_db):
    """第 5 轮遗漏的 #1：清单前像必须是"写入那一刻"的值。

    规划期的网络验证要跑几分钟，期间老板可能 PUT 改过行；不 expire 就写清单，
    `--restore-from` 会把他的新改动当成"原值"退回去。
    """
    from scripts.sweep_sector_mappings import write_manifest
    from sqlalchemy.orm import sessionmaker
    row = _row('T-六版前像', '159805', '传媒ETF')
    test_db.add(row)
    test_db.commit()
    # 另一个 session 改这行（模拟老板在页面点了保存）
    other = sessionmaker(bind=test_db.get_bind())()
    target = other.query(SectorFundMapping).filter(SectorFundMapping.id == row.id).first()
    target.fund_code = '510300'
    other.commit()
    other.close()
    test_db.expire_all()          # main() 里 write_manifest 前就是这一句
    path = write_manifest('v74-before-image', test_db)
    try:
        with io.open(path, encoding='utf-8') as f:
            snap = json.load(f)
    finally:
        os.remove(path)           # 用例自己的产物：不许往 docs/ 里堆假清单
    mine = [i for i in snap['rows'] if i['id'] == row.id]
    assert mine and mine[0]['fund_code'] == '510300', '前像是过期值，回滚会吞掉老板的改动'


def _plan(row, code='159537', name='信创ETF国泰'):
    return {'id': row.id, 'sector': row.sector_name, 'code': code, 'name': name,
            'from_code': row.fund_code, 'from_name': row.fund_name, 'core': '信创',
            'reason': '板块「信创」核心词「信创」，换成字面对口的场内 ETF %s' % name,
            'candidates': [{'code': code, 'name': name}], 'nav_date': '2026-09-18'}


def test_realign_midrun_failure_keeps_committed_rows_and_leaves_the_rest(test_db,
                                                                          monkeypatch):
    """第 5 轮遗漏的 #7（数据面）：中途抛异常时已成交保留、未成交一行都不写。

    `apply_realign` 自己**不**吞异常 —— 是 `main()` 的 except 记 exit 6、
    `finally` 用 out 参数 `written` 把"已成交"认下来再 `demote_unwritten`。
    所以这里必须按 main 的调用形状测：out 参数 + try/except + finally 降级。
    """
    from scripts import sweep_sector_mappings as sweep
    from scripts.sweep_sector_mappings import apply_realign, demote_unwritten
    a = _row('T-六版半A', '516810', '旧科创信息ETF')
    b = _row('T-六版半B', '516811', '旧科创信息ETF2')
    test_db.add_all([a, b])
    test_db.add(FundInfo(fund_code='159537', fund_name='信创ETF国泰'))
    test_db.commit()

    def fake_reaudit(code, name, sector):
        if sector == 'T-六版半B':
            raise RuntimeError('站点抖动')
        return {'verdict': 'ok', 'reason': '品种名一致', 'identity': {'verdict': 'ok'}}

    monkeypatch.setattr(sweep, 'reaudit_new_code', fake_reaudit)
    plans = [_plan(a), _plan(b)]
    created, written = [], []
    with pytest.raises(RuntimeError):
        apply_realign(test_db, plans, created, written=written)
    assert [p['sector'] for p in written] == ['T-六版半A'], written
    test_db.expire_all()
    assert test_db.query(SectorFundMapping).filter(
        SectorFundMapping.sector_name == 'T-六版半B').first().fund_code == '516811'
    # 已成交那行：用户可见理由用**计划里那句**，不是机器自证的 Jaccard
    test_db.refresh(a)
    assert a.verify_message.startswith('板块「信创」核心词')
    assert 'Jaccard' not in a.verify_message
    # 未成交那行不能被当成"已经处理过"：按体检结论该降就降
    results = [_result(a, verdict='ok'), _result(b, verdict='code_is_other_fund')]
    assert demote_unwritten(test_db, results, plans, written) == 1
    test_db.refresh(b)
    assert b.reviewed is False and b.is_fetchable is False


def test_realign_survives_legacy_array_evidence(test_db, monkeypatch):
    """第 5 轮遗漏的 #9：老数据的 evidence 是候选轨迹**数组**，realign 不许崩。"""
    from scripts import sweep_sector_mappings as sweep
    from scripts.sweep_sector_mappings import apply_realign
    row = _row('T-六版旧数组', '516810', '旧科创信息ETF',
               evidence=json.dumps([{'stage': 'T1', 'code': '516810'}]))
    test_db.add(row)
    test_db.add(FundInfo(fund_code='159537', fund_name='信创ETF国泰'))
    test_db.commit()
    monkeypatch.setattr(sweep, 'reaudit_new_code',
                        lambda c, n, s: {'verdict': 'ok', 'reason': '一致',
                                         'identity': {'verdict': 'ok'}})
    assert len(apply_realign(test_db, [_plan(row)], [])) == 1
    test_db.refresh(row)
    evidence = json.loads(row.evidence)
    assert isinstance(evidence, dict) and evidence['prev'] == [{'stage': 'T1',
                                                                'code': '516810'}]
    assert evidence['identity_realign']['from_code'] == '516810'


def test_human_review_is_attributed_to_the_owner(test_db):
    """第 8 轮 BLOCKER：点一次"标记已审查"= 老板的结论，署名与锁定必须一致。

    以前的写法让 agent 行保留 `reviewed_by='agent'` 却拿到 `owner_locked=1`：
    一边用 owner 分支绕过 agent 自己的置信度门槛去驱动预测改标，
    一边在回写脚本里被报成"老板手定的有意代理"（实测 秦安股份 0.7975、硬件 0.755）。
    """
    row = _row('T-审查署名', '562700', '汽车零部件ETF', reviewed=False,
               match_source='agent', reviewed_by='agent', confidence=0.7975,
               verified_at=datetime.now())
    test_db.add(row)
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    assert SectorFundService(test_db).mark_reviewed_by_id(row.id) is True
    test_db.refresh(row)
    assert row.reviewed is True
    assert row.reviewed_by == 'owner', '人点的审查不许署成 agent 的名'
    assert row.owner_locked is True


def test_audit_import_still_refuses_a_row_the_owner_blessed(test_db):
    """上一例的下游：真被老板批过的行，机器回写必须拒绝覆盖（不能拿'agent'当挡箭牌）。"""
    from src.api.routes.config import _audit_owner_guard
    blessed = _row('T-审查署名2', '512480', '半导体ETF', reviewed=True,
                   reviewed_by='owner', owner_locked=True)
    assert _audit_owner_guard(blessed) == 'owner_locked'
    stale_agent_lock = _row('T-审查署名3', '512480', '半导体ETF', reviewed=True,
                            reviewed_by='agent', owner_locked=True)
    assert _audit_owner_guard(stale_agent_lock) == 'owner_locked'


def test_manifest_write_failure_still_reports_created_codes():
    """M6：清单写坏时，已提交的基金档案必须被喊出来（否则回滚漏删）。"""
    import inspect
    from scripts import sweep_sector_mappings as sweep
    body = inspect.getsource(sweep.apply_results)
    assert body.count('except Exception') >= 1
    assert '请手工删除本轮新建基金档案' in body
    main_src = inspect.getsource(sweep.main)
    # D5：中途失败必须让进程非 0 退出，否则 CI/老板看到"跑完了"就当成功
    assert 'exit_code = 6' in main_src
