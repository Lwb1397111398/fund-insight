# -*- coding: utf-8 -*-
"""给老板看的那份文档里，凡是引号框起来的"页面上会写的那句话"，必须逐字出自页面或接口。

第 79 轮我自己写下的那句话是拼出来的：文档写 页面上会写"今天已经自动补过一次"，
而页面上那一句逐字是 `今天（<那天>）已经自动补过一次，这一开没有再动。`
（`web/index.html:2600`）。这一族在本仓被扣过不止一次（第 54 轮 A-9、第 67 轮 MINOR-12、
第 68 轮那三档话术）：**转述界面话术＝替页面作保**，老板照文档去找那句话、找不着，
就会以为自己看错了。所以修完文本还要有一把尺子，否则下一批又漂。

尺子的判据面：只判 `DEPLOYMENT.md`（唯一一份"不含术语、写给老板"的文档），
只判「」框起来的引语。`<…>` 是"这里插入一个值"的占位（日期、只数），
它两侧的每一段都必须逐字能在**语料**里找到 —— 语料 = `web/index.html` +
`web/*.js` + `src/**/*.py` 的源码文本（页面插值与后端下发的文案都在里面，
例如 `目标日净值尚未发布，等待中` 出自 `src/services/prediction_verify_task.py`）。
不是界面话术的引语必须登记进 `NOT_UI_WORDING` 并写依据；登记了却文档里已经没有的，同样红。
"""
import glob
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "DEPLOYMENT.md"

# 文档里出现了「」、但它**不是**"页面上看得见的那句话"的引语 ⇒ 逐条写依据。
NOT_UI_WORDING = {
    '不得把结构迁移绑定到 Render 启动命令':
        '老板的禁令原文（`render.yaml` 不许动），不是页面文案',
}


def _corpus():
    parts = []
    for pat in ['web/index.html', 'web/*.js', 'src/**/*.py']:
        for f in glob.glob(str(ROOT / pat), recursive=True):
            parts.append(io.open(f, encoding='utf-8', errors='replace').read())
    return ''.join(parts)


def _stray_quotes(text, corpus):
    """返回 [(引语, 语料里找不到的那一段)]：`<…>` 当占位，两侧每一段都要逐字对得上。"""
    bad = []
    for q in re.findall('「([^」]+)」', text):
        if q in NOT_UI_WORDING:
            continue
        for frag in [f for f in re.split(r'<[^>]*>', q) if len(f) >= 2]:
            if frag not in corpus:
                bad.append((q, frag))
                break
    return bad


def test_deployment_md_never_paraphrases_a_sentence_it_claims_the_page_writes():
    corpus = _corpus()
    text = io.open(DOC, encoding='utf-8').read()
    stray = _stray_quotes(text, corpus)
    assert stray == [], ('DEPLOYMENT.md 引号里那句"页面上会写的话"不是逐字来的：%s'
                         '（把引语换成页面/接口里的原文，或登记进 NOT_UI_WORDING 并写依据）' % stray)


def test_a_made_up_screen_sentence_in_the_doc_is_named_by_the_ruler(tmp_path):
    """空判对照：尺子必须真的能响。现造一处拼出来的引语 ⇒ 点名它。"""
    copy = tmp_path / 'DEPLOYMENT.md'
    copy.write_text(io.open(DOC, encoding='utf-8').read()
                    + '\n6. 页面上会写「今天确实已经补跑过了喔」。\n', encoding='utf-8')
    stray = _stray_quotes(copy.read_text(encoding='utf-8'), _corpus())
    assert [q for q, _ in stray] == ['今天确实已经补跑过了喔'], stray


def test_a_placeholder_that_hides_a_wrong_fragment_is_still_caught():
    """占位符两侧的每一段都算，不许因为中间插了值就整条免检。"""
    corpus = _corpus()
    text = '「今天（<那一天>）早就自动补过一次，这一开没有再动。」'
    assert _stray_quotes(text, corpus) == [(
        '今天（<那一天>）早就自动补过一次，这一开没有再动。',
        '）早就自动补过一次，这一开没有再动。')]
    # 同一条引语把"早就"改回页面里的"已经"就应当放行（证明上一条红的是那个字，不是规则本身）。
    assert _stray_quotes(text.replace('早就', '已经'), corpus) == []


def test_the_registry_only_covers_quotes_that_are_actually_still_in_the_doc():
    """登记的豁免必须对着现文：那句话已经从文档里删了，条目就该一起删（不许留死条目）。"""
    text = io.open(DOC, encoding='utf-8').read()
    present = set(re.findall('「([^」]+)」', text))
    dead = sorted(k for k in NOT_UI_WORDING if k not in present)
    assert dead == [], 'NOT_UI_WORDING 里有文档已经不再引用的死条目：%s' % dead


@pytest.mark.parametrize('quote', sorted(NOT_UI_WORDING))
def test_a_registered_exception_says_why_it_is_not_page_wording(quote):
    assert NOT_UI_WORDING[quote].strip(), '登记「%s」却没写依据' % quote
