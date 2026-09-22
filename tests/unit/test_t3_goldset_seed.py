# -*- coding: utf-8 -*-
"""T3 金标集种子脚本：分数必须跟着**本行那只基金**走。

第 27 轮 D-MAJOR-2 实测：`emit_t3_goldset_seed.py` 原来取 `t3s[0]`，而 T3 是逐个候选
评估的（一行多条）⇒ 52 行里 29 行发布的分数不属于本行基金。这份表是用来标定
`T3_PASS_SCORE = 70` 的，配错候选等于拿别人的答案校准自己的判据。
同族的错还有 claim_sim 那次"47 条其实 10 条"（行级字段配了别行的子记录）。
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    'emit_t3_goldset_seed', os.path.join(ROOT, 'scripts', 'emit_t3_goldset_seed.py'))
gold = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gold)


# 真实结构（镜像 `核聚变 / 159525` 那行的 evidence.tiers 摘录）：本码在第二条
FU = [
    {'stage': 'T3', 'code': '562350', 'suitable': False, 'proxy': True, 'score': 30.0},
    {'stage': 'T3', 'code': '159525', 'suitable': True, 'proxy': False, 'score': 90.0},
    {'stage': 'T3', 'code': '159611', 'suitable': False, 'proxy': True, 'score': 40.0},
]


def test_score_follows_the_own_fund_code_of_the_row():
    rec = gold.pick_own_t3(FU, '159525')
    assert rec['score'] == 90.0 and rec['suitable'] is True, rec
    # 这一条同时钉住"不许拿第一条顶"：t3s[0] 是 30.0 / suitable=False
    assert rec is not FU[0]


def test_missing_own_record_returns_none_instead_of_borrowing():
    """配不到本码记录时返回 None（脚本会把它单独计数报出来），不许借别的候选的分数。"""
    assert gold.pick_own_t3(FU, '999999') is None
    assert gold.pick_own_t3([], '159525') is None
    assert gold.pick_own_t3(None, '159525') is None


def test_code_comparison_tolerates_none_and_int_codes():
    """库里 `fund_code` 是文本，但 evidence 里的候选代码可能来自 JSON 数字或 None。"""
    assert gold.pick_own_t3([{'code': 159525, 'score': 90.0}], '159525') is not None
    assert gold.pick_own_t3([{'code': None, 'score': 90.0}], None) is None
    assert gold.pick_own_t3([{'code': '562350', 'score': 30.0}], '') is None
