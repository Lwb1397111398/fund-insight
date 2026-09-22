# -*- coding: utf-8 -*-
"""未审查映射的降级路径不再兜底"体检说自己都不信"的那几行（第 26 轮，任务 #23）。

我此前对老板说过"reviewed=0 的行不会自动生效"——**说轻了**：
`get_fund_by_sector` 找不到已审查映射时会降级查 `reviewed=False`，
而降级查询原先只过"可服务"这一道门，既不看置信度也不看相关性旗标 ⇒
新帖子的板块匹配照样会用上"核聚变→红利低波ETF富国""区块链→云计算ETF招商"这类行，
而这正是老板最初抱怨的现象（"识别出来的基金离板块该对应的差十万八千里"）。

修法刻意保守：只挡"体检算过、并且**名册里确实另有字面对口的那只**"的行。
名册里查不到更对口的（核聚变、中科三环这一类）不挡 —— 挡了就是把可用映射清零，
那是制造新问题，不是解决问题。
"""
import json

from src.models.database import SectorFundMapping
from src.services.sector_fund_service import SectorFundService


def _row(db, sector, code, name, reviewed, relevance_low=None, fetchable=None):
    evidence = '{}'
    if relevance_low is not None:
        evidence = json.dumps({'identity': {'verdict': 'ok',
                                            'relevance_low': relevance_low}})
    row = SectorFundMapping(sector_name=sector, fund_code=code, fund_name=name,
                            is_active=True, reviewed=reviewed, confidence=0.7,
                            is_fetchable=fetchable, evidence=evidence)
    db.add(row)
    db.commit()
    return row


def _fresh_service(db):
    # 类级缓存会跨用例串味；这条测的是查询逻辑，不是缓存
    SectorFundService._cache.clear()
    SectorFundService._cache_loaded = False
    return SectorFundService(db)


def test_unreviewed_row_flagged_irrelevant_is_not_served(test_db):
    """改动前会红的那条：未审查 + `relevance_low=True` 以前照样被拿去贴新帖子。"""
    _row(test_db, 'ZZZ核聚变', '159525', '红利低波ETF富国', reviewed=False,
         relevance_low=True, fetchable=True)
    assert _fresh_service(test_db).get_fund_by_sector('ZZZ核聚变') is None, (
        '机器自己都标了"字面无关且另有对口标的"的行仍被服务 ⇒ 老板看到的还是错标的')


def test_unreviewed_row_without_a_better_alternative_still_serves(test_db):
    """不能一刀切：名册里没有更对口的（核聚变这类），降级服务仍然要有结果。"""
    _row(test_db, 'ZZZ小众板块', '159865', '养殖ETF', reviewed=False,
         relevance_low=False, fetchable=True)
    got = _fresh_service(test_db).get_fund_by_sector('ZZZ小众板块')
    assert got and got['code'] == '159865', '把没有更好可选的板块清成"无标的"＝制造新问题'


def test_never_audited_row_is_not_blacklisted_by_missing_flag(test_db):
    """从没体检过的行（evidence 里根本没有 identity）必须照旧可用。"""
    _row(test_db, 'ZZZ未体检', '512170', '医疗ETF', reviewed=False, fetchable=True)
    got = _fresh_service(test_db).get_fund_by_sector('ZZZ未体检')
    assert got and got['code'] == '512170', '缺旗标被当成"不相关" ⇒ 降级路整体失效'


def test_owner_reviewed_row_is_untouched_by_the_new_gate(test_db):
    """老板点过的行由他负责：同一面旗标不能把已审查映射也挡掉。"""
    _row(test_db, 'ZZZ老板认的', '159890', '云计算ETF招商', reviewed=True,
         relevance_low=True, fetchable=True)
    got = _fresh_service(test_db).get_fund_by_sector('ZZZ老板认的')
    assert got and got['code'] == '159890', '降级门越界管到已审查行＝替老板撤回了他的判断'
