# -*- coding: utf-8 -*-
"""冷启动与"取不到数据"时的前端诚实性（任务 #27 / #26 / #30 / #32 的页面那一半）。

判据形态：这个仓库测前端的方式就是扫 `web/index.html` 文本（`test_frontend_loading.py`
一系都是这么写的）。**文本断言的天花板很低**，第 29 轮两份复评当场证给我看：
- 一条 `'「」' not in text` 是恒真的 —— 渲染出来的空格子来自 `{{ }}` 插值，
  而字面 `「」` 只出现在注释里，两者永不相等；
- 一条 `<th[^>]*>` 把属性整个吃掉，于是 `not any('title=' in h)` 结构上不可能响。
所以下面每条判据都配了一个**可复跑的变异**：`python scripts/mutation_proof_frontend.py`
（21 处变异、覆盖本文件 10 条判据；把源码逐处退回"修复前的形状"，对应判据必须红，
跑完逐文件回读比对还原）。判据没配到变异的一律不算数。
其中 `test_the_wake_retry_behaves_the_way_the_page_needs_it` 不读文本，它用 node **执行页面里那份源码**。
"""
import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / 'web' / 'index.html'
PREDICTION_JS = PROJECT_ROOT / 'web' / 'prediction-manager.js'


def _html():
    return INDEX_HTML.read_text(encoding='utf-8')


class _Text(HTMLParser):
    """只收正文文字；script/style 与注释都不算正文。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def _visible_text(html):
    p = _Text()
    p.feed(re.sub(r'<!--.*?-->', '', html, flags=re.S))
    return ' '.join(''.join(p.chunks).split())


def _body(src, decl, close='\n                };'):
    """取一个 16 空格缩进的箭头函数体。`decl` 写全：'checkAuth = async () =>'。"""
    m = re.search(re.escape('const ' + decl) + r' \{(.*?)' + re.escape(close), src, flags=re.S)
    assert m, '找不到 %s，判据要跟着改名一起改' % decl
    return m.group(1)


def _expr(src, decl):
    """取一条表达式体箭头函数（可能折行）：从声明到行尾分号。"""
    m = re.search(re.escape(decl) + r'.*?;\n', src, flags=re.S)
    assert m, '找不到 %s：判据要跟着改名一起改' % decl
    return m.group(0)


def test_first_load_fetches_all_go_through_the_wake_retry():
    """首屏取数点必须全部经过 `withWakeRetry`。

    口径要说准：这条只钉"列出来的这几处"，**不是**"任何新增的取数点都会红"
    （第 29 轮 B 指出我上一版注释说过头了）。新增首屏取数时请把它加进 `sites`，
    并补一条变异。`prediction-manager.js` 里那一处走注入，单独钉。
    """
    html = _html()
    assert 'const withWakeRetry = async (fn) =>' in html, '重试助手没定义'
    assert "axios.get('/api/health'" in html, '唤醒探针没打 /api/health（它免鉴权且真做 SELECT 1）'
    sites = [r"axios\.get\('/api/stats'\)",
             r"axios\.get\('/api/bloggers'\)",
             r"axios\.get\('/api/stats/evidence'\)",
             r'axios\.get\(`/api/funds\?',
             r"axios\.get\(url\)"]
    for pat in sites:
        total = len(re.findall(pat, html))
        wrapped = len(re.findall(r'withWakeRetry\(\(\) => ' + pat, html))
        assert total and total == wrapped, (
            '%s：共 %d 处、只有 %d 处走唤醒重试 ⇒ 漏掉的那处唤醒时会直接失败'
            % (pat, total, wrapped))
    # 第 6 个首屏取数点在子模块里（onMounted 就调），它只能靠注入拿到助手
    assert 'withWakeRetry,' in html, '没把 withWakeRetry 注入 predictionManager'
    js = PREDICTION_JS.read_text(encoding='utf-8')
    assert re.search(r'const wake = options\.withWakeRetry', js), '子模块没用注入的重试'
    assert "wake(() => axios.get('/api/predictions/verify-all/status'))" in js, \
        'verify-all/status 这一处首屏取数没走重试'


def test_only_an_auth_rejection_may_clear_the_saved_password():
    """`checkAuth` 里"清口令"只能由 401/403 触发。

    第 29 轮两份复评共同抓到上一版只挡了"服务器没答话"：503（本仓库没配
    `ACCESS_PASSWORD` 时 `main.py` 就是这么回的）、唤醒期的 502/504、路由内 500
    仍然走"密码失效"分支 ⇒ 服务端的错要老板重输口令。
    """
    html = _html()
    guard = _expr(html, 'const isAuthRejection')
    assert '401' in guard and '403' in guard and 'e.response.status' in guard, \
        '认证判定不再只认 401/403 ⇒ 别的状态码会顺手清掉老板口令'
    down = _expr(html, 'const isServiceDown')
    for code in ('502', '503', '504'):
        assert code in down, '唤醒期网关/服务端的 %s 没算进"服务不可用"，既不排队也不留口令' % code
    assert "!e.response" in down
    auth = _body(html, 'checkAuth = async () =>')
    wipe = auth.find("localStorage.removeItem('access_password')")
    cond = auth.find('if (!isAuthRejection(e))')
    assert 0 <= cond < wipe, '清口令又回到"任何失败都清"了'
    assert 'return;' in auth[cond:wipe], '非认证失败必须先返回，不能掉进清口令分支'
    assert 'serviceProblem.value' in auth[cond:wipe], '要说清是连不上还是服务返回了几号'


def test_the_wake_wait_is_shared_and_always_releases_the_flag():
    """并发失败只能排一次队，且 `serviceWaking` 必须在 `finally` 里复位。

    上一版每个失败调用各起一个 90 秒轮询：health 被放大十几倍，谁先退出谁把共享标记
    置 false ⇒ 还在等的调用顶着一句已经消失的提示继续转，而"重试连接"按钮永久 disabled
    的形态也真实存在（去掉 finally 就是）。
    """
    html = _html()
    wait = _body(html, 'waitUntilAwake = (maxSeconds = 90) =>')
    assert 'if (wakeWait) return wakeWait;' in wait, '没有共用一个等待：并发失败会各起一轮 90 秒轮询'
    assert 'finally {' in wait and 'serviceWaking.value = false' in wait, \
        'serviceWaking 不在 finally 里复位 ⇒ 探针异常时提示与按钮状态永久卡住'
    assert 'wakeWait = null' in wait, '等待结束后没清句柄：第二次冷启动会复用已完成的旧 Promise'
    retry = _body(html, 'withWakeRetry = async (fn) =>')
    assert 'isServiceDown(e)' in retry and 'waitUntilAwake()' in retry, \
        '重试路径不再经过等待 ⇒ "等唤醒"整段成死代码（变异 MU1 的形状）'
    assert 'throw e' in retry, '非服务不可用的错必须原样抛出（401 不能也去等 90 秒）'


def test_a_missing_number_is_not_rendered_as_zero():
    """"没取到"不能渲染成 0：0 是一个断言（"库里没有"），而那一刻我们只知道没取到。

    镜像实测 27 个博主。上一版 `fetchStats` 的 catch 只有 `console.error`，
    五个统计卡写的是 `stats.overall?.X || 0` ⇒ 唤醒失败首屏就是"0 博主 / 0 帖子"。
    """
    html = _html()
    assert re.search(r'const statVal = \(v\) => statsError\.value \? ', html), \
        '统计卡没走 statVal：取不到时又会报 0'
    cards = re.findall(r'<div class="value">\{\{(.*?)\}\}</div>', html, flags=re.S)
    stats_cards = [c for c in cards if 'stats.overall' in c]
    assert len(stats_cards) >= 5, '统计卡数量变了（现 %d），判据要跟着改' % len(stats_cards)
    assert all('statVal(' in c for c in stats_cards), \
        ['还是 `|| 0` 形态的卡：%s' % c for c in stats_cards if 'statVal(' not in c]
    assert 'statsError' in _body(html, 'fetchStats = async () =>'), 'fetchStats 不记失败原因'


def test_the_empty_state_cannot_lie_while_a_fetch_is_still_pending():
    """空状态必须先排掉"还在取"和"没取到"，最后才允许说"库里没有"。

    顺序错了就会在等待唤醒的那几十秒里报"暂无博主数据"：上一版把 `bloggersError`
    在函数入口清空，于是错误被抹掉、`loading` 又已经是 false ⇒ 假事实（第 29 轮 A-MAJOR-2）。
    """
    html = _html()
    block = re.search(r'<div v-else class="empty-state">(.*?)</div>', html, flags=re.S)
    assert block and '暂无博主数据' in block.group(1), '找不到博主榜的空状态'
    src = block.group(1)
    order = [k for k in ('serviceWaking', 'loading', 'bloggersError', '暂无博主数据') if k in src]
    assert order == ['serviceWaking', 'loading', 'bloggersError', '暂无博主数据'], \
        '空状态分支顺序错了（现 %s）：等待期/在飞期会先撞上"库里没有"那句' % order
    fb = _body(html, 'fetchBloggers = async () =>')
    clear = fb.find("bloggersError.value = ''")
    ok = fb.find('res.data.success')
    assert clear < 0 or clear > ok, '入口就清错误：唤醒等待期会把失败提示抹掉，只剩"暂无博主数据"'
    assert 'catch' in fb, 'fetchBloggers 没有 catch：唤醒失败只是未处理拒绝'


def test_realigned_note_cannot_render_an_unfilled_slot():
    """`realigned` 的每个插槽都得有自己的守卫，缺字段时要有别的说法（任务 #26）。"""
    html = _html()
    block = re.search(r'<div v-if="m\.realigned && !m\.reviewed"[^>]*>(.*?)</div>',
                      html, flags=re.S)
    assert block, '找不到"机器已纠正"横幅本体'
    src = block.group(1)
    for field in ('core', 'from_code', 'from_name'):
        slot = '{{ m.realigned.%s }}' % field
        if slot not in src:
            continue
        # 守卫必须是**这个字段自己**：写成 `v-if="m.realigned.core || kind"` 时
        # ETF 升级行（core 为空）照样进这一支，又渲染出「」（第 29 轮 A 的 MU4）
        assert 'v-if="m.realigned.%s"' % field in src.split(slot)[0], \
            '%s 的守卫不是它自己：这一类记录没这个字段，页面上就是空格子' % field
    assert "<template v-else>" in src, '三类说法之外还要有一条兜底'
    assert 'm.realigned.replaced' in src, '没读 `replaced`：存量行说不出原来是什么'


def test_blogger_table_calibers_are_readable_without_hover():
    """两个口径的说明必须是正文，不能只挂在 title 上（手机没有 hover）。"""
    html = _html()
    text = _visible_text(html)
    for phrase in ('现算命中率', '含归档', '等级 grade 按它定'):
        assert phrase in text, '%s 不在正文里：窄屏读不到口径' % phrase
    assert '排名按' in text and '命中率' in text, 'TOP 博主弹窗没有口径说明'
    # 表头：连**属性**一起看，否则 `title=` 藏在被吃掉的属性里，判据结构上不可能响
    heads = re.findall(r'<th((?:\s[^>]*?)?)>((?:(?!</th>).)*)</th>', html, flags=re.S)
    assert heads, '一个表头都没扫到 —— 正则失效了，这条判据也就是恒真的'
    labelled = [(a, t) for a, t in heads if '现算命中率' in t]
    assert labelled, '准确率表头没写口径'
    assert not [a for a, _t in heads if 'title=' in a], \
        '表头又把要看的字塞回 hover 里了（title 属性：%s）' % [a for a, _ in heads if 'title=' in a]


def test_audit_filter_explains_itself_in_text_not_only_a_title():
    """点开体检筛选按钮后，"这一档是什么意思"必须是正文（本轮自己立的规矩）。"""
    html = _html()
    text = _visible_text(html)
    assert '名册无对口' in text, '第三态没有计数入口'
    assert "relevance_state === 'no_literal_fund'" in html, '第三态没进筛选/统计'
    assert '名册里没有字面对口的标的' in text, '第三态的行内说法要能和"挂错了"区分开'
    assert '这些行挂的是代理关系' in text, '筛选后的解释只在 title 里：手机读不到'
    assert '另有字面对口的那只' in text, '"挂错了可重匹配"那一档也要有正文解释'


def test_take_data_failure_says_so_instead_of_looking_like_an_empty_database():
    """证据体检失败要说出来，并且拦住"把准确率当结论"。"""
    html = _html()
    text = _visible_text(html)
    assert '结论证据体检拉取失败' in text, '证据体检失败要说出来，不能停在"加载中"'
    assert '下面的准确率没有区间可看' in text, '取不到区间时必须拦住"把准确率当结论"'
    ev = _body(html, 'fetchEvidence = async () =>')
    tail = ev[ev.rindex('catch'):]
    assert 'evidenceError.value =' in tail, 'catch 分支必须把失败写进可见状态，不能只 console.error'
    assert 'isServiceDown(e)' in tail, '失败原因不再走同一个判据（两把尺子）'


def _decl(src, decl):
    """取整条箭头函数声明（含函数体）。`decl` 写全到 `=>`。"""
    head = 'const ' + decl + ' {'
    assert head in src, '找不到 %s' % decl
    i = src.index(head)
    j = src.index('\n                };', i) + len('\n                };')
    return src[i:j]


NODE = shutil.which('node')


def test_the_wake_retry_behaves_the_way_the_page_needs_it():
    """把 `index.html` 里那三个助手原样抽出来，在 node 里跑真实错误形状。

    第 29 轮 A-MAJOR-3：文本判据钉不住行为——把"等唤醒"整段变成死代码，6 条判据全绿。
    这条不读文本结论，它**执行页面里那一份源码**：口令错 ⇒ 不许去轮 /api/health；
    502/503/504 与断网 ⇒ 排队等醒并重跑一次；500 ⇒ 原样抛出（既不删口令也不空等 90 秒）。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    html = _html()
    prelude = (
        "const ref = (v) => ({ value: v });\n"
        "const calls = [];\n"
        "let impl = null;\n"
        "const axios = { get: async (url) => { calls.push(url); return impl(url); } };\n"
        "const serviceWaking = ref(false);\n"
        "let wakeWait = null;\n")
    helpers = '\n'.join([
        _expr(html, 'const isAuthRejection'),
        _expr(html, 'const isServiceDown'),
        _decl(html, 'waitUntilAwake = (maxSeconds = 90) =>'),
        _decl(html, 'withWakeRetry = async (fn) =>')])
    driver = """
const results = {};
const err = (o) => { const e = new Error('x'); Object.assign(e, o); return e; };
const run = async (name, first) => {
    calls.length = 0;
    let n = 0;
    impl = async (url) => {
        if (url === '/api/health') return { data: { status: 'ok' } };
        n += 1;
        if (n === 1 && first) throw first;
        return { data: { success: true, data: {} } };
    };
    try { await withWakeRetry(() => axios.get('/api/stats')); results[name] = { ok: true }; }
    catch (e) { results[name] = { ok: false, code: (e.response && e.response.status) || e.code }; }
    results[name].calls = calls.slice();
};
(async () => {
    await run('unauthorized', err({ response: { status: 401 } }));
    await run('gateway', err({ response: { status: 502 } }));
    await run('server_error', err({ response: { status: 500 } }));
    await run('network', err({ code: 'ERR_NETWORK' }));
    results.waking_after = serviceWaking.value;
    process.stdout.write(JSON.stringify(results));
})();
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(prelude + helpers + driver)
        path = f.name
    try:
        r = subprocess.run([NODE, path], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=60)
        assert r.returncode == 0, 'node 跑挂了：%s' % (r.stderr or '')[:400]
        out = json.loads(r.stdout)
    finally:
        Path(path).unlink(missing_ok=True)
    stats_only = ['/api/stats']
    waited = ['/api/stats', '/api/health', '/api/stats']
    assert out['unauthorized'] == {'ok': False, 'code': 401, 'calls': stats_only}, \
        '口令错却去轮询唤醒：老板要白等 90 秒，而且这跟唤醒无关'
    assert out['gateway'] == {'ok': True, 'calls': waited}, \
        '502（唤醒期网关）没有"等醒 + 重跑一次" ⇒ 老板看到的还是失败'
    assert out['server_error'] == {'ok': False, 'code': 500, 'calls': stats_only}, \
        '500 去排队等唤醒：那是代码 bug，不是实例在睡'
    assert out['network'] == {'ok': True, 'calls': waited}, '断网/超时这条主路径没走通'
    assert out['waking_after'] is False, '等待结束后 serviceWaking 没复位：提示与按钮会永久卡住'
