# -*- coding: utf-8 -*-
"""板块→基金 agent 单测：零网络、零真实 LLM，只注入桩。

这些用例的存在意义是"能不能证伪"：每条都对应老板抱怨的一种错配方式，
若实现退回到"随便给一只基金"，对应用例就会红。
"""
import json

import pytest

from src.models.database import SessionLocal
from src.services.sector_fund_agent import (
    SectorFundAgent, SectorDecision, FundCandidate, apply_decision,
    compute_confidence, similarity, is_etf, normalize_fund_name,
    is_broad_index_fund, sector_allows_broad_index, as_bool,
    AUTO_REVIEW_CONFIDENCE, CLAIM_SIM_REJECT,
)


@pytest.fixture(autouse=True)
def _clear_sector_caches():
    """agent 的 T0 读两处进程内缓存，测试间必须清干净，否则会互相污染。"""
    from src.services.sector_fund_service import SectorFundService
    SectorFundService._cache.clear()
    SectorFundService._cache_loaded = False
    yield
    SectorFundService._cache.clear()
    SectorFundService._cache_loaded = False


def make_agent(llm_script, verify_map=None, search_map=None):
    """llm_script: 按 LLM 调用顺序给出的返回值列表；None 表示该次调用失败。"""
    calls = {'i': 0}

    def llm(prompt):
        idx = calls['i']
        calls['i'] += 1
        if idx >= len(llm_script):
            return None
        item = llm_script[idx]
        return item  # 允许直接给 dict

    def verify(code, claimed_name=''):
        entry = (verify_map or {}).get(code, {
            'ok': True, 'is_strict_ok': True, 'kind': 'fund',
            'official_name': claimed_name or f'{code}基金', 'fund_type': '指数型'})
        return dict(entry)

    def search(keyword):
        return (search_map or {}).get(keyword, [])

    agent = SectorFundAgent(llm_call=llm, verify_call=verify, search_call=search)
    agent._prompt_log = []
    return agent


# ---------- 纯函数 ----------

def test_similarity_survives_company_prefix():
    """官方名带公司前缀时，剥离后仍能对上（否则正常映射会被误判为不相关）。"""
    assert normalize_fund_name('华夏国证半导体芯片ETF') .startswith('国证')
    assert similarity('华夏国证半导体芯片ETF', '国泰中证半导体芯片ETF') > 0.4


def test_is_etf_by_name_and_code():
    assert is_etf('159995', '芯片ETF华宝') is True
    assert is_etf('588000', '科创50ETF华夏') is True
    assert is_etf('001594', '某主动混合') is False


def test_claim_threshold_is_documented():
    assert CLAIM_SIM_REJECT == 0.35


# ---------- T3 语义闸门：根治"八竿子打不着" ----------

def test_irrelevant_fund_is_not_chosen():
    """LLM 给错方向（半导体→白酒ETF）时，T3 必须拦下，且不得自动 reviewed。"""
    agent = make_agent([
        {'candidates': [{'code': '161725', 'name': '白酒ETF', 'reason': 'x'}],
         'keywords': ['半导体'], 'aliases': []},
        {'judgements': [{'code': '161725', 'suitable': False, 'proxy': False, 'score': 5}]},
        None,   # T5 代理也不给结论
    ], verify_map={'161725': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '招商中证白酒指数(LOF)A', 'fund_type': '股票型'}})
    decision = agent.resolve('半导体', db=None)
    assert decision.chosen is None or decision.chosen.code != '161725'
    assert decision.status in ('needs_review', 'no_fund')
    assert decision.auto_reviewable is False


def test_relevant_fund_passes_and_is_reviewable():
    agent = make_agent([
        {'candidates': [{'code': '159995', 'name': '芯片ETF', 'reason': '跟踪国证半导体芯片指数'}],
         'keywords': ['半导体'], 'aliases': ['半导体芯片']},
        {'judgements': [{'code': '159995', 'suitable': True, 'proxy': False, 'score': 96}]},
    ], verify_map={'159995': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '华夏国证半导体芯片ETF', 'fund_type': '股票型'}})
    decision = agent.resolve('半导体')
    assert decision.status == 'matched'
    assert decision.chosen.code == '159995'
    assert decision.confidence >= AUTO_REVIEW_CONFIDENCE
    assert decision.auto_reviewable is True


def test_hallucinated_name_is_judged_on_official_name():
    """LLM 记错代码↔名称：基金真实存在就保留，但用官方名判断并打折置信度。"""
    agent = make_agent([
        {'candidates': [{'code': '512660', 'name': '军工ETF', 'reason': 'y'}],
         'keywords': ['卫星'], 'aliases': []},
        {'judgements': [{'code': '512660', 'suitable': False, 'proxy': False, 'score': 10}]},
        None,
    ], verify_map={'512660': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '国泰中证半导体ETF', 'fund_type': '股票型'}})
    decision = agent.resolve('SpaceX')
    stage2 = [e for e in decision.evidence if e.get('stage') == 'T2']
    assert stage2 and stage2[0]['verdict'] == 'pass'   # 没被 T2 误杀
    assert decision.chosen is None                      # 但 T3 用官方名判定不相关


def test_stock_code_rejected():
    agent = make_agent([
        {'candidates': [{'code': '600519', 'name': '贵州茅台', 'reason': 'z'}],
         'keywords': ['白酒'], 'aliases': []},
        {'judgements': []},
        None,
    ], verify_map={'600519': {'ok': False, 'is_strict_ok': False, 'kind': 'stock',
                              'official_name': None, 'fund_type': ''}})
    decision = agent.resolve('白酒')
    assert decision.chosen is None
    assert any('股票' in (e.get('reason') or '') for e in decision.evidence
               if e.get('stage') == 'T2')


# ---------- 关键词搜索回环（老板设想的 agent 路径）----------

def test_keyword_loop_asks_llm_to_pick_before_adopting():
    """T4 搜到结果后必须由 LLM 复判，绝不沿用"取第一个"的旧逻辑。"""
    search_calls = []

    def search(keyword):
        search_calls.append(keyword)
        return [{'fund_code': '561980', 'fund_name': '半导体设备ETF招商', 'fund_type': ''},
                {'fund_code': '159325', 'fund_name': '半导体ETF南方', 'fund_type': ''}]

    agent = SectorFundAgent(
        llm_call=lambda p: ({'judgements': [{'code': '159325', 'suitable': True,
                                             'proxy': False, 'score': 92}]}
                            if 'judgements' in p else
                            ({'candidates': [], 'keywords': ['半导体设备'], 'aliases': []}
                             if 'candidates' in p else None)),
        verify_call=lambda code, name='': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                                           'official_name': name or code, 'fund_type': '指数型'},
        search_call=search)
    decision = agent.resolve('半导体设备')
    assert search_calls, '关键词搜索没有被调用'
    assert decision.chosen is not None
    assert decision.direct_exhausted is True
    assert [e for e in decision.evidence if e.get('stage') == 'T4']


def test_sector_name_always_used_as_keyword():
    """LLM 给了跑题关键词时，板块名本身仍要搜一次（防 T1 幻觉带偏）。"""
    seen = []

    def search(keyword):
        seen.append(keyword)
        return []

    agent = SectorFundAgent(
        llm_call=lambda p: {'candidates': [], 'keywords': ['游戏'], 'aliases': []},
        verify_call=lambda code, name='': {'ok': True, 'is_strict_ok': True, 'kind': 'fund'},
        search_call=search)
    agent.resolve('存储')
    assert '存储' in seen


# ---------- 有意代理（老板规则：没有对口基金时取关联度最大的替代）----------

def test_proxy_only_after_direct_exhausted():
    agent = make_agent([
        {'candidates': [{'code': '159206', 'name': '军工ETF', 'reason': ''}],
         'keywords': ['卫星'], 'aliases': []},
        {'judgements': [{'code': '159206', 'suitable': False, 'proxy': False, 'score': 30}]},
        {'code': '159206', 'score': 78, 'reason': 'SpaceX 属商业航天，A 股最接近的是卫星产业/军工 ETF'},
    ], verify_map={'159206': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '军工ETF华宝', 'fund_type': '指数型'}},
        search_map={'卫星': []})
    decision = agent.resolve('SpaceX')
    assert decision.status == 'proxy'
    assert decision.chosen.t3_proxy is True
    assert decision.direct_exhausted is True
    assert any(e.get('stage') == 'T5' for e in decision.evidence)


def test_proxy_not_reviewable_when_direct_not_exhausted():
    decision = SectorDecision(sector='存储', status='proxy', direct_exhausted=False)
    cand = FundCandidate(code='159813', t3_suitable=True, t3_proxy=True, t3_score=90,
                         confidence=0.99)
    decision.chosen = cand
    decision.confidence = 0.99
    assert decision.auto_reviewable is False


def test_etf_preferred_over_lof():
    """同板块既有 ETF 又有 LOF 时选 ETF（老板：ETF 最纯粹）。"""
    agent = make_agent([
        {'candidates': [{'code': '161725', 'name': '白酒LOF'}, {'code': '512690', 'name': '酒ETF'}],
         'keywords': ['白酒'], 'aliases': []},
        {'judgements': [{'code': '161725', 'suitable': True, 'proxy': False, 'score': 95},
                        {'code': '512690', 'suitable': True, 'proxy': False, 'score': 88}]},
    ], verify_map={'161725': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '万家中证白酒指数(LOF)A'},
                   '512690': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '酒ETF鹏华'}})
    decision = agent.resolve('白酒')
    assert decision.chosen.code == '512690'


# ---------- 降级路径：LLM/网络不可用不能拖垮帖子分析主链路 ----------

def test_allow_llm_false_does_not_call_llm_or_crash():
    agent = make_agent([{'candidates': [{'code': '159995'}]}])
    decision = agent.resolve('半导体', allow_llm=False)
    assert decision.status in ('needs_review', 'no_fund', 'matched')
    assert decision.chosen is None or decision.chosen.source == 't0_static'


def test_llm_unavailable_marks_unavailable_not_wrong():
    """LLM 全程无响应 = 判定不可用（degraded），不能伪装成"这板块没有对口基金"。"""
    agent = make_agent([None, None, None])
    decision = agent.resolve('半导体')
    assert decision.chosen is None
    assert decision.status in ('needs_review', 'no_fund')
    assert any(e.get('verdict') == 'unavailable' for e in decision.evidence)


def test_budget_timeout_flagged_separately():
    agent = make_agent([{'candidates': [{'code': '159995', 'name': '芯片ETF'}],
                         'keywords': ['半导体'], 'aliases': []}])
    decision = agent.resolve('半导体', budget_ms=0)
    assert decision.timed_out is True


def test_empty_sector_returns_no_fund():
    assert SectorFundAgent().resolve('   ').status == 'no_fund'


# ---------- 写库策略：证据不齐不得 reviewed，锁定的行不得覆盖 ----------

def _seed(db, sector, code='159995', reviewed=False, locked=False):
    from src.models.database import FundInfo, SectorFundMapping
    if not db.query(FundInfo).filter_by(fund_code=code).first():
        db.add(FundInfo(fund_code=code, fund_name='占位基金'))
        db.commit()
    row = SectorFundMapping(sector_name=sector, fund_code=code, fund_name='旧名',
                            reviewed=reviewed, is_active=True, owner_locked=locked)
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _clean_mapping_rows():
    """apply_decision 会 commit，测试之间会互相看见，必须按 sector 清理干净。"""
    from src.models.database import SectorFundMapping
    yield
    db = SessionLocal()
    try:
        db.query(SectorFundMapping).filter(
            SectorFundMapping.sector_name.like('T-测试%')).delete(synchronize_session=False)
        db.commit()
    except Exception:
        # 其他用例会临时把 engine 换到自己的临时库（表都不存在），清理失败不影响本用例结论
        db.rollback()
    finally:
        db.close()


def test_low_confidence_write_keeps_reviewed_false(db_session):
    from src.models.database import SessionLocal  # noqa: F401  fixture 依赖
    row = _seed(db_session, 'T-测试低置信')
    decision = SectorDecision(
        sector='T-测试低置信', status='matched', confidence=0.40,
        chosen=FundCandidate(code='159995', name='芯片ETF', source='llm',
                             official_name='芯片ETF', t3_suitable=True,
                             t3_score=55, confidence=0.40,
                             verify={'is_strict_ok': True}))
    result = apply_decision(db_session, decision)
    db_session.refresh(row)
    assert result['applied'] is True
    assert row.reviewed is False
    assert row.match_source == 'agent'


def test_full_evidence_write_marks_reviewed(db_session):
    row = _seed(db_session, 'T-测试全证据')
    decision = SectorDecision(
        sector='T-测试全证据', status='matched', confidence=0.92,
        chosen=FundCandidate(code='159995', name='芯片ETF', source='llm',
                             official_name='芯片ETF', t3_suitable=True, t3_proxy=False,
                             t3_score=95, confidence=0.92,
                             verify={'is_strict_ok': True}))
    decision.evidence = [{'stage': 'T2', 'code': '159995', 'verdict': 'pass'},
                         {'stage': 'T3', 'code': '159995', 'suitable': True, 'score': 95}]
    apply_decision(db_session, decision)
    db_session.refresh(row)
    assert row.reviewed is True
    assert row.reviewed_by == 'agent'
    assert row.confidence == pytest.approx(0.92)
    assert [e['stage'] for e in json.loads(row.evidence)] == ['T2', 'T3']


def test_owner_locked_row_is_never_overwritten(db_session):
    row = _seed(db_session, 'T-测试锁定', locked=True)
    decision = SectorDecision(
        sector='T-测试锁定', status='matched', confidence=0.99,
        chosen=FundCandidate(code='512480', name='别的', official_name='别的',
                             t3_suitable=True, t3_score=99, confidence=0.99,
                             verify={'is_strict_ok': True}))
    result = apply_decision(db_session, decision)
    db_session.refresh(row)
    assert result['applied'] is False
    assert row.fund_code == '159995'


def test_confidence_formula_prefers_etf_and_strict_fetch():
    lof = FundCandidate(code='161725', kind='lof', source='llm', t3_score=90,
                        sector_overlap=0.3, verify={'is_strict_ok': True})
    etf = FundCandidate(code='512690', kind='etf', source='llm', t3_score=90,
                        sector_overlap=0.3, verify={'is_strict_ok': True})
    assert compute_confidence(etf) > compute_confidence(lof)
    weak = FundCandidate(code='512690', kind='etf', source='llm', t3_score=90,
                         sector_overlap=0.3, verify={'is_strict_ok': False})
    assert compute_confidence(weak) < compute_confidence(etf)


def test_ai_batch_manager_start_does_not_deadlock(monkeypatch):
    """start() 持锁期间又调用 is_running()，用非重入锁会自锁死（实测会挂住请求）。"""
    import time
    from src.services import sector_fund_agent as agent_mod
    from src.services.sector_ai_match_task import SectorAiMatchManager

    monkeypatch.setattr(agent_mod, 'resolve_sector_fund',
                        lambda sector, **kw: SectorDecision(sector=sector, status='no_fund'))
    manager = SectorAiMatchManager()
    started = time.monotonic()
    first = manager.start(['T-测试批量A'], apply=False)
    assert first['success'] is True
    assert time.monotonic() - started < 2.0, 'start() 阻塞，疑似锁重入死锁'
    second = manager.start(['T-测试批量B'], apply=False)
    assert time.monotonic() - started < 4.0, '第二次调用被卡住'
    assert second['success'] is False or second['data']['status'] in ('running', 'completed')


# ---------- 宽基凑数：跑批实测发现过的真实缺陷 ----------

def test_broad_index_rejected_for_industry_sector():
    """豆粕→中证500ETF、中药→沪深300ETF联接 这类"拿大盘凑数"必须硬拒。"""
    assert is_broad_index_fund('中证500ETF博时') is True
    assert is_broad_index_fund('沪深300ETF联接A') is True
    assert is_broad_index_fund('国泰中证豆粕ETF') is False
    assert sector_allows_broad_index('豆粕') is False
    assert sector_allows_broad_index('沪深300') is True
    assert sector_allows_broad_index('日股') is True
    assert sector_allows_broad_index('科技成长') is False

    agent = make_agent([
        {'candidates': [{'code': '159968', 'name': '中证500ETF', 'reason': ''}],
         'keywords': ['豆粕'], 'aliases': []},
        {'judgements': [{'code': '159968', 'suitable': True, 'proxy': False, 'score': 95}]},
        None,
    ], verify_map={'159968': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '中证500ETF博时', 'fund_type': '指数型'}},
        search_map={'豆粕': []})
    decision = agent.resolve('豆粕')
    assert decision.chosen is None
    assert any('宽基' in (e.get('reason') or '') for e in decision.evidence
               if e.get('stage') == 'T2')


def test_broad_index_allowed_for_market_sector():
    """日股→日经225ETF、亚太→亚太精选 是正当选择，不能被宽基规则误杀。"""
    agent = make_agent([
        {'candidates': [{'code': '513880', 'name': '日经225ETF', 'reason': ''}],
         'keywords': ['日经'], 'aliases': []},
        {'judgements': [{'code': '513880', 'suitable': True, 'proxy': False, 'score': 95}]},
    ], verify_map={'513880': {'ok': True, 'is_strict_ok': True, 'kind': 'fund',
                              'official_name': '日经225ETF华安', 'fund_type': '指数型'}})
    decision = agent.resolve('日股')
    assert decision.chosen is not None and decision.chosen.code == '513880'


def test_llm_string_false_is_not_true():
    """LLM 回 "suitable":"false" 时不能当 True（bool("false") 是 True，会误自动审查）。"""
    assert as_bool('false') is False
    assert as_bool('False') is False
    assert as_bool('是') is True
    assert as_bool(True) is True
    assert as_bool(1) is True
    assert as_bool(None) is False


def test_industry_etf_with_quanzhi_token_not_rejected():
    """`中证全指证券/医药卫生/半导体` 是行业 ETF，不能被宽基规则误杀。"""
    assert is_broad_index_fund('华宝中证全指证券公司ETF') is False
    assert is_broad_index_fund('广发中证全指医药卫生ETF') is False
    assert is_broad_index_fund('国泰中证全指半导体设备ETF') is False
    assert is_broad_index_fund('中证500ETF博时') is True
    assert is_broad_index_fund('沪深300ETF联接A') is True
    assert is_broad_index_fund('易方达创业板ETF') is True


def test_auto_review_requires_t2_and_t3_evidence():
    """没有抓站与语义证据的结论不得自动置已审查。"""
    decision = SectorDecision(
        sector='T-测试无证据', status='matched', confidence=0.99, evidence=[],
        chosen=FundCandidate(code='159995', official_name='芯片ETF', source='llm',
                             t3_suitable=True, t3_score=99, confidence=0.99,
                             verify={'is_strict_ok': True}))
    assert decision.auto_reviewable is False
    decision.evidence = [{'stage': 'T2', 'code': '159995', 'verdict': 'pass'},
                         {'stage': 'T3', 'code': '159995', 'suitable': True, 'score': 99}]
    assert decision.auto_reviewable is True


def test_resolve_creates_fresh_agent_state():
    """并发安全：单次调用状态不能挂在模块级单例上。"""
    from src.services import sector_fund_agent as mod
    a1 = mod.SectorFundAgent(llm_call=lambda p: None)
    a2 = mod.SectorFundAgent(llm_call=lambda p: None)
    a1._deadline_at = 1.0
    a1._llm_calls = 9
    assert a2._deadline_at is None and a2._llm_calls == 0
