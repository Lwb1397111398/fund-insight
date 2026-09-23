# -*- coding: utf-8 -*-
"""页面与三个子 manager 之间的**接线**闸（不是文本判据，是双向对账）。

为什么要有（第 34 轮 A-MAJOR-2/3、B-MAJOR-2 同一族）：
- 页面 `const { insightsLoaded } = viewpointManager;` 解构了一个 manager **没导出**的名字 ⇒
  拿到 `undefined` ⇒ 那张卡永远显示 `—`，页面不报任何错；
- manager 里读 `options.isServiceDown`，页面**忘了注入** ⇒ 断网被说成"接口报错"，
  而所有文本判据与"跑源码"的判据都照绿 —— 因为跑源码的判据自己手写了一份**完整的** options 桩，
  测的是另一套接线（A 的原话："桩在描述自己"）。

两条断言把这类问题变成会红的东西：
① 页面从某个 manager 解构出来的名字，必须真在那个 manager 的 `return {}` 名单里；
② manager 里读到的每一个 `options.X`，必须真在页面对它那次 `create…Manager({ … })` 的实参里
   （**不管有没有 `|| 兜底`** —— 有兜底恰恰是这类 bug 不说话的原因）。
"""
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HTML = (PROJECT_ROOT / 'web' / 'index.html').read_text(encoding='utf-8')

MANAGERS = {                      # 工厂名 -> (文件名, 页面里接收返回值的变量名)
    'createPostManager': ('web/post-manager.js', 'postManager'),
    'createViewpointManager': ('web/viewpoint-manager.js', 'viewpointManager'),
    'createPredictionManager': ('web/prediction-manager.js', 'predictionManager'),
}


def _balanced(text, open_idx):
    """从 `open_idx`（指向 `{`）起取配对的花括号内容。"""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i
    raise AssertionError('花括号没配平（页面或 manager 的结构变了，判据要跟着改）')


def _manager_exports(src):
    """`return { ... }` 里导出的名字：只认 `name,`（shorthand）与 `name:`（键）两种位置，
    值里的标识符不算进来 —— 否则名单被灌水，第①条闸就形同虚设。"""
    i = src.rindex('\n        return {')
    body, _ = _balanced(src, i + src[i:].index('{'))
    names = set()
    for m in re.finditer(r'([A-Za-z_$][\w$]*)\s*([:,]?)', body):
        tok, sep = m.group(1), m.group(2)
        if sep == ':':
            names.add(tok)
        elif sep == ',' and not re.match(r'^(function|return|const|let|var|new|typeof)$', tok):
            names.add(tok)
    return names


def _manager_option_reads(src):
    return set(re.findall(r'\boptions\.([A-Za-z_$][\w$]*)', src))


def _call_options(src, factory):
    """页面里 `window.createX({ ... })` 实参的键。

    一行可以写好几个 shorthand（`viewpoints, viewpointDetail, analyzing,`），
    所以逐个 token 看它后面跟的是 `:`（key: value）还是 `,`（shorthand），不能按行取第一个。
    """
    i = src.index('window.%s(' % factory)
    brace = src.index('{', i)
    body, _ = _balanced(src, brace)
    keys = set()
    for m in re.finditer(r'([A-Za-z_$][\w$]*)\s*([:,]?)', body):
        tok, sep = m.group(1), m.group(2)
        if sep == ':':
            keys.add(tok)
        elif sep == ',' and not re.match(r'^(function|return|const|let|var)$', tok):
            keys.add(tok)
    return keys


def _destructured(src, var):
    """页面里 `const { a, b, ... } = var;` 解构出来的名字（可能跨行）。

    先从 `} = var;` 往回找**最近**的 `const {` —— 直接用惰性正则会从文件里第一个
    `const {` 开始配（那是 `const { createApp, ... } = Vue;`），一吞就吞掉整页。
    """
    end = re.search(r'\}\s*=\s*%s;' % re.escape(var), src)
    assert end, '页面上找不到 `const { ... } = %s;`，改名要一起改这条判据' % var
    start = src.rindex('const {', 0, end.start())
    return {t.strip() for t in src[start + len('const {'):end.start()].split(',') if t.strip()}


@pytest.mark.parametrize('factory', sorted(MANAGERS))
def test_everything_the_page_destructures_is_actually_exported(factory):
    fname, var = MANAGERS[factory]
    src = (PROJECT_ROOT / fname).read_text(encoding='utf-8')
    exported = _manager_exports(src)
    got = _destructured(HTML, var)
    missing = sorted(n for n in got if n not in exported)
    assert not missing, \
        '页面从 %s 解构了它没导出的名字：%s ⇒ 拿到 undefined，那块数静默变 —（不报错）' % (fname, missing)


@pytest.mark.parametrize('factory', sorted(MANAGERS))
def test_every_option_the_manager_reads_is_actually_injected(factory):
    fname, _var = MANAGERS[factory]
    src = (PROJECT_ROOT / fname).read_text(encoding='utf-8')
    reads = _manager_option_reads(src)
    injected = _call_options(HTML, factory)
    missing = sorted(reads - injected)
    assert not missing, \
        '%s 读这些 options：%s，但页面的 `%s({…})` 没给 ⇒ 带兜底的那些会**不说话地降级**' \
        '（第 34 轮 `isServiceDown` 就是这么漏的）' % (fname, missing, factory)


def test_the_gate_itself_is_not_vacuous():
    """两条闸都必须**看得见东西**：名单为空就等于没闸（第 30 轮起反复立的规矩）。"""
    for factory, (fname, var) in MANAGERS.items():
        src = (PROJECT_ROOT / fname).read_text(encoding='utf-8')
        assert len(_manager_exports(src)) > 10, '%s 的导出名单只解析出 %d 个，正则失效了' % (
            fname, len(_manager_exports(src)))
        assert _manager_option_reads(src), '%s 一个 options.* 都不读？那这条闸是空的' % fname
        assert len(_call_options(HTML, factory)) > 4, '%s 的实参键只解析出一点点，页面结构变了' % factory
        assert _destructured(HTML, var), '页面没从 %s 解构任何东西' % var
