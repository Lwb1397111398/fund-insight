# -*- coding: utf-8 -*-
"""字面这根轴的**第三态**（任务 #32）：名册里查无对口基金 ≠ 已核对为相关。

第 27 轮的实测：`sector_relevance` 为了不把 `市场→上证50`、`大盘→沪深300` 这类
故意的宽基代理全判成不相关，把"名册里查无更对口"直接算成了"相关" ——
于是 `区块链→云计算ETF`、`核聚变→红利低波ETF`、`低空经济→机器人ETF` 这批行
**永远不会被旗标**、没人复核，却还在给新帖子挑标的。
镜像 2026-09-23 量到这样的未审查行 17 条（命令见 `docs/迭代计划/S6-上线前检查单.md` §7 附近）。

现在 `relevance_state()` 给三态，体检把第三态写进 `evidence.identity.relevance_state`，
`build_worklist` 单独报数。服务判据（要不要拿这行去贴新帖）**本轮不改** ——
那 17 条里既有 `核聚变→红利低波ETF`（该拦）也有 `北美→纳指ETF`（是对的），
硬拦会连正确的也一起掉，所以先把状态摆出来，拦不拦是老板的决定项。
"""
import json

from src.services.sector_identity_audit import (
    RELEVANT, RELEVANT_ALT_EXISTS, RELEVANT_NO_LITERAL, build_worklist,
    relevance_state, row_relevance_state, sector_relevance,
)

NO_ALT = lambda _core: False      # noqa: E731  名册里查无含该词的基金
HAS_ALT = lambda core: core != ''  # noqa: E731  名册里另有含该词的基金


def test_the_three_states_are_distinguishable():
    assert relevance_state('半导体', '半导体ETF国联安', HAS_ALT) == RELEVANT
    # 字面说不出话 + 名册里另有更对口 ⇒ 这才是该降的错挂
    assert relevance_state('区块链', '云计算ETF富国', HAS_ALT) == RELEVANT_ALT_EXISTS
    # 同样字面说不出话，但名册里压根查无含"区块链"的基金 ⇒ 第三态，不是"相关"
    assert relevance_state('区块链', '云计算ETF富国', NO_ALT) == RELEVANT_NO_LITERAL


def test_no_longer_silently_reported_as_relevant():
    """这一条是 #32 的本体：老写法把第三态压成 True（"相关"），于是永不旗标。"""
    assert sector_relevance('区块链', '云计算ETF富国', NO_ALT) is True   # 兼容：不改变判据
    assert relevance_state('区块链', '云计算ETF富国', NO_ALT) != RELEVANT, \
        '第三态又被压成"相关"了 ⇒ 这批行会重新变成无人复核却仍在服务'


def test_shared_single_char_still_counts_as_relevant():
    """与 `audit_static_sector_map.relevance_kind` 同一方向：共一个汉字算弱命中，不算错挂。"""
    assert relevance_state('建材', '基建ETF国泰', HAS_ALT) == RELEVANT


class _Row:
    def __init__(self, evidence):
        self.evidence = evidence if isinstance(evidence, str) else json.dumps(evidence)


def test_row_state_reads_the_stored_three_states():
    for state in (RELEVANT, RELEVANT_ALT_EXISTS, RELEVANT_NO_LITERAL):
        row = _Row({'identity': {'verdict': 'ok', 'relevance_state': state}})
        assert row_relevance_state(row) == state


def test_legacy_rows_are_not_invented_as_flagged():
    """老数据只有布尔位：`relevance_low=True` 能确定是"另有更对口"；
    `False` **不能**读成"已核对为相关"（那一档里混着第三态），只能保守当 relevant，
    等下一次体检补上三态。"""
    assert row_relevance_state(_Row({'identity': {'relevance_low': True}})) == RELEVANT_ALT_EXISTS
    assert row_relevance_state(_Row({'identity': {'relevance_low': False}})) == RELEVANT
    assert row_relevance_state(_Row(None)) == RELEVANT
    assert row_relevance_state(_Row('不是 JSON')) == RELEVANT


def test_worklist_counts_the_third_state_separately():
    rows = [{'verdict': 'ok', 'code': '1', 'stored_name': 'x', 'reviewed': False,
             'relevance_state': RELEVANT_NO_LITERAL},
            {'verdict': 'ok', 'code': '2', 'stored_name': 'y', 'reviewed': False,
             'relevance_state': RELEVANT_ALT_EXISTS},
            {'verdict': 'ok', 'code': '3', 'stored_name': 'z', 'reviewed': True,
             'relevance_state': RELEVANT}]
    work = build_worklist(rows)
    assert work['no_literal_fund'] == 1 and work['alternative_exists'] == 1, work
