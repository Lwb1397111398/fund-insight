# -*- coding: utf-8 -*-
"""人工改标的的身份证明与溯源清理（第 6 轮评审 M4 / 同文件 minor）。

为什么单独立一个文件：这两件事都只在 `sector_fund_service` 的**写入出口**上生效，
而写入出口是三条路径的汇合点（老板在 UI 里保存、`mark_reviewed` 勾选、批量审查）。
第 6 轮实测的就是第一条：UI 里输一只 600519「贵州茅台」会直接产出
`is_fetchable=NULL(=可服务) + reviewed=1 + owner_locked=1` 的行——
体检与 agent 从此永久碰不到它，而这恰好是本轮唯一一条**没有任何身份证明**的写入。

零网络：`arbitrate_mapping` 是唯一的探针出口，全部用例都注入桩。
"""
import json

import pytest

from src.models.database import FundInfo, SectorFundMapping
from src.services import sector_identity_audit as audit
from src.services.sector_fund_service import (
    MANUAL_UNSERVABLE_MESSAGE, SectorFundService, _drop_identity_evidence)


@pytest.fixture(autouse=True)
def _clean_service_cache():
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False
    yield
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False


def _mapping(db, sector, code, name, **kw):
    if not db.query(FundInfo).filter_by(fund_code=code).first():
        db.add(FundInfo(fund_code=code, fund_name=name))
    row = SectorFundMapping(sector_name=sector, fund_code=code, fund_name=name,
                            reviewed=True, is_active=True,
                            match_source='manual', verified_at=None, **kw)
    db.add(row)
    db.commit()
    return row


def _fake_arbitrate(monkeypatch, verdict, official=None, reason='桩', raises=False):
    """把探针换成固定答案：测的是"拿到答案之后怎么办"，不是探针本身。"""
    if raises:
        def boom(*a, **kw):
            raise RuntimeError('站点炸了')
        monkeypatch.setattr(audit, 'arbitrate_mapping', boom)
        return

    def fake(code, stored, sector='', **kw):
        return {'verdict': verdict, 'code': code, 'stored_name': stored,
                'official_name': official, 'jaccard': 0.0, 'status': 'ok',
                'suggested_code': None, 'suggested_name': None,
                'reason': reason, 'evidence': {'domain_source': 'stub'}}
    monkeypatch.setattr(audit, 'arbitrate_mapping', fake)


# ------------------------------------------------- M4：改标的必须先证身份

@pytest.mark.parametrize('verdict', ['not_a_fund', 'code_is_other_fund',
                                     'wrong_code', 'not_fetchable'])
def test_stock_like_code_is_not_allowed_to_become_immune(test_db, monkeypatch, verdict):
    """四类"查到了且不对"的结论：不置审查、不锁老板，并当场写成不可服务。

    **这一支只覆盖"那个代码在 `fund_info` 里已经有档案"的情形**（第 50 轮自查出的那条分叉）：
    `sector_fund_mapping.fund_code` 有外键指向 `fund_info`，档案被身份门拒建时代码**不能**落库
    ⇒ 那种形状是"整笔拒改"，判据在 `test_fund_info_archive_gate.py`（那边的夹具自己开了
    `PRAGMA foreign_keys`，本文件的 `test_db` 没开 —— 正因为没开，上一批"悬空写"才能在这里全绿）。
    这里先造一只**已经存在的垃圾档案**（真基金码挂着股票名那一批的形状），考察的是
    "指控落库时这一行必须被降级、不许带免疫、读路径必须查不到它"。
    """
    _fake_arbitrate(monkeypatch, verdict, official='大成添利宝货币B')
    row = _mapping(test_db, 'T-手改股票', '000725', '京东方Ａ')
    test_db.add(FundInfo(fund_code='600519', fund_name='贵州茅台'))
    test_db.commit()
    service = SectorFundService(test_db)
    result = service.update_mapping(row.id, fund_code='600519', fund_name='贵州茅台')

    test_db.refresh(row)
    assert result is not None and result['reviewed'] is False
    assert row.fund_code == '600519', '老板填的代码要落库，不能被静默丢弃'
    assert row.is_fetchable is False
    assert row.reviewed is False and (row.reviewed_by in (None, False))
    # 不撤锁的话 `row_unservable()` 的 owner 例外会把上面的降级判断整个吃掉
    assert row.owner_locked is False
    assert MANUAL_UNSERVABLE_MESSAGE in (row.verify_message or '')
    assert '大成添利宝货币B' in row.verify_message
    payload = json.loads(row.evidence or '{}')
    assert payload['identity']['verdict'] == verdict
    assert payload['identity']['source'] == 'manual_edit'
    # 所有读路径都不得再把这个板块交给这只"股票"
    assert service.get_fund_by_sector('T-手改股票') is None


@pytest.mark.parametrize('verdict', ['unknown', 'probe_unavailable'])
def test_no_conclusion_never_accuses(test_db, monkeypatch, verdict):
    """"没查到"不是反向证据：站点抖动/新基金未入库时一律沿用旧行为。"""
    _fake_arbitrate(monkeypatch, verdict)
    row = _mapping(test_db, 'T-手改无结论', '512170', '医疗ETF华宝')
    result = SectorFundService(test_db).update_mapping(
        row.id, fund_code='588000', fund_name='科创50ETF华夏')

    test_db.refresh(row)
    assert result['reviewed'] is True
    assert row.is_fetchable is None          # NULL = 可服务（从未体检）
    # 第 18 轮 MAJOR-1：探针没结论只说明"不能冤枉老板"，不等于"送他永久免疫"。
    # 审查状态照旧落，署名与锁定要显式 owner_confirm。
    assert row.reviewed is True and not row.owner_locked
    assert row.reviewed_by == 'manual_review'
    assert row.verify_message is None        # 没有结论就没有指控
    assert 'identity' not in json.loads(row.evidence or '{}')


def test_probe_crash_is_swallowed(test_db, monkeypatch):
    """探针抛异常也不能挡住保存：那次写入本身是老板的显式决定。"""
    _fake_arbitrate(monkeypatch, 'error', raises=True)
    row = _mapping(test_db, 'T-探针炸了', '512170', '医疗ETF华宝')
    result = SectorFundService(test_db).update_mapping(
        row.id, fund_code='159995', fund_name='芯片ETF')
    test_db.refresh(row)
    assert result['reviewed'] is True
    assert row.is_fetchable is None and row.reviewed is True
    assert not row.owner_locked, '保存成功 ≠ 体检免疫'


def test_valid_fund_code_behaves_like_before_except_the_lock(test_db, monkeypatch):
    """证明通过 = 旧行为减去"白送的锁定"：可服务、已审查，但免疫要老板自己点。"""
    _fake_arbitrate(monkeypatch, 'ok', official='华夏国证半导体芯片ETF')
    row = _mapping(test_db, 'T-手改正常', '512170', '医疗ETF华宝',
                   evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}),
                   is_fetchable=False)
    result = SectorFundService(test_db).update_mapping(
        row.id, fund_code='159995', fund_name='芯片ETF')

    test_db.refresh(row)
    assert result['reviewed'] is True and result['fund_code'] == '159995'
    assert row.is_fetchable is None, '换标的 = 退回"从未体检"，不许自我背书'
    assert row.reviewed is True and not row.owner_locked
    assert row.verify_message is None
    assert json.loads(row.evidence) == {}, '旧结论讲的是被换掉那只，必须摘干净'


def test_same_code_edit_skips_the_proof(test_db, monkeypatch):
    """没换标的就不许打站：探针只在 `changed` 时调用（UI 只改名字也要能保存）。"""
    calls = []
    _fake_arbitrate(monkeypatch, 'not_a_fund')
    real = audit.arbitrate_mapping

    def counting(*a, **kw):
        calls.append(a)
        return real(*a, **kw)
    monkeypatch.setattr(audit, 'arbitrate_mapping', counting)
    row = _mapping(test_db, 'T-只改名', '512170', '华宝中证医疗ETF')
    SectorFundService(test_db).update_mapping(row.id, fund_code='512170',
                                              fund_name='医疗ETF华宝')
    test_db.refresh(row)
    assert calls == [], '同码保存不该触发身份探针（PUT 每次都会打一次站）'
    assert row.reviewed is True and not row.owner_locked


# ------------------- 第 18 轮 MAJOR-1：署名与豁免只认显式确认

def test_edit_save_cannot_buy_owner_immunity(test_db, monkeypatch):
    """改动前会红：一次普通 PUT 就落 `reviewed_by=owner + owner_locked`，
    而这两样之中任何一样都会让 `row_unservable()` 直接返回 False ⇒ 永久体检免疫。
    """
    _fake_arbitrate(monkeypatch, 'unknown')
    row = _mapping(test_db, 'T-编辑免疫', '512170', '医疗ETF华宝')
    result = SectorFundService(test_db).update_mapping(
        row.id, fund_code='588000', fund_name='科创50ETF华夏')
    test_db.refresh(row)
    assert result['reviewed_by'] != 'owner' and result['owner_locked'] is False
    assert (row.reviewed_by, row.owner_locked) != ('owner', True)
    from src.services.sector_identity_audit import row_unservable
    assert row_unservable(row) is False       # 从未体检，本来就该可服务
    # 但这一行必须**仍然在体检射程内**：锁定为假 ⇒ owner 例外不该生效
    row.reviewed_by, row.owner_locked = None, False
    row.is_fetchable = False
    assert row_unservable(row) is True, '没锁定的行体检要能判下来'


def test_owner_confirm_still_grants_signature_and_lock(test_db, monkeypatch):
    """显式确认 = 逐行审查那一条路的语义，编辑保存也能买到，但要明说要。"""
    _fake_arbitrate(monkeypatch, 'ok', official='华夏科创50ETF')
    row = _mapping(test_db, 'T-确认免疫', '512170', '医疗ETF华宝')
    SectorFundService(test_db).update_mapping(
        row.id, fund_code='588000', fund_name='科创50ETF华夏', owner_confirm=True)
    test_db.refresh(row)
    assert row.reviewed_by == 'owner' and row.owner_locked is True


def test_changing_the_target_revokes_an_inherited_lock(test_db, monkeypatch):
    """老板当年锁的是旧标的：换代码没重新确认，豁免不能继承给新代码。"""
    _fake_arbitrate(monkeypatch, 'unknown')
    row = _mapping(test_db, 'T-继承锁定', '512170', '医疗ETF华宝',
                   reviewed_by='owner', owner_locked=True)
    SectorFundService(test_db).update_mapping(
        row.id, fund_code='588000', fund_name='科创50ETF华夏')
    test_db.refresh(row)
    assert not row.owner_locked and row.reviewed_by != 'owner', \
        '换了标的还留着锁定 = 新代码天生免于体检'
    assert row.reviewed is True               # 审查状态是"看过"，与免疫分开

# ------------------------------- minor：人工改码要把机器换标溯源一起摘掉

def test_manual_code_change_clears_machine_swap_keys():
    """`_drop_identity_evidence` 旧版只摘 `identity`，留下溯源章 = 永久禁止自审。"""
    raw = json.dumps({'identity': {'verdict': 'ok'},
                      'identity_realign': {'code': '159805', 'from_code': '010677'},
                      'etf_upgrade': {'code': '159877', 'from_code': '162412'},
                      'identity_before_upgrade': {'verdict': 'ok'},
                      'prev': [{'stage': 'T1'}],
                      'tiers': [{'stage': 'T3'}]})
    left = json.loads(_drop_identity_evidence(raw))
    assert set(left) == {'prev', 'tiers'}, sorted(left)


def test_machine_swap_flag_survives_nothing_after_a_manual_edit(test_db, monkeypatch):
    """端到端：老板改码 = 那两个章等的确认，之后 agent 必须能重新自审这一行。

    `mark_reviewed=False` 是刻意的：留着老板锁定的话 `machine_swap_of()` 会因为
    owner 例外而返回 None，测出来的就只是那条例外而不是"章被摘掉了"。
    """
    _fake_arbitrate(monkeypatch, 'ok', official='传媒ETF鹏华')
    row = _mapping(test_db, 'T-溯源确认', '159805', '传媒ETF',
                   evidence=json.dumps({'etf_upgrade': {'code': '159805',
                                                        'from_code': '010677'}}))
    assert audit.machine_swap_of(row) is not None
    assert audit.identity_view(row)['realigned']['kind'] == 'etf_upgrade'

    SectorFundService(test_db).update_mapping(row.id, fund_code='512170',
                                              fund_name='医疗ETF华宝',
                                              mark_reviewed=False)
    test_db.refresh(row)
    assert not row.owner_locked and not row.reviewed
    assert audit.machine_swap_of(row) is None, '前端还在指一次更早的机器纠正'
    assert audit.identity_view(row)['realigned'] is None

    # 章没了：这一行不再被 agent 的自批挡板挡住
    from src.services.sector_fund_agent import (
        FundCandidate, SectorDecision, apply_decision)
    if not test_db.query(FundInfo).filter_by(fund_code='512170').first():
        test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    test_db.commit()
    _fake_arbitrate(monkeypatch, 'ok', official='医疗ETF南方')
    decision = SectorDecision(
        sector='T-溯源确认', status='matched', confidence=0.95,
        chosen=FundCandidate(code='512170', name='医疗ETF南方', source='llm',
                             official_name='医疗ETF南方', t3_suitable=True,
                             t3_score=98, confidence=0.95,
                             verify={'is_strict_ok': True}))
    decision.evidence = [{'stage': 'T2', 'code': '512170', 'verdict': 'pass'},
                         {'stage': 'T3', 'code': '512170', 'suitable': True}]
    result = apply_decision(test_db, decision, mapping_id=row.id)
    test_db.refresh(row)
    assert result['applied'] is True and row.reviewed is True
