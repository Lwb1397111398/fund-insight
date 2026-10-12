# -*- coding: utf-8 -*-
"""投资建议页 v2（可执行建议）判据 —— 页面必须把"买还是卖"说清楚。

本批（2026-10-12）把含糊的「关注板块」换成：仓位指挥牌（stance + position_label + 依据 +
override 理由）、板块操作清单（action-badge 七档 + 后端附挂的基金）、进页拉最新建议。
判据形态沿用本仓前端测法：扫 `web/index.html` 文本 + 每条配可复跑的变异
（`scripts/mutation_proof_frontend.py`，判据名以 `test_the_advice`/`test_no_page_uses`/
`test_entering_advice` 前缀路由到本文件）。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / 'web' / 'index.html'


def _html():
    return INDEX_HTML.read_text(encoding='utf-8')


def test_the_advice_page_renders_the_position_card():
    """仓位指挥牌：stance 分色、仓位话术、数据依据、override 理由四件都要在。"""
    html = _html()
    assert 'advice.position_advice' in html, '仓位指挥牌没读 position_advice'
    assert ":class=\"'stance-' + (advice.position_advice.stance || 'unknown')\"" in html, \
        'stance 分档没绑到指挥牌上（分色/分档话术会整块失效）'
    assert 'advice.position_advice.position_label' in html
    assert 'advice.position_advice.basis' in html, '数据依据（哪只宽基、涨了多少、截至哪天）没渲染'
    assert 'advice.position_advice.override_reason' in html, 'AI 逆闸时系统要求它写理由，页面却不展示'


def test_the_advice_page_lists_sector_actions_with_explicit_badges():
    """板块操作清单：七档动作徽章由后端 action_label 直出；标的基金代码要能看见；
    老建议（没有清单）回退旧标签块，不许整页空白。"""
    html = _html()
    assert 'advice.sector_actions && advice.sector_actions.length > 0' in html
    assert ":class=\"'action-' + a.action\"" in html, '动作徽章没按七档枚举上色'
    assert 'a.action_label' in html, '中文动作标签（买入建仓/全部卖出…）没渲染'
    assert 'a.fund_code' in html and 'a.fund_name' in html, '板块对应的基金标的没渲染'
    assert 'a.confidence' in html
    assert 'v-else-if="advice.suggested_sectors' in html, \
        '旧建议没有 sector_actions 时必须有回退块，否则历史页整块消失'


def test_no_page_uses_the_ambiguous_word_guanzhu_in_advice_blocks():
    """「关注」是本批明令禁止的含糊词：后端校验层拒绝含它的 advice_content，
    页面标题/标签也不许再出现（老板原话：压根不知道是买还是卖）。"""
    assert '关注' not in _html(), '「关注」又回到页面上了 —— 该词说不清买卖，v2 已全面禁用'


def test_entering_advice_view_fetches_the_latest_advice():
    """进「投资建议」页先 GET /api/advice：以前刷新即空态（后端早有这个端点，页面没接）。"""
    html = _html()
    assert "const loadLatestAdvice = async () =>" in html
    assert "axios.get('/api/advice')" in html, 'loadLatestAdvice 没打最新建议端点'
    assert "if (!advice.value) { await loadLatestAdvice(); }" in html, \
        'loadView(\'advice\') 没接上：刷新页面仍会回空态'
