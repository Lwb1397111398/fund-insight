# -*- coding: utf-8 -*-
"""板块映射身份体检（S4a）的安全网。

这里锁死的是"6 位代码在股票域和基金域会撞码"这一件事：
000725 在基金域是「大成添利宝货币B」，映射表里存的却是「京东方Ａ」（深市股票）。
旧口径"能抓到净值"对这类行永远返回通过，于是老板看到的结论是
"京东方看涨的预测被一只货币基金验证了"。所以判定只能用基金域自证，
而"没查到"与"查到了且不对"必须严格区分——后者才允许降级。
"""
import io
import json
import os
from datetime import datetime

import pytest

from src.fund.fund_api import jaccard_name, normalize_for_identity
from src.models.database import FundInfo, SectorFundMapping
from src.services import sector_identity_audit as audit

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), '..', 'fixtures',
                       'name_identity_golden.json')


@pytest.fixture(autouse=True)
def _no_roster_download(monkeypatch):
    """单测零网络：`_find_fund_twin` 会回落去拉 3.1MB 的基金域名册。"""
    monkeypatch.setattr(audit.fund_api, 'load_fund_roster',
                        lambda refresh=False: {'by_code': {}, 'codes_by_name': {},
                                               'size': 0})
    yield


@pytest.fixture(autouse=True)
def _clean_service_cache():
    """`SectorFundService._cache` 是类属性：不清就会把上一个测试的映射漏给下一个。"""
    from src.services.sector_fund_service import SectorFundService
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False
    yield
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False


def _domain(status='ok', name=None, source='roster'):
    return lambda code: {'status': status, 'name': name, 'fund_type': None,
                         'source': source}


def _hits(*triples):
    """triples: (code, name, is_fund)"""
    return lambda keyword: list(triples)


def _roster(present):
    return lambda name: present


def _arbitrate(code, stored, sector, status='ok', official=None, hits=(), roster=None):
    return audit.arbitrate_mapping(
        code, stored, sector,
        _domain=_domain(status, official),
        _hits=_hits(*hits),
        _name_in_roster=_roster(roster if roster is not None else False))


# ---------- 归一化与阈值 ----------

def test_normalization_golden():
    """钉死归一化与相似度：阈值附近那几行（0.40/0.4667）全靠这两个函数不出意外。"""
    golden = json.load(io.open(GOLDEN_PATH, encoding='utf-8'))
    for raw, expected in golden['normalize'].items():
        assert normalize_for_identity(raw) == expected, raw
    for pair, expected in golden['jaccard'].items():
        a, b = pair.split('<->')
        assert jaccard_name(a, b) == pytest.approx(expected), pair
    bound = golden['threshold_boundary']['IDENTITY_OK_JACCARD']
    assert bound == audit.IDENTITY_OK_JACCARD
    for pair in golden['threshold_boundary']['must_stay_ok']:
        a, b = pair.split('<->')
        assert jaccard_name(a, b) >= bound, '正确行被阈值踢出：%s' % pair
    for pair in golden['threshold_boundary']['must_stay_below']:
        a, b = pair.split('<->')
        assert jaccard_name(a, b) < bound, '错误行越过阈值：%s' % pair


def test_owner_proxy_similarity_tripwire():
    """老板手定的两只代理不能被身份判据动到——调阈值或改归一化都会撞这条红灯。"""
    assert jaccard_name('卫星ETF永赢', '永赢国证商用卫星通信产业ETF') \
        >= audit.IDENTITY_OK_JACCARD, 'SpaceX→159206 会被身份判定误杀'
    from scripts.sweep_sector_mappings import pick_demotions
    bad = {'verdict': audit.VERDICT_NOT_A_FUND, 'reviewed': True}
    assert [r['id'] for r in pick_demotions([dict(bad, id=99, owner_row=False)])] == [99]
    assert pick_demotions([dict(bad, id=147, owner_row=True)]) == []   # 老板手定不许动
    assert pick_demotions([dict(bad, id=98, reviewed=False)]) == []    # 本来就没审查
    assert pick_demotions([{'verdict': audit.VERDICT_UNKNOWN,
                            'reviewed': True, 'id': 97}]) == []        # 没结论不降级


# ---------- 品种标签 ----------

def test_kind_labels_v2():
    """旧正则 `15[0-9]{3}` 只有 5 位，深市 15xxxx 一条都匹配不上；联接基金也不能算 ETF。"""
    from src.services.sector_fund_agent import fund_kind_label, is_etf, is_exchange_listed

    assert is_exchange_listed('159995') and is_exchange_listed('512170')
    assert is_etf('512170', '华宝中证医疗ETF')
    assert fund_kind_label('159206', '富国中证卫星产业') == 'exchange_other'
    assert fund_kind_label('159995', '华夏国证半导体芯片ETF') == 'etf'
    assert fund_kind_label('162412', '华宝医疗ETF联接A') == 'feeder'
    assert fund_kind_label('010677', '工银传媒指数C') == 'otc'
    assert fund_kind_label('511260', '十年国债ETF国泰') == 'etf'
    from src.services.sector_fund_agent import KIND_PRIOR
    assert KIND_PRIOR['feeder'] < KIND_PRIOR['etf']
    assert KIND_PRIOR['exchange_other'] < KIND_PRIOR['etf']


# ---------- 身份判定顺序 ----------

def test_domain_filter_before_self_hit():
    """股票行的搜索命中码常常**等于**本行码（德明利→001309），先认自命中就等于全放过。"""
    result = _arbitrate('001309', '德明利', '德明利',
                        official='东方红睿逸定开混合',
                        hits=[('001309', '德明利', False)])
    assert result['verdict'] == audit.VERDICT_NOT_A_FUND
    assert '股票' in result['reason']


def test_not_a_fund_needs_roster_confirmation():
    """只靠检索接口判"不是基金"不够：它同一次查询会 10 条↔1 条地抖。"""
    unconfirmed = _arbitrate('001309', '德明利', '德明利',
                             official='东方红睿逸定开混合',
                             hits=[('001309', '德明利', False)], roster=True)
    assert unconfirmed['verdict'] != audit.VERDICT_NOT_A_FUND
    # 检索接口单独说话不算数，但"代码的真实基金与板块零汉字交集"是另一条离线强证据
    assert unconfirmed['verdict'] == audit.VERDICT_CODE_IS_OTHER_FUND
    weak = _arbitrate('001309', '德明利', '东方红睿逸定开混合',
                      official='东方红睿逸定开混合',
                      hits=[('001309', '德明利', False)], roster=True)
    assert weak['verdict'] == audit.VERDICT_UNKNOWN


def test_alias_is_not_wrong_code():
    """同一只基金的正名与别名语序不同（Dice 只有 0.33），必须靠字符集 Jaccard 放过。"""
    result = _arbitrate('512170', '华宝中证医疗ETF', '医疗', official='医疗ETF华宝')
    assert result['verdict'] == audit.VERDICT_OK


def test_qdii_name_with_gupiao_is_still_a_fund():
    """「宏利印度股票(QDII)A」名字里有"股票"两个字，但它是只真 QDII 基金。"""
    result = _arbitrate('006105', '宏利印度股票(QDII)A', '印度股',
                        official='宏利印度股票(QDII)A')
    assert result['verdict'] == audit.VERDICT_OK


def test_code_is_other_fund_catches_unrelated_target():
    """核聚变→「机械ETF富国」：代码确实是基金，但与板块零汉字交集，正是老板说的八竿子打不着。"""
    result = _arbitrate('159886', '富国中证核电ETF159886', '核聚变',
                        official='机械ETF富国')
    assert result['verdict'] == audit.VERDICT_CODE_IS_OTHER_FUND


def test_no_cjk_core_lands_unknown_not_rejected():
    """'6G'/'5GETF' 这类没有中文核心词的板块，零交集毫无信息量，只能给 unknown。"""
    # exact_name_hits 只回"完全同名"的命中，所以这里必须是空列表
    result = _arbitrate('515050', '5GETF', '6G', official='通信ETF华夏', hits=[])
    assert result['verdict'] == audit.VERDICT_UNKNOWN


def test_ownership_floor_blocks_garbage():
    """拓维信息→「招商安拓债券A」这种垃圾命中不能驱动降级（相似度低于 0.60 门槛）。"""
    # 站点说"这个码不存在"，而唯一同名命中是一只不像的基金（jaccard < 0.60）：
    # 不能据此断言 wrong_code，只能 not_fetchable / unknown
    result = _arbitrate('002261', '拓维信息A', '拓维信息A', status='absent',
                        hits=[('004529', '招商安拓债券A', True)], roster=False)
    assert result['verdict'] != audit.VERDICT_WRONG_CODE
    assert result['suggested_code'] is None
    assert result['verdict'] in (audit.VERDICT_NOT_FETCHABLE, audit.VERDICT_UNKNOWN)


def test_wrong_code_offers_the_real_fund():
    """站点说"基金域没有这个码"，但同名基金在别处存在 → wrong_code + 建议码。"""
    result = audit.arbitrate_mapping(
        '999999', '工银瑞信中证中药指数A', '中药',
        _domain=_domain('absent', None),
        _hits=_hits(('004529', '工银瑞信中证中药指数A', True)),
        _name_in_roster=_roster(False))
    assert result['verdict'] == audit.VERDICT_WRONG_CODE
    assert result['suggested_code'] == '004529'


@pytest.mark.parametrize('status,expected', [
    ('error', audit.VERDICT_PROBE_UNAVAILABLE),
])
def test_probe_failure_never_becomes_a_verdict(status, expected):
    """站点抖动的唯一归宿是 probe_unavailable：否则一次网络故障＝一次批量降级。"""
    result = _arbitrate('000725', '京东方Ａ', '京A', status=status)
    assert result['verdict'] == expected
    assert result['verdict'] not in audit.UNSERVABLE_VERDICTS


def test_search_failure_is_not_no_result():
    """检索接口失败返回 None，与"查无同名"的 [] 必须区分（判定的两条腿）。"""
    result = audit.arbitrate_mapping(
        '000725', '京东方Ａ', '京A',
        _domain=_domain('ok', '大成添利宝货币B'),
        _hits=lambda k: None,
        _name_in_roster=_roster(False))
    assert result['verdict'] == audit.VERDICT_PROBE_UNAVAILABLE


# ---------- 相关性（只报告） ----------

def test_relevance_flags_absurd_pairs():
    # 注入"名册里有没有可配对基金"，否则这条测试会打网络
    assert audit.sector_relevance('保险', '纳斯达克ETF华安',
                                  has_alternative=lambda c: True) is False
    assert audit.sector_relevance('保险', '纳斯达克ETF华安',
                                  has_alternative=lambda c: False) is True
    assert audit.sector_relevance('创新药', '创新药ETF银华',
                                  has_alternative=lambda c: True) is True


def test_relevance_ignores_generic_market_and_pure_latin():
    assert audit.sector_relevance('大盘', '沪深300ETF华泰柏瑞') is True
    assert audit.sector_relevance('CPO', '某只无关基金') is True


def test_contains_core_is_substring_by_design():
    """相关性判据刻意用子串：宁可漏报，也不要误报。

    '建信创新驱动' 会子串命中 '信创'（假命中），但要求整词边界会把 '富国中证核电ETF'
    里的 '核电' 也判成没命中——那是更坏的假阴性。旗标只用于报告，不下降级。
    """
    assert audit.contains_core('建信创新驱动混合A', '信创') is True
    assert audit.contains_core('富国中证核电ETF', '核电') is True
    assert audit.contains_core('医疗ETF华宝', '中药') is False
    assert audit.cjk_core('信创') == '信创'
    assert audit.cjk_core('5G概念') == '概念'
    assert audit.cjk_core('SpaceX') == ''


def test_identity_view_does_not_touch_the_network():
    """接口每行都会调 identity_view：这里一旦去打名册，GET /sector-mappings 就会变成
    几百次全表扫描，所以 relevance_low 必须由体检算好写进 evidence。"""
    import socket

    row = SectorFundMapping(sector_name='保险', fund_code='159632',
                            fund_name='纳斯达克ETF华安',
                            evidence=json.dumps({'identity': {
                                'verdict': 'ok', 'relevance_low': True,
                                'official_name': '纳斯达克ETF华安'}}))
    real = socket.socket
    socket.socket = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('identity_view 不该发请求'))
    try:
        view = audit.identity_view(row)
    finally:
        socket.socket = real
    assert view['relevance_low'] is True
    assert view['identity_verdict'] == 'ok'


# ---------- 读路径门禁 ----------

def _mapping(sector, code, name, **kw):
    kwargs = dict(sector_name=sector, fund_code=code, fund_name=name,
                  reviewed=True, is_active=True)
    kwargs.update(kw)
    return SectorFundMapping(**kwargs)


def test_null_fetchable_row_still_serves(test_db):
    """105/119 条历史 reviewed 行 is_fetchable 是 NULL：谓词写错会让它们集体消失。"""
    test_db.add(_mapping('T-测试医疗', '512170', '华宝中证医疗ETF'))
    test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    hit = SectorFundService(test_db).get_fund_by_sector('T-测试医疗')
    assert hit and hit['code'] == '512170'


def test_unservable_row_excluded_from_both_branches(test_db):
    """降级必须同时挡住 reviewed 分支与降级分支，还要挡住**缓存**路径。"""
    test_db.add(_mapping('T-测试京东方', '000725', '京东方Ａ', is_fetchable=False))
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    service = SectorFundService(test_db)
    assert service.get_fund_by_sector('T-测试京东方') is None
    service._load_cache()                      # 缓存路径也必须看不到
    assert 'T-测试京东方' not in service._cache
    assert service.get_all_mappings().get('T-测试京东方') is None


def test_match_fund_for_prediction_never_returns_stock(test_db):
    from src.services.prediction_verify_service import PredictionVerifyService
    test_db.add(_mapping('T-测试制冷剂', '000530', '冰山冷热', is_fetchable=False))
    test_db.add(FundInfo(fund_code='000530', fund_name='冰山冷热'))
    test_db.commit()
    service = PredictionVerifyService(test_db)
    prediction = type('P', (), {'fund_code': None, 'fund_name': None,
                                'sector': 'T-测试制冷剂', 'sector_type': None})()
    code, name = service.match_fund_for_prediction(prediction)
    assert code != '000530'


def test_mark_reviewed_refuses_unservable_even_with_owner_confirm(test_db):
    """一键"全部标记已审查"不能复活股票行——证据齐备恰恰是体检自己写上去的。"""
    row = _mapping('T-测试紫紫', '000938', '紫光股份', is_fetchable=False,
                   reviewed=False, match_source='sweep', verified_at=datetime.now())
    test_db.add(row)
    test_db.add(FundInfo(fund_code='000938', fund_name='紫光股份'))
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    service = SectorFundService(test_db)
    assert service.mark_reviewed_by_id(row.id, True, owner_confirm=True) is False
    assert service.batch_mark_reviewed([row.id], True, owner_confirm=True) == 0
    assert service._last_batch_review_rejected == ['T-测试紫紫']
    test_db.refresh(row)
    assert row.reviewed is False


def test_sweep_does_not_arm_the_review_gate(test_db):
    """体检写的是 evidence/verified_at/is_fetchable；一旦顺手写 match_source，
    "证据齐备=可以审查"的门禁就被集体点亮，等于门禁不存在。"""
    from scripts.sweep_sector_mappings import apply_results

    row = _mapping('T-测试中药', '004529', '工银瑞信中证中药指数A', reviewed=True,
                   is_fetchable=None, match_source=None, verified_at=None)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='004529', fund_name='长盛盛通纯债C'))
    test_db.commit()
    result = _arbitrate('004529', '工银瑞信中证中药指数A', '中药',
                        official='长盛盛通纯债C')
    assert result['verdict'] == audit.VERDICT_CODE_IS_OTHER_FUND
    result.update(id=row.id, sector=row.sector_name, reviewed=True,
                  owner_locked=False, owner_row=False)
    apply_results(test_db, [result], [result], evidence_only=False)
    test_db.refresh(row)
    assert row.reviewed is False
    assert row.match_source is None          # 绝不写 match_source
    assert row.is_fetchable is False
    stored = json.loads(row.evidence)['identity']
    assert stored['verdict'] == audit.VERDICT_CODE_IS_OTHER_FUND
    assert '代码' in stored['reason'] and '004529' in stored['reason']


def test_evidence_only_keeps_reviewed(test_db):
    from scripts.sweep_sector_mappings import apply_results

    row = _mapping('T-测试只写证据', '000970', '中科三环', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='000970', fund_name='中科三环'))
    test_db.commit()
    result = _arbitrate('000970', '中科三环', '中科三环',
                        official='东方红睿元混合', hits=[('000970', '中科三环', False)])
    result.update(id=row.id, sector=row.sector_name, reviewed=True,
                  owner_locked=False, owner_row=False)
    apply_results(test_db, [result], [result], evidence_only=True)
    test_db.refresh(row)
    assert row.reviewed is True              # 只写证据列，不动审查状态
    assert row.is_fetchable is False


def test_restore_from_manifest_roundtrip(test_db):
    from scripts.sweep_sector_mappings import MANIFEST_FIELDS, write_manifest

    row = _mapping('T-测试回滚', '002273', '水晶光电', reviewed=True,
                   match_source='agent', confidence=0.91)
    test_db.add(row)
    test_db.commit()
    path = write_manifest('pytest', test_db, [row])
    data = json.load(io.open(path, encoding='utf-8'))
    assert set(data['fields']) == set(MANIFEST_FIELDS)
    row.reviewed = False
    row.is_fetchable = False
    row.confidence = None
    test_db.commit()

    from scripts.sweep_sector_mappings import restore
    restore(test_db, path)
    test_db.refresh(row)
    assert row.reviewed is True and row.is_fetchable is None
    assert row.confidence == 0.91 and row.match_source == 'agent'
    os.remove(path)


def test_writer_mirrors_verdict_into_the_column(test_db):
    """列是"可服务"的**唯一可查询真值**，verdict 是解释。

    SQL 侧读者（`get_fund_by_sector`、`match_fund_for_prediction`、批量映射加载）
    过滤不了 JSON 里的 verdict，所以写入方必须保证不变量：
    **verdict 属不可服务集合 ⇒ is_fetchable 必为 False**。
    这条测试锁的就是这个镜像关系，它一断，SQL 读者立刻全部漏防。
    """
    from scripts.sweep_sector_mappings import apply_results
    from src.services.sector_identity_audit import UNSERVABLE_VERDICTS

    rows = []
    for verdict in sorted(UNSERVABLE_VERDICTS):
        rows.append(_mapping('T-测试镜像-%s' % verdict[:8], '510300', '沪深300ETF',
                             evidence=json.dumps({'identity': {'verdict': verdict}})))
    test_db.add_all(rows)
    test_db.commit()
    results = [{'id': r.id, 'verdict': json.loads(r.evidence)['identity']['verdict'],
                'reviewed': True, 'owner_row': False, 'reason': 'x',
                'official_name': None, 'jaccard': 0.0,
                'suggested_code': None, 'suggested_name': None,
                'suggestions': [], 'relevance_low': False} for r in rows]
    apply_results(test_db, results, results, evidence_only=False)
    for r in rows:
        test_db.refresh(r)
        assert r.is_fetchable is False, '镜像断裂：%s' % r.sector_name
        assert r.reviewed is False


@pytest.mark.parametrize('truth_source', ['column', 'verdict_only'])
def test_python_side_readers_block_both_truth_sources(test_db, truth_source):
    """行级判据 `row_unservable` 的两个事实源都要让 Python 侧读者闭嘴。

    旧版测试写成"源码里出现过 servable_predicate 字样"——未使用的 import 也能通过，
    根本挡不住漏改的读者。
    """
    from src.services.sector_fund_service import SectorFundService
    from src.services.sector_fund_agent import SectorFundAgent

    evidence = None
    is_fetchable = None
    if truth_source == 'column':
        is_fetchable = False
    else:
        evidence = json.dumps({'identity': {'verdict': audit.VERDICT_NOT_A_FUND,
                                            'reason': '股票'}})
    row = _mapping('T-测试全读者', '000725', '京东方Ａ',
                   reviewed=True, is_fetchable=is_fetchable, evidence=evidence)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    audit.invalidate_denied_cache()

    service = SectorFundService(test_db)
    service._cache_loaded = False
    service._cache = {}
    assert 'T-测试全读者' not in service.get_all_mappings()
    candidates = SectorFundAgent().tier0('T-测试全读者', db=test_db)
    assert '000725' not in [c.code for c in candidates]
    assert audit.row_unservable(row) is True
    assert audit.identity_view(row)['servable'] is False


def test_sql_readers_block_demoted_rows_in_the_retag_writer(test_db):
    """真正会写预测的那条通道（`sync_sector_mappings` 的映射源）必须有行为测试。

    之前只有"源码里出现过谓词函数名"这种检查，未使用的 import 也算通过。
    """
    from datetime import date
    from src.models.database import Blogger, Post, Prediction
    from src.services.prediction_maintenance_service import PredictionMaintenanceService

    blogger = Blogger(name='身份测试博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, content='正文', post_date=date(2026, 6, 1))
    test_db.add(post)
    test_db.flush()
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code='999999',
        fund_name='原标的', sector='T-测试改标读者', prediction_type='up',
        prediction_content='上涨', prediction_date=date(2026, 6, 1),
        prediction_period='1周', target_date=date(2026, 6, 8))
    test_db.add(prediction)
    test_db.add(_mapping('T-测试改标读者', '000725', '京东方Ａ',
                         reviewed=True, is_fetchable=False))
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.add(FundInfo(fund_code='999999', fund_name='原标的'))
    test_db.commit()
    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    hit = [d for d in result['details'] if d['prediction_id'] == prediction.id]
    assert hit == [], '被体检否掉的映射不该再驱动改标：%r' % hit


def test_name_only_edit_cannot_revive_a_demoted_row(test_db):
    """降级理由的钥匙是代码：只改名字就把股票"洗白"成已审查是不安全的。"""
    row = _mapping('T-测试改名复活', '000725', '京东方Ａ',
                   reviewed=False, is_fetchable=False,
                   evidence=json.dumps({'identity': {'verdict': audit.VERDICT_NOT_A_FUND}}))
    test_db.add(row)
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    from src.services.sector_fund_service import SectorFundService
    service = SectorFundService(test_db)
    assert service.update_mapping(row.id, fund_name='京东方A', mark_reviewed=True) is None
    test_db.refresh(row)
    assert row.reviewed is False
    assert service.get_fund_by_sector('T-测试改名复活') is None


def test_static_map_is_withheld_when_deny_set_unavailable(monkeypatch):
    """拒绝集取不到时"哪些标的被否"不可知；放行 122 条内置映射等于体检白做且是静默的。"""
    from src.analyzer import llm_analyzer
    from src.services import sector_identity_audit as aud
    monkeypatch.setattr(aud, 'denied_map_available', lambda: False)
    assert llm_analyzer.LLMAnalyzer._audited_static_map() == {}


def test_agent_refuses_to_whiten_a_demoted_row(db_session):
    """agent 只有净值判据，没有身份判据：同码重判不能把"不可服务"洗白。"""
    from src.services.sector_fund_agent import (
        FundCandidate, SectorDecision, apply_decision)
    row = SectorFundMapping(sector_name='T-测试agent洗白', fund_code='000725',
                            fund_name='京东方Ａ', reviewed=True, is_active=True,
                            is_fetchable=False, verify_message='是股票')
    db_session.add(row)
    db_session.add(FundInfo(fund_code='000725', fund_name='大成添利宝货币B'))
    db_session.commit()
    decision = SectorDecision(
        sector='T-测试agent洗白', status='matched', confidence=0.95,
        chosen=FundCandidate(code='000725', name='京东方Ａ', official_name='京东方Ａ',
                             t3_suitable=True, t3_score=99, confidence=0.95,
                             verify={'is_strict_ok': True}))
    result = apply_decision(db_session, decision, mapping_id=row.id)
    assert result['applied'] is False
    assert result['status'] == 'audit_rejected'
    db_session.refresh(row)
    assert row.is_fetchable is False


def test_rejected_codes_never_falls_back_to_the_process_cache(test_db, monkeypatch):
    """拒绝集必须查调用方那个库：进程级 TTL 缓存指向另一个数据库时会静默失效。

    归一化分支以前漏传 db，两处查的是两个库——这条测试用 strict 包装把这类
    "忘记带 session"的改动直接变成红灯。
    """
    def strict(refresh=False, db=None):
        if db is None:
            raise AssertionError('拒绝集查询没带 session：会读到另一个数据库')
        return real(refresh=refresh, db=db)

    real = audit.denied_code_map
    monkeypatch.setattr(audit, 'denied_code_map', strict)
    # 行按**归一后的键**存，查询用归一前的名字 → 必须走归一化那一支
    test_db.add(_mapping('半导体', '000725', '京东方Ａ',
                         reviewed=False, is_fetchable=False))
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    assert audit.rejected_codes('半导体板块', db=test_db) == {'000725'}


def test_update_mapping_does_not_write_itself_into_the_cache(test_db):
    """写库出口只能 refresh_cache()：手写 dict 会让不可服务的行绕过所有过滤。"""
    from src.services.sector_fund_service import SectorFundService
    row = _mapping('T-测试缓存毒化', '512170', '华宝中证医疗ETF', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    test_db.commit()
    service = SectorFundService(test_db)
    service.refresh_cache()
    row.is_fetchable = False          # 模拟保存之后体检才把它判死
    test_db.commit()

    service.update_mapping(row.id, fund_name='医疗ETF华宝', mark_reviewed=False)
    assert 'T-测试缓存毒化' not in SectorFundService._cache


def test_repair_resets_flags_that_have_no_verdict(test_db):
    """旧语义写下的 `is_fetchable=False`（没有身份结论）必须能复位。

    那一列现在的语义是"可服务"；遗留 False 会让行在所有 SQL 读者里黑洞，
    而页面上没有任何理由可以解释——老板只会以为功能坏了。
    """
    from scripts.sweep_sector_mappings import repair_legacy_fetchable_flag
    legacy = _mapping('T-测试遗留标记', '512170', '华宝中证医疗ETF',
                      reviewed=True, is_fetchable=False,
                      evidence=json.dumps([{'stage': 'T3'}]))
    audited = _mapping('T-测试已体检标记', '000725', '京东方Ａ',
                       reviewed=False, is_fetchable=False,
                       evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    test_db.add_all([legacy, audited])
    test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    assert repair_legacy_fetchable_flag(test_db) == 1
    test_db.commit()
    test_db.refresh(legacy)
    test_db.refresh(audited)
    assert legacy.is_fetchable is None
    assert audited.is_fetchable is False


def test_retag_writer_refuses_unservable_mapping(test_db):
    """改标源也要认镜像：agent 换标的会把列清成 NULL，只查列等于没防住。"""
    from src.services.prediction_maintenance_service import PredictionMaintenanceService
    row = _mapping('T-测试改标镜像', '000725', '京东方Ａ', reviewed=True,
                   is_fetchable=None,
                   evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    test_db.add(row)
    test_db.add(FundInfo(fund_code='000725', fund_name='京东方Ａ'))
    test_db.commit()
    eligible = PredictionMaintenanceService._mapping_eligible(row, 0.0)
    assert eligible is False
    row.is_fetchable = False
    assert PredictionMaintenanceService._mapping_eligible(row, 0.0) is False


def test_sweep_never_blacklists_an_owner_locked_row(test_db):
    """老板锁定的行不能被打上"不可服务"：审查门禁连 owner_confirm 都拒绝，
    一旦盖下去就是永久关在门外，而那是老板自己选的有意代理。"""
    from scripts.sweep_sector_mappings import apply_results
    row = _mapping('债券', '512000', '券商ETF华宝', reviewed=True,
                   owner_locked=True, reviewed_by='owner')
    test_db.add(row)
    test_db.add(FundInfo(fund_code='512000', fund_name='券商ETF华宝'))
    test_db.commit()
    result = _arbitrate('512000', '券商ETF华宝', '债券', official='券商ETF华宝',
                        hits=[])
    result['verdict'] = audit.VERDICT_CODE_IS_OTHER_FUND      # 假设判据想否掉它
    result.update(id=row.id, sector=row.sector_name, reviewed=True,
                  owner_locked=True, owner_row=True)
    apply_results(test_db, [result], [], evidence_only=False)
    test_db.refresh(row)
    assert row.is_fetchable is not False
    assert row.reviewed is True


def test_etf_upgrade_prefers_established_etf(test_db, monkeypatch):
    """老板规则"ETF 最纯粹"落地时不能挑到一只新发的迷你 ETF。

    排名信号用"本系统已存了多少条净值"：名字长度分不出 512170 和 158010，
    但历史深度能；同时新标的必须能建档（fund_code 有外键，实测踩到 IntegrityError）。
    """
    from datetime import date
    from scripts.sweep_sector_mappings import pick_etf_upgrade
    from src.models.database import FundHistory

    monkeypatch.setattr(
        audit.fund_api, 'verify_fund_fetchable',
        lambda code, **kw: {'is_strict_ok': True, 'history_count': 20, 'ok': True})
    row = _mapping('T-测试ETF升级', '162412', '华宝医疗ETF联接A', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='162412', fund_name='华宝医疗ETF联接A'))
    test_db.add(FundInfo(fund_code='512170', fund_name='医疗ETF华宝'))
    for i in range(12):                      # 512170 有历史，158010 没有
        test_db.add(FundHistory(fund_code='512170', nav_date=date(2026, 1, i + 1),
                                nav=1.0 + i))
    test_db.commit()

    verdict = {'official_name': '华宝医疗ETF联接A', 'suggestions': [
        {'code': '158010', 'name': '医疗ETF嘉实'},
        {'code': '512170', 'name': '医疗ETF华宝'}]}
    upgrade = pick_etf_upgrade(test_db, row, verdict)
    assert upgrade['code'] == '512170', upgrade
    assert upgrade['local_history_rows'] == 12
    assert 'ETF' in upgrade['reason'] or '纯粹' in upgrade['reason']


def test_etf_upgrade_skips_owner_and_bad_rows(test_db, monkeypatch):
    """老板手定的代理与已被身份体检否掉的行都不许自动换标的。"""
    from scripts.sweep_sector_mappings import pick_etf_upgrade
    monkeypatch.setattr(audit.fund_api, 'verify_fund_fetchable',
                        lambda code, **kw: {'is_strict_ok': True})
    owner = _mapping('债券', '512000', '券商ETF华宝', reviewed=True,
                     owner_locked=True, reviewed_by='owner')
    bad = _mapping('T-测试ETF升级跳过', '000725', '京东方Ａ', reviewed=False,
                   is_fetchable=False,
                   evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    test_db.add_all([owner, bad])
    test_db.commit()
    assert pick_etf_upgrade(test_db, owner, {'official_name': '券商ETF华宝'}) is None
    assert pick_etf_upgrade(test_db, bad, {'official_name': '大成添利宝货币B'}) is None
