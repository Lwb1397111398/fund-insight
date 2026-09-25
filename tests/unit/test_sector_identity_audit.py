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


def test_restore_from_manifest_roundtrip(test_db, monkeypatch, tmp_path):
    import scripts.sweep_sector_mappings as sweep
    from scripts.sweep_sector_mappings import MANIFEST_FIELDS, write_manifest

    monkeypatch.setattr(sweep, 'OUT_DIR', str(tmp_path))
    row = _mapping('T-测试回滚', '002273', '水晶光电', reviewed=True,
                   match_source='agent', confidence=0.91)
    test_db.add(row)
    test_db.commit()
    path = write_manifest('pytest', test_db)
    data = json.load(io.open(path, encoding='utf-8'))
    assert set(data['fields']) == set(MANIFEST_FIELDS)
    row.reviewed = False
    row.is_fetchable = False
    row.confidence = None
    test_db.commit()

    from scripts.sweep_sector_mappings import restore
    restore(test_db, path, apply=True)   # 第 46 轮 B-M8：还原默认 dry-run，真写要 apply
    test_db.refresh(row)
    assert row.reviewed is True and row.is_fetchable is None
    assert row.confidence == 0.91 and row.match_source == 'agent'


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
    def strict(refresh=False, db=None, sectors=None):
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

    from datetime import date as _date
    monkeypatch.setattr(
        audit.fund_api, 'verify_fund_fetchable',
        lambda code, **kw: {'is_strict_ok': True, 'history_count': 20, 'ok': True,
                            'nav_date': _date.today().isoformat()})
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
    from datetime import date as _date
    monkeypatch.setattr(audit.fund_api, 'verify_fund_fetchable',
                        lambda code, **kw: {'is_strict_ok': True,
                                            'nav_date': _date.today().isoformat()})
    owner = _mapping('债券', '512000', '券商ETF华宝', reviewed=True,
                     owner_locked=True, reviewed_by='owner')
    bad = _mapping('T-测试ETF升级跳过', '000725', '京东方Ａ', reviewed=False,
                   is_fetchable=False,
                   evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    test_db.add_all([owner, bad])
    test_db.commit()
    assert pick_etf_upgrade(test_db, owner, {'official_name': '券商ETF华宝'}) is None
    assert pick_etf_upgrade(test_db, bad, {'official_name': '大成添利宝货币B'}) is None
    # 存量没有体检证据、但**本轮**判成股票的行也不能换标的（生产第一次跑就是这种库）
    fresh = _mapping('T-升级看新鲜结论', '000938', '紫光股份', reviewed=True)
    test_db.add(fresh)
    test_db.add(FundInfo(fund_code='000938', fund_name='紫光股份'))
    test_db.commit()
    assert pick_etf_upgrade(test_db, fresh, {'official_name': '紫光股份',
                                             'verdict': 'not_a_fund'}) is None
    assert pick_etf_upgrade(test_db, bad, {'official_name': 'x', 'verdict': 'ok',
                                          'suggestions': [{'code': '512000',
                                                           'name': '券商ETF华宝'}]},
                           proxy_codes={'512000'}) is None


# ---------- S4a-v7.3：核心词、候选 ETF 与不相关标的确定性纠正 ----------

ROSTER_FIXTURE = {
    'by_code': {
        '512170': {'name': u'医疗ETF华宝', 'fund_type': u'指数型-股票'},
        '512070': {'name': u'证券保险ETF易方达', 'fund_type': u'指数型-股票'},
        '159537': {'name': u'信创ETF国泰', 'fund_type': u'指数型-股票'},
        '159632': {'name': u'纳斯达克ETF华安', 'fund_type': u'QDII'},
        '516810': {'name': u'农业ETF华夏', 'fund_type': u'指数型-股票'},
        '159811': {'name': u'5GETF博时', 'fund_type': u'指数型-股票'},
        '162412': {'name': u'华宝医疗ETF联接A', 'fund_type': u'指数型-股票'},
        '004529': {'name': u'长盛盛通纯债C', 'fund_type': u'债券型'},
    },
    'codes_by_name': {},
}
CORES_FIXTURE = [u'信创', u'医疗', u'保险', u'证券', u'5G', u'中药', u'市场', u'养殖']


def _fake_roster(monkeypatch):
    monkeypatch.setattr(audit.fund_api, 'load_fund_roster',
                        lambda refresh=False: ROSTER_FIXTURE)


@pytest.mark.parametrize(u'sector,expected', [
    (u'信创', u'信创'), (u'养殖', u'养殖'), (u'中证500', u'中证500'), (u'5G', u'5G'),
    (u'SpaceX概念', u'SpaceX'), (u'全A指数', u'全A'),
    (u'业绩板块', u''), (u'市场', u''), (u'资源股', u''), (u'A股', u''),
    (u'Ai应用', u''), (u'应用', u''),
])
def test_sector_core_extraction_rules(sector, expected):
    u"""核心词是 realign 的准入闸门：泛指词必须出局，拉丁缩写不能被抽坏。"""
    assert audit.sector_core(sector) == expected, sector


def test_core_matcher_golden_matches_implementation():
    u"""145 个板块名的核心词表是**实测基线**，改规则一定会被撞。"""
    path = os.path.join(os.path.dirname(__file__), '..', 'fixtures',
                        'core_matcher_golden.json')
    # 金标随仓库走（scripts/probe_sector_cores.py 生成并同步）：文件不在就是真出事，
    # 静默 skip 会让 CI 上一整条规则跑 0 个用例还全绿
    with io.open(path, encoding='utf-8') as f:
        data = json.load(f)
    for item in data['sectors']:
        assert audit.sector_core(item['sector']) == item['core'], item['sector']
    # 逐行核对规则；条数另有 test_golden_counts_are_pinned 钉死（只跟文件自己比是恒真）


def test_sh_etf_segment_not_dropped(monkeypatch):
    u"""`code[:2] in ('15','5')` 曾把 986 只沪市 ETF 全丢掉（'512170'[:2]=='51'）。"""
    _fake_roster(monkeypatch)
    assert '512170' in {c['code'] for c in audit.etf_candidates(
        u'医疗', all_cores=CORES_FIXTURE, roster=ROSTER_FIXTURE)}
    assert '512070' in {c['code'] for c in audit.etf_candidates(
        u'保险', all_cores=CORES_FIXTURE, roster=ROSTER_FIXTURE)}


def test_feeder_etf_lookalike_excluded(monkeypatch):
    u"""162412 名字带 ETF 却是场外联接基金，不能当"最纯粹"的标的被选中。"""
    _fake_roster(monkeypatch)
    assert '162412' not in {c['code'] for c in audit.etf_candidates(
        u'医疗', all_cores=CORES_FIXTURE, roster=ROSTER_FIXTURE)}


def test_composite_index_can_serve_two_sectors(monkeypatch):
    u"""「证券保险ETF」同时服务 证券 与 保险：等长认领不作废（撞车另有消解）。"""
    _fake_roster(monkeypatch)
    for sector in (u'保险', u'证券'):
        assert '512070' in {c['code'] for c in audit.etf_candidates(
            sector, all_cores=CORES_FIXTURE, roster=ROSTER_FIXTURE)}, sector


def test_no_fabricated_suggestion(monkeypatch):
    u"""名册里没有对口号就不许硬凑：候选必须为空，而不是挑一只沾边的。"""
    _fake_roster(monkeypatch)
    assert audit.etf_candidates(u'中药', all_cores=CORES_FIXTURE,
                                roster=ROSTER_FIXTURE) == []
    assert audit.etf_candidates(u'市场', all_cores=CORES_FIXTURE,
                                roster=ROSTER_FIXTURE) == []


@pytest.mark.parametrize(u'sector,official,expected', [
    (u'5G', u'通信ETF华夏', True),
    (u'AI', u'人工智能ETF易方达', True),
    (u'保险', u'纳斯达克ETF华安', False),
    (u'信创', u'农业ETF华夏', False),
])
def test_relevance_synonyms(sector, official, expected):
    u"""同义写法（5G↔通信）不能判成"字面无关"，真错配（保险↔纳斯达克）必须判出来。"""
    assert audit.sector_relevance(sector, official,
                                  has_alternative=lambda core: True) is expected


def _realign_row(db, sector, code, name, **kw):
    row = _mapping(sector, code, name, reviewed=True, **kw)
    db.add(row)
    db.add(FundInfo(fund_code=code, fund_name=name))
    db.commit()
    return row


def _realign_setup(monkeypatch, test_db, sector=u'信创', code=u'516810',
                   name=u'农业ETF华夏', **kw):
    from datetime import date
    from scripts.sweep_sector_mappings import pick_irrelevant_replacement
    _fake_roster(monkeypatch)
    monkeypatch.setattr(
        audit.fund_api, u'verify_fund_fetchable',
        lambda c, **k: {u'is_strict_ok': True, u'ok': True,
                        u'nav_date': date.today().isoformat(), u'history_count': 30})
    monkeypatch.setattr(
        audit.fund_api, u'get_fund_domain_name',
        lambda c, use_roster=True: {
            u'status': u'ok',
            u'name': ROSTER_FIXTURE[u'by_code'].get(c, {}).get(u'name'),
            u'fund_type': None,
            u'source': u'roster' if use_roster else u'pingzhong'})
    return pick_irrelevant_replacement, _realign_row(test_db, sector, code, name, **kw)


def test_realign_picks_matching_etf_and_resets_conclusion(test_db, monkeypatch):
    u"""换标的必须连带把旧结论全部复位，且不能留"名字还是旧标的"的假证据。"""
    pick, row = _realign_setup(monkeypatch, test_db)
    used = set()
    plan = pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True},
                used=used, cores=CORES_FIXTURE)
    assert plan[u'code'] == '159537', plan
    assert '159537' in used

    from scripts.sweep_sector_mappings import apply_realign
    created = []
    written = apply_realign(test_db, [plan], created)
    assert written and created == ['159537']
    test_db.refresh(row)
    assert (row.fund_code, row.fund_name) == ('159537', u'信创ETF国泰')
    assert row.reviewed is False and row.owner_locked is False
    assert row.confidence is None
    # 审查门要 match_source+verified_at：置 None 会让行"可服务但不可审查"，死在待审查里
    assert row.match_source == u'identity_realign'
    assert row.is_fetchable is True
    evidence = json.loads(row.evidence)
    assert evidence[u'identity'][u'official_name'] == u'信创ETF国泰'
    assert evidence[u'identity'][u'verdict'] == u'ok'
    assert evidence[u'identity_realign'][u'from_code'] == '516810'
    assert evidence[u'identity_realign'][u'core'] == u'信创'


def test_realign_no_double_booking(test_db, monkeypatch):
    u"""两个板块抢同一只 ETF 时，第二个必须换下一只或放弃（占用消解）。"""
    pick, row = _realign_setup(monkeypatch, test_db)
    assert pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True},
                used={'159537'}, cores=CORES_FIXTURE) is None


def test_upgrade_etf_writes_nothing_under_evidence_only(test_db, monkeypatch):
    """--evidence-only 的承诺是"只写证据列"，升级标的也必须停。"""
    from scripts.sweep_sector_mappings import apply_results
    from src.models.database import FundInfo
    row = _mapping('低波', '027137', '广发红利低波指数A', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='027137', fund_name='广发红利低波指数A'))
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': False,
                'official_name': '广发红利低波指数A', 'reason': 'x', 'jaccard': 1.0,
                'suggested_code': None, 'suggested_name': None, 'suggestions': [],
                'relevance_low': False,
                'etf_upgrade': {'code': '512890', 'name': '红利低波ETF',
                                'replaced': '027137 x', 'local_history_rows': 3,
                                'reason': '有场内 ETF 可用'}}]
    apply_results(test_db, results, [], evidence_only=True, upgrade_etf=True)
    test_db.refresh(row)
    assert row.fund_code == '027137', 'evidence-only 竟然换了标的'
    assert json.loads(row.evidence).get('etf_upgrade') is None


def test_upgrade_refuses_proxy_and_taken_codes(test_db, monkeypatch):
    """非空断言：行本身是 ok + 场外基金，只有代理码/占用两条能挡住升级。"""
    from scripts.sweep_sector_mappings import pick_etf_upgrade
    from src.models.database import FundInfo
    monkeypatch.setattr(audit.fund_api, 'verify_fund_fetchable',
                        lambda c, **k: {'is_strict_ok': True, 'ok': True,
                                        'nav_date': __import__('datetime').date.today().isoformat()})
    row = _mapping('医疗', '027137', '华宝医疗ETF联接A', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='027137', fund_name='华宝医疗ETF联接A'))
    test_db.commit()
    verdict_row = {'official_name': '华宝医疗ETF联接A', 'verdict': 'ok', 'suggestions': [
        {'code': '512000', 'name': '券商ETF华宝'}]}
    assert pick_etf_upgrade(test_db, row, verdict_row, proxy_codes={'512000'}) is None
    verdict_row['suggestions'] = [{'code': '512170', 'name': '医疗ETF华宝'}]
    assert pick_etf_upgrade(test_db, row, verdict_row, proxy_codes=set(),
                            taken={'512170'}) is None
    got = pick_etf_upgrade(test_db, row, verdict_row, proxy_codes=set(), taken=set())
    assert got and got['code'] == '512170', got


def test_proxy_deny_codes_fail_closed(monkeypatch):
    """读不到刻意代理拒绝集必须报 None（调用方 exit 8），不能"当没有代理"继续写。"""
    from scripts import sweep_sector_mappings as sweep
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == 'src.services.sector_fund_agent':
            raise ImportError('模拟导入失败')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', boom)
    assert sweep.proxy_deny_codes() is None


def test_upgrade_leaves_no_stale_identity_evidence(test_db):
    """升级写完不许留下"verdict=ok 讲的是旧代码"的假自证（镜像不变量的另一半）。"""
    from scripts.sweep_sector_mappings import apply_results
    from src.models.database import FundInfo
    row = _mapping('传媒', '162412', '华宝医疗ETF联接A', reviewed=True,
                   is_fetchable=True,
                   evidence=json.dumps({'identity': {'verdict': 'ok',
                                                     'official_name': '华宝医疗ETF联接A'}}))
    test_db.add(row)
    test_db.add(FundInfo(fund_code='162412', fund_name='华宝医疗ETF联接A'))
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': False,
                'official_name': '华宝医疗ETF联接A', 'reason': '旧的', 'jaccard': 1.0,
                'suggested_code': None, 'suggested_name': None, 'suggestions': [],
                'relevance_low': False,
                'etf_upgrade': {'code': '159877', 'name': '医疗ETF南方',
                                'replaced': '162412 x', 'local_history_rows': 9,
                                'reason': '有场内 ETF 可用'}}]
    apply_results(test_db, results, [], evidence_only=False, upgrade_etf=True)
    test_db.refresh(row)
    ev = json.loads(row.evidence)
    assert row.fund_code == '159877' and row.is_fetchable is None
    assert 'identity' not in ev and ev.get('identity_before_upgrade'), '旧身份证据没挪走'


@pytest.mark.parametrize(u'kw,verdict_row', [
    ({u'sector': u'市场', u'code': '510300', u'name': u'沪深300ETF'},
     {u'verdict': u'ok', u'relevance_low': True}),
    ({u'sector': u'保险', u'code': '159632', u'name': u'纳斯达克ETF华安',
      u'owner_locked': True, u'reviewed_by': u'owner'},
     {u'verdict': u'ok', u'relevance_low': True}),
    ({u'sector': u'信创', u'code': '000725', u'name': u'京东方Ａ'},
     {u'verdict': u'not_a_fund', u'relevance_low': False}),
])
def test_realign_refuses_generic_owner_and_stock(test_db, monkeypatch, kw, verdict_row):
    u"""泛指核心词、老板手定行、名字本身是股票的行一律不换标的。"""
    pick, row = _realign_setup(monkeypatch, test_db, **kw)
    assert pick(test_db, row, verdict_row, used=set(), cores=CORES_FIXTURE) is None


def test_realign_restore_removes_created_fund(test_db, monkeypatch, tmp_path):
    u"""回滚要把"本轮为映射新建的基金档案"一并删掉，否则它永久挂在基金列表里。"""
    import scripts.sweep_sector_mappings as sweep
    from scripts.sweep_sector_mappings import (apply_realign, finalize_manifest,
                                               restore, write_manifest)
    pick, row = _realign_setup(monkeypatch, test_db)
    # 清单是 `docs/.../sweep-manifest-*.json` 那份**真实回滚依据**的同名产物：用例产物
    # 一律钉到 tmp_path（上一版写进 docs/ 且断言中途失败就不清，本机留下两份假清单）
    monkeypatch.setattr(sweep, 'OUT_DIR', str(tmp_path))
    path = write_manifest(u'pytest-realign', test_db)
    assert os.path.normcase(os.path.dirname(path)) == os.path.normcase(str(tmp_path)), \
        '清单没落进临时目录 ⇒ 用例产物又堆进 docs/ 了：%s' % path
    plan = pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True},
                used=set(), cores=CORES_FIXTURE)
    created = []
    apply_realign(test_db, [plan], created)
    assert finalize_manifest(path, created) == ['159537']
    assert test_db.query(FundInfo).filter(FundInfo.fund_code == '159537').first()

    restore(test_db, path, apply=True)   # 第 46 轮 B-M8：还原默认 dry-run
    test_db.refresh(row)
    assert row.fund_code == '516810' and row.reviewed is True
    assert test_db.query(FundInfo).filter(FundInfo.fund_code == '159537').first() is None


def test_realign_fixes_a_demoted_row(test_db, monkeypatch):
    u"""code_is_other_fund 的坏行（存名不是那个码）也要能换标的，换完必须重新仲裁。

    v7.1 准入表第 3 行。只测 ok+relevance_low 等于把"把坏行修好"这条主路径漏掉。
    """
    from scripts.sweep_sector_mappings import apply_realign
    roster = {'by_code': dict(ROSTER_FIXTURE['by_code']), 'codes_by_name': {}}
    roster['by_code'][u'560080'] = {u'name': u'中药ETF汇添富', u'fund_type': u'指数型-股票'}
    pick, row = _realign_setup(monkeypatch, test_db, sector=u'中药', code=u'004529',
                               name=u'工银瑞信中证中药指数A')
    # _realign_setup 里那两份补丁要覆盖掉：名册得含 560080，域名解析得走 pingzhong 分支
    monkeypatch.setattr(audit.fund_api, u'load_fund_roster', lambda refresh=False: roster)
    monkeypatch.setattr(
        audit.fund_api, u'get_fund_domain_name',
        lambda c, use_roster=True: {
            u'status': u'ok', u'name': roster[u'by_code'].get(c, {}).get(u'name'),
            u'fund_type': None,
            u'source': u'roster' if use_roster else u'pingzhong'})
    plan = pick(test_db, row, {u'verdict': u'code_is_other_fund', u'relevance_low': False},
                used=set(), cores=CORES_FIXTURE + [u'中药'])
    assert plan[u'code'] == u'560080', plan
    created = []
    assert apply_realign(test_db, [plan], created)
    test_db.refresh(row)
    assert row.fund_code == u'560080' and row.is_fetchable is True
    identity = json.loads(row.evidence)[u'identity']
    assert identity[u'official_name'] == u'中药ETF汇添富'
    # 非名册来源（pingzhong）才算自证：拿名册写名再用名册仲裁是恒 1.0 的自我背书
    assert identity[u'evidence'][u'domain_source'] == u'pingzhong'


def test_realign_returns_none_without_candidates(test_db, monkeypatch):
    u"""粗筛后无候选必须直接返回 None，不能退化成"挑一只沾边的"。"""
    from scripts import sweep_sector_mappings as sweep
    pick, row = _realign_setup(monkeypatch, test_db)
    monkeypatch.setattr(sweep, u'etf_candidates_for', lambda *a, **k: [])
    assert pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True},
                used=set(), cores=CORES_FIXTURE) is None


def test_stale_nav_candidate_rejected(test_db, monkeypatch):
    u"""净值停更 >30 天的候选必须拒掉：储能/信创的候选本地净值全是 0 条，只看本地深度会随机选。"""
    from datetime import date, timedelta
    from scripts.sweep_sector_mappings import rank_verified_candidates
    cands = [{u'code': u'512170', u'name': u'医疗ETF华宝'}]
    stale = (date.today() - timedelta(days=90)).isoformat()
    monkeypatch.setattr(audit.fund_api, u'verify_fund_fetchable',
                        lambda c, **k: {u'is_strict_ok': True, u'ok': True,
                                        u'nav_date': stale})
    assert rank_verified_candidates(test_db, cands, set()) == []
    monkeypatch.setattr(audit.fund_api, u'verify_fund_fetchable',
                        lambda c, **k: {u'is_strict_ok': True, u'ok': True,
                                        u'nav_date': date.today().isoformat()})
    ranked = rank_verified_candidates(test_db, cands, set())
    assert [r[u'cand'][u'code'] for r in ranked] == [u'512170']


def test_core_variants_case_insensitive():
    u"""`5g`/`5G`、`Ai应用`/`AI应用` 必须等价（比较前统一大小写）。"""
    assert audit.core_variants(u'5g') == (u'5g', u'通信')
    assert audit.core_variants(u'5G') == (u'5G', u'通信')
    assert audit.contains_core(u'5getf博时', u'5G')


def test_upgrade_reason_names_the_held_fund(test_db, monkeypatch):
    u"""升级理由要写老板在册的那只基金名，不能写站点解析出的另一个名字。"""
    from datetime import date as _date
    from scripts.sweep_sector_mappings import pick_etf_upgrade
    monkeypatch.setattr(audit.fund_api, u'verify_fund_fetchable',
                        lambda c, **k: {u'is_strict_ok': True, u'ok': True,
                                        u'nav_date': _date.today().isoformat()})
    row = _mapping(u'低波', u'027137', u'广发红利低波指数A', reviewed=True)
    test_db.add(row)
    test_db.commit()
    # official_name 故意与在册名不同：两者相同的话这条用例改回旧写法也照样绿（空断言）
    upgrade = pick_etf_upgrade(test_db, row, {u'official_name': u'红利低波ETF华泰柏瑞',
                                             u'suggestions': [{u'code': u'512890',
                                                              u'name': u'红利低波ETF'}]})
    assert upgrade and u'广发红利低波指数A' in upgrade[u'reason']
    assert u'红利低波ETF华泰柏瑞' not in upgrade[u'reason']


# ---------- 代码质检第 2 轮补的用例 ----------

def test_etf_candidates_include_their_own_core(monkeypatch):
    """认领宇宙漏了自己 = `--upgrade-etf` 对 109/145 个在册板块静默变 no-op。

    调用方（`pick_etf_upgrade`）传的 all_cores 可能只来自静态表，
    本板块核心词必须在里面，否则 `mine` 恒 0、候选恒空。
    """
    _fake_roster(monkeypatch)
    assert audit.etf_candidates(u'医疗', all_cores=[u'保险', u'证券'],
                                roster=ROSTER_FIXTURE), u'自己的核心词不在宇宙里就不该颗粒无收'


def test_feeder_named_etf_on_in_exchange_code_excluded(monkeypatch):
    """把"排联接"这条规则真的跑到：16xxxx 的联接基金靠码段就被挡了，测它等于没测。"""
    roster = {'by_code': dict(ROSTER_FIXTURE['by_code']), 'codes_by_name': {}}
    roster['by_code'][u'159998'] = {u'name': u'医疗ETF联接A', u'fund_type': u'指数型-股票'}
    monkeypatch.setattr(audit.fund_api, u'load_fund_roster', lambda refresh=False: roster)
    codes = {c['code'] for c in audit.etf_candidates(
        u'医疗', all_cores=CORES_FIXTURE, roster=roster)}
    assert u'512170' in codes and u'159998' not in codes, codes


def test_synonyms_are_symmetric():
    """单向同义表会让"通信板块配 5GETF"判成不相关，接着把对的标的换掉。"""
    assert audit.sector_relevance(u'5G', u'通信ETF华夏',
                                  has_alternative=lambda c: True) is True
    assert audit.sector_relevance(u'通信', u'5GETF博时',
                                  has_alternative=lambda c: True) is True


@pytest.mark.parametrize(u'sector', [u'债券'])
def test_realign_refuses_alias_renamed_sector(test_db, monkeypatch, sector):
    """别名会把板块改写成另一个主题（实测 债券→券商），核心词与候选都不是老板看到的那个板块。"""
    pick, row = _realign_setup(monkeypatch, test_db, sector=sector, code=u'512000',
                               name=u'券商ETF华宝')
    verdict_row = {u'verdict': u'ok', u'relevance_low': True}
    assert pick(test_db, row, verdict_row, used=set(),
                cores=CORES_FIXTURE, realign_codes=set()) is None


def test_realign_refuses_proxy_codes_on_both_sides(test_db, monkeypatch):
    """刻意代理拒绝集要同时挡"行内现有代码"和"候选代码"，否则会把 512000 推给别的板块。"""
    pick, row = _realign_setup(monkeypatch, test_db)
    assert pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True}, used=set(),
                cores=CORES_FIXTURE, realign_codes={u'516810'}) is None
    assert pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True}, used=set(),
                cores=CORES_FIXTURE, realign_codes={u'159537'}) is None


def test_realign_respects_targets_held_by_other_rows(test_db, monkeypatch):
    """跨轮撞车：上一轮已经把 159537 给了别的板块，这一轮就不许再给它。"""
    pick, row = _realign_setup(monkeypatch, test_db)
    assert pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True}, used=set(),
                cores=CORES_FIXTURE, occupied={u'159537'}) is None
    plan = pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True}, used=set(),
                cores=CORES_FIXTURE, occupied={u'999999'})
    assert plan and plan[u'code'] == u'159537'


def test_realign_writes_nothing_when_only_roster_knows_the_code(test_db, monkeypatch):
    """非名册来源拿不到名字 = 唯一的循环自证被切断，此时必须不写。"""
    from scripts.sweep_sector_mappings import apply_realign
    pick, row = _realign_setup(monkeypatch, test_db)
    plan = pick(test_db, row, {u'verdict': u'ok', u'relevance_low': True}, used=set(),
                cores=CORES_FIXTURE)
    monkeypatch.setattr(
        audit.fund_api, u'get_fund_domain_name',
        lambda c, use_roster=True: {
            u'status': u'absent' if not use_roster else u'ok',
            u'name': u'信创ETF国泰' if use_roster else None,
            u'fund_type': None, u'source': u'roster' if use_roster else u'pingzhong'})
    created = []
    assert apply_realign(test_db, [plan], created) == []
    test_db.refresh(row)
    assert row.fund_code == u'516810', u'重新仲裁没过却照样换了标的'
    assert created == []


def test_demote_unwritten_falls_back_to_demotion(test_db):
    """realign 计划被否之后，坏行必须回到"该降就降"，不能顶着 reviewed=True 挂着降级结论活着。"""
    from scripts.sweep_sector_mappings import demote_unwritten
    row = _mapping(u'T-回退降级', u'000725', u'京东方Ａ', reviewed=True,
                   evidence=json.dumps({u'identity': {u'verdict': u'code_is_other_fund'}}))
    test_db.add(row)
    test_db.commit()
    plan = {u'id': row.id, u'code': u'512170'}
    results = [{u'id': row.id, u'verdict': u'code_is_other_fund', u'owner_row': False}]
    assert demote_unwritten(test_db, results, [plan], []) == 1
    test_db.refresh(row)
    assert row.reviewed is False and row.is_fetchable is False
    assert demote_unwritten(test_db, results, [plan], [plan]) == 0


@pytest.mark.parametrize(u'apply,realign,ev,expected', [
    (True, True, False, True), (False, True, False, False),
    (True, False, False, False), (True, True, True, False),
])
def test_should_realign_gate(apply, realign, ev, expected):
    from argparse import Namespace
    from scripts.sweep_sector_mappings import should_realign
    assert should_realign(Namespace(apply=apply, realign_irrelevant=realign,
                                    evidence_only=ev)) is expected


def test_identity_view_exposes_the_realign_provenance(test_db):
    """前端逐行提示与分桶直接读这几个键，键名一改就静默变空。

    `identity_realign` 记录带 `code`（写库时是 `dict(plan, ...)`，plan 必有 code）：
    溯源只在"换到的标的仍是当前标的"时算未确认，老板一旦再改标的旗标就该消失。
    """
    row = _mapping(u'T-视图', u'512170', u'医疗ETF华宝', evidence=json.dumps({
        u'identity': {u'verdict': u'ok'},
        u'identity_realign': {u'code': u'512170', u'from_code': u'516810',
                             u'from_name': u'农业ETF华夏',
                             u'core': u'信创', u'reason': u'x',
                             u'replaced': u'516810 农业ETF华夏'}}))
    view = audit.identity_view(row)
    assert view[u'realigned'] == {u'from_code': u'516810', u'from_name': u'农业ETF华夏',
                                 u'core': u'信创', u'reason': u'x',
                                 u'kind': u'identity_realign',
                                 # 存量 ETF 升级行只有这一句人话，缺了它前端只能说"查不到"
                                 u'replaced': u'516810 农业ETF华夏'}
    row.fund_code = u'600000'          # 老板自己又换了标的 = 已确认
    assert audit.identity_view(row)[u'realigned'] is None


def test_upgrade_registers_occupancy_within_the_round(test_db, monkeypatch):
    """两个板块同轮抢一只 ETF：第一个成交后必须把码登记进调用方的占用集。

    占用登记以前写在 main 的 print 循环里，谁都测不到；现在内聚进本函数。
    """
    from datetime import date
    from scripts.sweep_sector_mappings import pick_etf_upgrade
    from src.models.database import FundInfo
    monkeypatch.setattr(audit.fund_api, u'verify_fund_fetchable',
                        lambda c, **k: {u'is_strict_ok': True, u'ok': True,
                                        u'nav_date': date.today().isoformat()})
    a = _mapping(u'T-占用A', u'027137', u'华宝医疗ETF联接A', reviewed=True)
    b = _mapping(u'T-占用B', u'027138', u'华宝医疗ETF联接C', reviewed=True)
    test_db.add_all([a, b])
    test_db.add(FundInfo(fund_code=u'027137', fund_name=u'华宝医疗ETF联接A'))
    test_db.add(FundInfo(fund_code=u'027138', fund_name=u'华宝医疗ETF联接C'))
    test_db.commit()
    taken = set()
    cands = [{u'code': u'512170', u'name': u'医疗ETF华宝'}]
    first = pick_etf_upgrade(test_db, a, {u'official_name': u'华宝医疗ETF联接A',
                                         u'verdict': u'ok', u'suggestions': cands},
                             taken=taken, proxy_codes=set())
    assert first and u'512170' in taken, first
    second = pick_etf_upgrade(test_db, b, {u'official_name': u'华宝医疗ETF联接C',
                                          u'verdict': u'ok', u'suggestions': cands},
                              taken=taken, proxy_codes=set())
    assert second is None, u'同一轮里两只板块挂上了同一只 ETF'


@pytest.mark.parametrize(u'sector,expected', [
    (u'5G指数', u'5G'), (u'AI主题', u'AI'), (u'SpaceX概念', u'SpaceX'),
    (u'5G板块', u'5G'), (u'5G', u'5G'),
])
def test_sector_core_strips_generic_tail_on_latin_cores(sector, expected):
    """拉丁缩写核心词一样要先剥泛指尾，否则 `5G指数` 会当成新核心词去配标的。"""
    assert audit.sector_core(sector) == expected


def test_golden_counts_are_pinned():
    """核心词表的条数必须钉死：只跟文件自己比是恒真断言，规则改坏也发现不了。"""
    path = os.path.join(os.path.dirname(__file__), u'..', u'fixtures',
                        u'core_matcher_golden.json')
    data = json.load(io.open(path, encoding=u'utf-8'))
    c = data[u'counts']
    assert (c[u'rows'], c[u'with_core'], c[u'rejected']) == (145, 129, 16), c


def test_coerce_handles_every_datetime_field():
    """manifest 里的日期是 ISO 串，回写 DateTime 列前必须还原（含 updated_at）。"""
    from datetime import datetime
    from scripts.sweep_sector_mappings import _coerce
    stamp = datetime(2026, 9, 20, 8, 9, 10).isoformat(sep=u' ')
    for field in (u'verified_at', u'updated_at'):
        assert _coerce(field, stamp) == datetime(2026, 9, 20, 8, 9, 10), field
    assert _coerce(u'fund_name', stamp) == stamp
    assert _coerce(u'updated_at', None) is None


def test_upgrade_resets_the_review_and_states_the_new_reason(test_db):
    """升级换标的=机器替老板决定，必须回到未审查，且理由写新标的（评审 BLOCKER 1/2）。"""
    from scripts.sweep_sector_mappings import apply_results
    from src.models.database import FundInfo
    row = _mapping('医疗', '162412', '华宝医疗ETF联接A', reviewed=True,
                   reviewed_by='agent', match_source='agent', match_kind='proxy',
                   confidence=0.68, llm_reason='旧标的的理由', keywords=['旧'],
                   is_fetchable=True, owner_locked=False)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='162412', fund_name='华宝医疗ETF联接A'))
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': False,
                'official_name': '华宝医疗ETF联接A', 'reason': '代码在基金域的品种名与映射名一致',
                'jaccard': 1.0, 'suggested_code': None, 'suggested_name': None,
                'suggestions': [], 'relevance_low': False,
                'etf_upgrade': {'code': '512170', 'name': '医疗ETF华宝',
                                'replaced': '162412 x', 'local_history_rows': 12,
                                'reason': '板块「医疗」有场内 ETF 可用，按"ETF 最纯粹"替换'}}]
    apply_results(test_db, results, [], evidence_only=False, upgrade_etf=True)
    test_db.refresh(row)
    assert row.fund_code == '512170'
    assert (row.reviewed, row.reviewed_by, row.confidence, row.match_kind) == \
        (False, None, None, None)
    assert row.match_source == 'etf_upgrade' and row.keywords is None
    assert '最纯粹' in row.verify_message and '一致' not in row.verify_message, \
        'verify_message 还是旧标的的体检结论 = 前端显示假自证'
    from src.services.sector_identity_audit import identity_view
    assert 'ETF 最纯粹' in identity_view(row)['identity_reason']


def test_upgrade_registers_created_fund_info_for_rollback(test_db):
    """升级新建的档案必须登记进清单，否则 --restore-from 之后留孤儿基金。"""
    from scripts.sweep_sector_mappings import apply_results
    from src.models.database import FundInfo
    row = _mapping('传媒', '162413', '传媒联接A', reviewed=True)
    test_db.add(row)
    test_db.add(FundInfo(fund_code='162413', fund_name='传媒联接A'))
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': False,
                'official_name': '传媒联接A', 'reason': 'r', 'jaccard': 1.0,
                'suggested_code': None, 'suggested_name': None, 'suggestions': [],
                'relevance_low': False,
                'etf_upgrade': {'code': '512980', 'name': '传媒ETF',
                                'replaced': '162413 x', 'local_history_rows': 5,
                                'reason': '有场内 ETF 可用'}}]
    created = []
    apply_results(test_db, results, [], evidence_only=False, upgrade_etf=True,
                  created_codes=created)
    assert created == ['512980'], created


def test_identity_view_survives_legacy_array_evidence(test_db):
    """evidence 列历史上是 agent 写的**数组**：解析必须容错，不然一行旧数据 500 整页。"""
    row = _mapping('T-旧数组证据', '510300', '沪深300ETF',
                   evidence=json.dumps([{'stage': 'T1', 'candidates': ['510300']}]))
    view = audit.identity_view(row)
    assert view['servable'] is True and view['identity_verdict'] is None
    assert view['relevance_low'] is False


def test_agent_evidence_coexists_with_audit_identity(test_db):
    """体检写 dict、agent 写数组的那一页已经统一成 {tiers:…, identity:…}：
    换标的要保住 realign 溯源，同码重判要保住 identity。"""
    from src.services.sector_fund_agent import (
        SectorDecision, FundCandidate, apply_decision)
    from src.models.database import FundInfo
    row = _mapping('T-证据共存2', '159995', '芯片ETF', reviewed=False,
                   evidence=json.dumps({'identity': {'verdict': 'ok'},
                                         'identity_realign': {'core': '芯片'}}))
    test_db.add(row)
    test_db.commit()
    decision = SectorDecision(
        sector='T-证据共存2', status='matched', confidence=0.9,
        chosen=FundCandidate(code='512170', name='医疗ETF华宝', source='search',
                             official_name='医疗ETF华宝', t3_suitable=True, t3_score=95,
                             confidence=0.9, verify={'is_strict_ok': True}))
    apply_decision(test_db, decision)
    test_db.refresh(row)
    payload = json.loads(row.evidence)
    assert payload['identity_realign'] == {'core': '芯片'}
    assert 'identity' not in payload and payload['identity_before_agent_switch']
    assert payload['tiers'][-1]['stage'] == 'FETCH'


def test_upgrade_never_touches_owner_rows(test_db):
    """老板手定的行（`reviewed_by='owner'`/`owner_locked`）不参与场内 ETF 升级。"""
    from scripts.sweep_sector_mappings import apply_results
    from src.models.database import FundInfo
    row = _mapping('债券', '512000', '券商ETF华宝', reviewed=True, reviewed_by='owner',
                   owner_locked=True, match_source='manual', confidence=0.5,
                   is_fetchable=True, verify_message='老板确认的有意代理：无债市标的')
    test_db.add(row)
    test_db.add(FundInfo(fund_code='512000', fund_name='券商ETF华宝'))
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': True,
                'official_name': '券商ETF华宝', 'reason': '代码在基金域的品种名与映射名一致',
                'jaccard': 1.0, 'suggested_code': None, 'suggested_name': None,
                'suggestions': [], 'relevance_low': False,
                'etf_upgrade': {'code': '511010', 'name': '国债ETF', 'from_code': '512000',
                                'replaced': 'x', 'local_history_rows': 3,
                                'reason': '不该发生'}}]
    apply_results(test_db, results, [], evidence_only=False, upgrade_etf=True)
    test_db.refresh(row)
    assert row.fund_code == '512000'
    assert row.verify_message == '老板确认的有意代理：无债市标的', '体检结论刷掉了老板的理由'
    assert row.owner_locked is True and row.reviewed is True


def test_audit_run_keeps_user_facing_reason(test_db):
    """D1 回归：一次普通体检（ok 结论）不许把面向用户的理由列刷成 Jaccard 语句。"""
    from scripts.sweep_sector_mappings import apply_results
    row = _mapping('中药', '560080', '中药ETF汇添富', reviewed=True, match_source='agent',
                   verify_message='直接对应：跟踪中证中药指数')
    test_db.add(row)
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'ok', 'reviewed': True, 'owner_row': False,
                'official_name': '中药ETF汇添富', 'jaccard': 1.0,
                'reason': '代码在基金域的品种名与映射名一致（Jaccard 1.000）',
                'suggested_code': None, 'suggested_name': None, 'suggestions': [],
                'relevance_low': False}]
    apply_results(test_db, results, [], evidence_only=False)
    test_db.refresh(row)
    assert row.verify_message == '直接对应：跟踪中证中药指数'
    # 但降级结论必须写进去：那才是这一列要告诉老板的"为什么不能用"
    row2 = _mapping('测试刷写', '000725', '京东方Ａ', reviewed=True, verify_message='')
    test_db.add(row2)
    test_db.commit()
    r2 = [{'id': row2.id, 'verdict': 'not_a_fund', 'reviewed': True, 'owner_row': False,
           'official_name': '大成添利宝货币B', 'jaccard': 0.0,
           'reason': '「京东方Ａ」在基金域查不到同名产品', 'suggested_code': None,
           'suggested_name': None, 'suggestions': [], 'relevance_low': False}]
    apply_results(test_db, r2, [r2[0]], evidence_only=False)
    test_db.refresh(row2)
    assert row2.verify_message == '「京东方Ａ」在基金域查不到同名产品'


def test_demote_unwritten_respects_midrun_owner_edits(test_db):
    """D2：计划未成交时也不许拿计划期的旧事实替老板解锁 / 判死他刚换的标的。"""
    from scripts.sweep_sector_mappings import demote_unwritten
    row = _mapping('T-回退看现状', '000938', '紫光股份', reviewed=True,
                   evidence=json.dumps({'identity': {'verdict': 'code_is_other_fund'}}))
    test_db.add(row)
    test_db.commit()
    results = [{'id': row.id, 'verdict': 'code_is_other_fund', 'owner_row': False}]
    plan = {'id': row.id, 'from_code': '000938', 'code': '512170'}
    # 老板在计划期内把标的换了并锁定 → 不能被回退降级
    row.fund_code = '510300'
    row.owner_locked = True
    test_db.commit()
    assert demote_unwritten(test_db, results, [plan], []) == 0
    test_db.refresh(row)
    assert row.owner_locked is True and row.reviewed is True
    # 解锁后，标的仍是计划里那个坏码 → 该降就降
    row.owner_locked = False
    row.fund_code = '000938'
    test_db.commit()
    assert demote_unwritten(test_db, results, [plan], []) == 1
    test_db.refresh(row)
    assert row.reviewed is False and row.is_fetchable is False


def test_denied_map_uses_the_single_unservable_judge(test_db):
    """第 21 轮 MAJOR-2：静态表那一步的拒绝集以前只看 `is_fetchable` 列。

    "不可服务"在库里有两处事实源（列 + `evidence.identity.verdict`），拒绝集只认列
    ⇒ 它是同一件事的**第四把尺子**：列没写、verdict 已否定的行，在硬编码表这一跳
    又能把代码交回验证链路。镜像今天背离 0 行，所以是潜伏口，不是今天的故障。
    """
    import json

    from src.models.database import SectorFundMapping
    from src.services.sector_identity_audit import denied_code_map, row_unservable

    row = SectorFundMapping(sector_name='拒绝集尺子', fund_code='999001',
                            fund_name='某股票名挂在基金码', is_active=True,
                            is_fetchable=None,          # 列没写
                            evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    test_db.add(row)
    test_db.commit()
    assert row_unservable(row) is True, '前置：唯一的判据认为它不可服务'

    denied = denied_code_map(db=test_db)
    assert '999001' in denied.get('拒绝集尺子', set()), \
        '拒绝集漏掉了"只有 verdict 否定"的行 ⇒ 第 4 步会把不可服务的代码再交出去'


def test_a_manifest_test_leaves_no_copy_in_the_repo():
    u"""用例产物不许堆进 `docs/`（本机实测漏过两份 `sweep-manifest-pytest-realign*.json`）。

    只认带 `pytest` 的 tag ⇒ 真实演练留下的回滚依据（`sweep-manifest-20260921-*.json`）不误伤。
    排在两处 `write_manifest` 之后（同文件按定义顺序跑），所以"漏在磁盘上"当场看得见。
    """
    docs = os.path.join(u'docs', u'迭代计划', u'run-2026-09-20')

    def leaked():
        return sorted(f for f in os.listdir(docs)
                      if f.startswith(u'sweep-manifest-') and u'pytest' in f)

    control = os.path.join(docs, u'sweep-manifest-pytest-control.json')
    try:
        with io.open(control, u'w', encoding=u'utf-8') as f:
            f.write(u'{}')
        assert u'sweep-manifest-pytest-control.json' in leaked(), \
            u'现造一份泄漏它也看不见 ⇒ 这把尺子恒空'
    finally:
        os.remove(control)
    assert not leaked(), \
        u'清单用例把产物写进了仓库的 docs/：%s ⇒ 把 `OUT_DIR` 钉到 tmp_path' % leaked()
