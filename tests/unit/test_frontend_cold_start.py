# -*- coding: utf-8 -*-
"""冷启动与"取不到数据"时的前端诚实性（任务 #27 / #26 / #30 / #32 的页面那一半）。

判据形态：这个仓库测前端的方式就是扫 `web/index.html` 文本（`test_frontend_loading.py`
一系都是这么写的）。**文本断言的天花板很低**，第 29 轮两份复评当场证给我看：
- 一条 `'「」' not in text` 是恒真的 —— 渲染出来的空格子来自 `{{ }}` 插值，
  而字面 `「」` 只出现在注释里，两者永不相等；
- 一条 `<th[^>]*>` 把属性整个吃掉，于是 `not any('title=' in h)` 结构上不可能响。
所以下面每条判据都配了一个**可复跑的变异**：`python scripts/mutation_proof_frontend.py`
（33 处变异、覆盖本文件 14 条判据；把源码逐处退回"修复前的形状"，对应判据必须红，
跑完逐文件回读比对还原，并校验变异真的落了盘）。判据没配到变异的一律不算数。
其中两条**不读文本**：`test_the_wake_retry_behaves_the_way_the_page_needs_it` 与
`test_check_auth_and_the_login_gate_behave_per_status_code` 用 node 执行页面里那份源码，
喂 401 / 403 / 502 / 503 / 500 / 断网 / 叫不醒七种真实形状。
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
    # 等待时长在四处文案里说同一件事（第 31 轮 B-MINOR-4：三处写"约 30~60 秒"、一处写"最长 90 秒"）
    assert '（约 30~60 秒）' not in html, '还有旧版唤醒时长说法，四处不一致'
    assert html.count('通常 30~60 秒，最长再等 90 秒') >= 4, '唤醒时长说法没统一（现 %d 处）' % html.count(
        '通常 30~60 秒，最长再等 90 秒')


def test_a_missing_number_is_not_rendered_as_zero():
    """"没取到"不能渲染成 0：0 是一个断言（"库里没有"），而那一刻我们只知道没取到。

    镜像实测 27 个博主。上一版 `fetchStats` 的 catch 只有 `console.error`，
    五个统计卡写的是 `stats.overall?.X || 0` ⇒ 唤醒失败首屏就是"0 博主 / 0 帖子"。
    """
    html = _html()
    assert 'const statVal = (v) => (statsError.value' in html, '统计卡没走 statVal：取不到时又会报 0'
    assert 'v === undefined' in _expr(html, 'const statVal'), \
        'statVal 只看 statsError：后端改字段名时它会报 0（第 31 轮 B-MINOR-1）'
    cards = re.findall(r'<div class="value">\{\{(.*?)\}\}</div>', html, flags=re.S)
    assert len(cards) >= 6, '统计卡只扫到 %d 张，正则或结构变了' % len(cards)
    assert all('statVal(' in c for c in cards if 'stats.overall' in c), \
        ['还是 `|| 0` 形态的卡：%s' % c for c in cards if 'stats.overall' in c and 'statVal(' not in c]
    # 第 6 张"待清理"卡不读 `stats.overall`，上一版判据对它结构上不可能响（第 30 轮两份复评同点）。
    # 注意不能写成"'|| 0' 且没有 '?'"——`retentionPreview?.total || 0` 里的 `?.` 会把它糊过去。
    unguarded = [c.strip() for c in cards if 'statVal(' not in c and "'—'" not in c]
    assert not unguarded, '这些卡把"没取到"报成 0：%s' % unguarded
    assert 'statsError' in _body(html, 'fetchStats = async () =>'), 'fetchStats 不记失败原因'


def test_the_empty_state_cannot_lie_while_a_fetch_is_still_pending():
    """空状态必须先排掉"还在取"和"没取到"，最后才允许说"库里没有"。

    顺序错了就会在等待唤醒的那几十秒里报"暂无博主数据"：上一版把 `bloggersError`
    在函数入口清空，于是错误被抹掉、`loading` 又已经是 false ⇒ 假事实（第 29 轮 A-MAJOR-2）。
    """
    html = _html()
    block = re.search(r'<div v-else class="empty-state">(.*?)</div>', html, flags=re.S)
    assert block and '暂无博主数据' in block.group(1), '找不到博主榜的空状态'
    src = re.sub(r'<!--.*?-->', '', block.group(1), flags=re.S)
    # 顺序要**从源码位置算出来**：上一版是"从固定元组里筛出现过的词"，
    # 那跟分支的真实顺序无关，把四支整个倒过来它照样绿（第 30 轮 A-MAJOR-1）。
    pos = {k: src.find('v-if="%s"' % k) if k == 'serviceWaking' else src.find('v-else-if="%s"' % k)
           for k in ('serviceWaking', 'loading', 'bloggersError')}
    pos['暂无博主数据'] = src.find('暂无博主数据')
    assert all(v >= 0 for v in pos.values()), '少了一条分支：%s' % pos
    assert [k for k, _ in sorted(pos.items(), key=lambda kv: kv[1])] == \
        ['serviceWaking', 'loading', 'bloggersError', '暂无博主数据'], \
        '空状态分支顺序（现 %s）：等待期/在飞期会先撞上"库里没有"那句' % sorted(pos.items(), key=lambda kv: kv[1])
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
    for phrase in ('现算命中率', '物理清理', '软删', '等级 grade 按它定'):
        assert phrase in text, '%s 不在正文里：窄屏读不到口径' % phrase
    # 措辞必须与算法一致：`blogger_stats` 只累 `archived_*`（物理清理），软删不进
    assert '含已删除归档' not in text, '又写成"含已删除归档"了：回收站软删根本不进这个数'
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
        "const serviceWaited = ref(false);\n"
        "const serviceProblem = ref('');\n"
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


def _run_page_js(snippet, helpers):
    """把 `index.html` 里那份源码 + 桩环境喂给 node，跑真实调用路径。"""
    if not NODE:
        pytest.skip('本机没有 node')
    prelude = """
const ref = (v) => ({ value: v });
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null),
                       setItem: (k, v) => { store[k] = String(v); },
                       removeItem: (k) => { delete store[k]; } };
const calls = []; const seenWaking = [];
let impl = null;
// 唤醒轮询是真会睡 90 秒的代码：这里把时钟与定时器接管掉，让"叫不醒"那一档也能秒级跑完
let fakeNow = Date.now();
Date.now = () => fakeNow;
const setTimeout = (fn) => { fn(); return 0; };
const axios = { defaults: { headers: { common: {} } },
                get: async (url) => { calls.push(url); if (url === '/api/health') seenWaking.push(serviceWaking.value); return impl(url); } };
const window = { setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {},
                 dispatchEvent: () => {}, CustomEvent: class {} };
const serviceWaking = ref(false); const serviceProblem = ref(''); const serviceWaited = ref(false);
const statsError = ref(''); const bloggersError = ref(''); const evidenceError = ref('');
const evidenceReport = ref(null); const stats = ref(null); const loading = ref(true);
const bloggers = ref([]);
const showPasswordModal = ref(false), passwordInput = ref(''), passwordVerifying = ref(false);
const passwordError = ref(''), authReady = ref(false), serviceUnreachable = ref(false);
let wakeWait = null;
const fetchStats = async () => {}; const fetchBloggers = () => {};
const restorePredictionVerifyTask = async () => {};
"""
    src = prelude + '\n'.join(helpers) + '\n' + snippet
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run([NODE, path], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=90)
        assert r.returncode == 0, 'node 跑挂：%s' % (r.stderr or r.stdout)[:400]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        Path(path).unlink(missing_ok=True)


def _wake_helpers(html):
    return [_expr(html, 'const isAuthRejection'),
            _expr(html, 'const isServiceDown'),
            _decl(html, 'waitUntilAwake = (maxSeconds = 90) =>'),
            _decl(html, 'withWakeRetry = async (fn) =>')]


def test_check_auth_and_the_login_gate_behave_per_status_code():
    """把 `checkAuth` / `retryConnect` 原样抽出来在 node 里跑，喂四种真实服务端形状。

    第 30 轮 A 的探针证明：只测助手函数（上一条判据）时，把 `isAuthRejection` 放宽到
    "任何有响应的错都算口令错"、或不置 `serviceWaking`、或压根不弹"连不上服务"，
    10 条判据全绿。这条测的是**调用点**：口令留没留、弹窗开没开、等没等醒、探针打了几次。
    """
    html = _html()
    helpers = (_wake_helpers(html)
               + [_decl(html, 'checkAuth = async () =>'),
                  _decl(html, 'submitPassword = async () =>'),
                  _decl(html, 'retryConnect = async () =>')])
    driver = """
const err = (o) => { const e = new Error('x'); Object.assign(e, o); return e; };
const run = async (name, first, healthDown) => {
    for (const k of Object.keys(store)) delete store[k];
    store['access_password'] = 'right-pw';
    calls.length = 0; seenWaking.length = 0;
    serviceProblem.value = ''; serviceWaited.value = false; serviceUnreachable.value = false;
    showPasswordModal.value = false; authReady.value = false;
    let n = 0;
    impl = async (url) => {
        if (url === '/api/health') {
            if (healthDown) { fakeNow += 3000; throw err({ code: 'ERR_NETWORK' }); }
            return { data: { status: 'ok' } };
        }
        n += 1;
        if (n === 1 && first) throw first;
        return { data: { success: true, data: {} } };
    };
    await checkAuth();
    return { name, kept: !!localStorage.getItem('access_password'),
             modal: serviceUnreachable.value, pwModal: showPasswordModal.value,
             ready: authReady.value, waited: serviceWaited.value,
             probes: calls.filter(u => u === '/api/health').length,
             statsCalls: calls.filter(u => u === '/api/stats').length,
             problem: serviceProblem.value, wakingSeen: seenWaking.some(v => v === true) };
};
(async () => {
    const out = [];
    out.push(await run('ok', null, false));
    out.push(await run('unauthorized', err({ response: { status: 401 } }), false));
    out.push(await run('forbidden', err({ response: { status: 403 } }), false));
    out.push(await run('gateway', err({ response: { status: 502 } }), false));
    out.push(await run('no_password_config', err({ response: { status: 503 } }), false));
    out.push(await run('server_error', err({ response: { status: 500 } }), false));
    out.push(await run('network', err({ code: 'ERR_NETWORK' }), false));
    out.push(await run('never_wakes', err({ code: 'ERR_NETWORK' }), true));
    // 同一套状态码规则也要覆盖"老板手输口令"那条路（第 31 轮：它此前零判据）
    const runSubmit = async (name, first) => {
        for (const k of Object.keys(store)) delete store[k];
        store['access_password'] = 'old-pw';
        passwordInput.value = 'typed-pw'; passwordError.value = '';
        showPasswordModal.value = true; authReady.value = false;
        calls.length = 0;
        let n = 0;
        impl = async (url) => {
            if (url === '/api/health') { if (first) { fakeNow += 3000; throw err({ code: 'ERR_NETWORK' }); } return { data: { status: 'ok' } }; }
            n += 1;
            if (n === 1 && first) throw first;
            return { data: { success: true, data: {} } };
        };
        await submitPassword();
        return { name, kept_old: !!localStorage.getItem('access_password'),
                 input_kept: !!passwordInput.value, ready: authReady.value,
                 err: passwordError.value };
    };
    out.push(await runSubmit('submit_ok', null));
    out.push(await runSubmit('submit_unauthorized', err({ response: { status: 401 } })));
    out.push(await runSubmit('submit_gateway', err({ response: { status: 502 } })));
    out.push(await runSubmit('submit_server_error', err({ response: { status: 500 } })));
    process.stdout.write(JSON.stringify(out));
})();
"""
    rows = {r['name']: r for r in _run_page_js(driver, helpers)}
    assert rows['ok']['ready'] and rows['ok']['kept'], '正常口令被误清/没进系统'
    for name in ('unauthorized', 'forbidden'):
        assert not rows[name]['kept'], '%s 这一档才该清口令' % name
        assert rows[name]['pwModal'] and not rows[name]['modal'], '口令错要弹密码框，不是弹"连不上"'
        assert rows[name]['probes'] == 0, '口令错却去轮 /api/health：老板白等 90 秒'
    for name in ('gateway', 'no_password_config', 'network'):
        assert rows[name]['kept'], '%s 是服务端的错，清老板口令等于惩罚用错的人' % name
        assert rows[name]['probes'] >= 1 and rows[name]['waited'], '%s 没排队等醒' % name
        assert rows[name]['wakingSeen'], '等待期间没置 serviceWaking ⇒ 首屏只有一行"加载中"'
        assert rows[name]['ready'] and not rows[name]['modal'], '%s 醒了就该进去，别再弹窗' % name
        assert rows[name]['statsCalls'] == 2, '%s 等醒后没把那一笔重跑' % name
    assert rows['never_wakes']['kept'] and rows['never_wakes']['modal'], \
        '叫不醒时要弹"连不上服务"，而且不许清口令'
    assert rows['never_wakes']['probes'] >= 3, \
        '等待窗口没真跑起来（探针只 %d 次）' % rows['never_wakes']['probes']
    assert rows['server_error']['kept'] and rows['server_error']['probes'] == 0, \
        '500 是代码 bug，不是实例在睡：不该排队，更不该清口令'
    assert rows['server_error']['modal'] and '500' in rows['server_error']['problem'], \
        '弹窗要说"服务返回 500"，不能含糊成"连不上"'
    # 手输口令那条路（`submitPassword`）此前零判据：A 放宽/收紧它都绿
    assert rows['submit_ok']['ready'], '正常口令没能登进系统'
    assert not rows['submit_unauthorized']['kept_old'], '401 才该把旧口令清掉'
    assert '密码错误' in rows['submit_unauthorized']['err']
    for name in ('submit_gateway', 'submit_server_error'):
        assert rows[name]['kept_old'], '%s 时不许清掉老板存过的口令' % name
        assert rows[name]['input_kept'], '失败时不许把老板刚输的口令也抹掉（还得重打一遍）'
        assert '没有丢' in rows[name]['err'], '%s 的提示要与"口令没丢"这件事一致' % name


def test_a_lost_job_handle_needs_a_404_not_any_error():
    """任务轮询：只有 404 才允许丢句柄，其余失败留着限次再问（第 31 轮两份复评共同抓到）。

    第 30 轮我改成"`isServiceDown` 为假就 `clearJob()`"，而 401/403/500 全在"为假"那一侧；
    更要命的是 `restoreAnalysisJob()` / `restoreViewpointTask()` 在 `onMounted` 里跑，
    **不等登录门** ⇒ `ACCESS_PASSWORD` 轮换那天，正在跑的批量分析/观点汇总会静默永久失联，
    老板重输密码后看不到，很可能再点一次造成重复写入。
    """
    for fname, clear, retry in (('post-manager.js', 'clearJob()', 'pollAnalysisJob(taskId, attempts + 1)'),
                                ('viewpoint-manager.js', 'clearPoll()', 'pollTask(taskId, attempts + 1)')):
        src = (PROJECT_ROOT / 'web' / fname).read_text(encoding='utf-8')
        i = src.find('error.response.status === 404')
        assert i >= 0, '%s 的轮询 catch 不再区分 404' % fname
        tail = src[max(0, i - 220):i + 520]
        assert 'error.response.status === 404' in tail and clear in tail, \
            '%s 的 catch 不再区分 404：丢句柄的条件写错了' % fname
        assert retry in tail, '%s 没有"留着句柄再问一次"这条腿' % fname
        assert 'MAX_POLL_FAILURES' in tail, '%s 的重试没有上限（会 10 秒一次打到天荒地老）' % fname
        guard, _, rest = tail.partition('404')
        _, _, else_leg = rest.partition('} else if')
        assert clear in guard.split('404)')[-1] or clear in rest[:rest.index('else')], \
            '%s 的 404 分支没有丢句柄' % fname
        assert clear not in else_leg, '%s 在非 404 分支里仍然清了句柄' % fname
    # 句柄还得真的能跨会话恢复：restore* 读的是 localStorage，不在失败路径上删它
    html = _html()
    assert 'restoreAnalysisJob()' in html and 'restoreViewpointTask()' in html


def _setup_exports(html):
    start = html.rindex('\n                return {')
    end = html.index('\n                };', start)
    names = set()
    for line in html[start + len('\n                return {'):end].split('\n'):
        for part in line.strip().rstrip(',').split(','):
            part = part.strip()
            if part:
                names.add(re.split(r'[:(]', part)[0].strip())
    return names


def test_everything_the_template_reads_is_actually_exported():
    """模板里引用的每个根标识符，必须在 `setup()` 的 return 名单里 —— 少一个就静默失效。

    第 31 轮 A 抓到：`serviceWaited` 用在弹窗第 986 行的 `v-if` 上，却没进 return，
    于是"已经等过一轮唤醒"那一支**永不渲染**，页面永远说"这不是唤醒问题"。
    本轮返修时又扫出第二个同类：API Key 输入框读 `showApiKey`，而它从未被声明过
    （`:type="showApiKey ? 'text' : 'password'"` 恒为 password，等于一个假开关）。
    这一类没有任何机器闸（浏览器探针常年 skip），所以把它钉成判据。
    """
    html = _html()
    tpl = html[html.index('<div id="app"'):html.index('<script src="/web/post-manager.js">')]
    tpl = re.sub(r'<!--.*?-->', '', tpl, flags=re.S)
    exprs = [m.group(1) for m in re.finditer(r'\{\{(.*?)\}\}', tpl, flags=re.S)]
    for attr in ('v-if', 'v-else-if', 'v-show', 'v-model'):
        exprs += [m.group(1) for m in re.finditer(r'\s%s="(.*?)"' % attr, tpl, flags=re.S)]
    exprs += [m.group(1) for m in re.finditer(r'(?:^|\s)(?::|v-bind:)[\w.-]+="(.*?)"', tpl, flags=re.S)]
    exprs += [m.group(1) for m in re.finditer(r'(?:^|\s)@[\w.-]+="(.*?)"', tpl, flags=re.S)]
    locals_ = {'$event', '$refs', '$attrs', '$'}     # '$' 来自模板字符串的 `${...}`
    for f in re.findall(r'\sv-for="(.*?)"', tpl, flags=re.S):
        lhs, _, rhs = f.partition(' in ')
        locals_ |= {a.strip() for a in lhs.strip('()').split(',') if a.strip()}
        exprs.append(rhs)
    for grp in re.findall(r'\(([^()]*)\)\s*=>', ' '.join(exprs)):
        locals_ |= {a.strip() for a in grp.split(',') if a.strip()}
    locals_ |= {m.group(1) for m in re.finditer(r'([A-Za-z_$][\w$]*)\s*=>', ' '.join(exprs))}
    builtins = set('''true false null undefined typeof in of new this return if else void delete
        Math Date JSON Object String Number Boolean Array Set Map Intl RegExp Error Promise
        parseInt parseFloat encodeURIComponent decodeURIComponent isNaN isFinite console window
        document localStorage alert confirm length value index key toFixed toString includes join
        split map filter reduce slice push replace trim padStart padEnd startsWith endsWith
        substring concat sort some every keys entries from'''.split())
    ids = set()
    for e in exprs:
        e = re.sub(r"'[^']*'|\"[^\"]*\"", "''", e)
        for m in re.finditer(r'(?<![.\w$])([A-Za-z_$][\w$]*)', e):
            tok = m.group(1)
            if e[m.end():m.end() + 1] == ':' or tok in builtins or tok in locals_:
                continue
            ids.add(tok)
    exported = _setup_exports(html)
    assert len(ids) >= 150, '只扫到 %d 个模板标识符，扫描本身失效了（判据会变成恒真）' % len(ids)
    missing = sorted(i for i in ids if i not in exported)
    assert not missing, ('模板读了但 setup() 没导出的标识符：%s ⇒ Vue 里恒为 undefined，'
                         '那一支永远不渲染' % missing)


def test_every_list_page_shares_the_same_honesty_rule():
    """帖子/预测/观点/板块映射也一样：**没取到不能写成"库里没有"**（第 31 轮 B-MAJOR-1/2）。

    上一批只给博主榜立了规矩，判据的正则又只取**第一个** `v-else class="empty-state"`，
    其余三条腿（`暂无帖子数据`/`暂无预测数据`/`暂无观点数据`）连 catch 都没有，
    怎么改都不会红。镜像真值 657 帖 / 1616 预测 / 71 观点 ⇒ 唤醒期点进去就是假空。
    """
    html = _html()
    for view in ('posts', 'predictions', 'viewpoints'):
        assert "emptyText('%s')" % view in html, '%s 页还在无条件说"暂无数据"' % view
    assert '暂无帖子数据' not in html and '暂无预测数据' not in html and '暂无观点数据' not in html
    et = _expr(html, 'const emptyText')
    assert 'serviceWaking.value' in et and 'viewErrors[v]' in et, 'emptyText 少了唤醒/失败两条腿'
    # 板块映射页的四个数在取不到时不能报"共 0 条 / 已审查 0"
    assert "viewErrors.mappings" in html, '映射页没有失败态'
    assert html.count("viewErrors.mappings") >= 3, '映射页的失败态没同时接管计数行与取数函数'
    ls = [l for l in html.split('\n') if 'const loadSectorMappings' in l]
    assert len(ls) == 1, 'loadSectorMappings 应该只有一处定义（现 %d）' % len(ls)
    tail = ls[0][ls[0].rindex('catch'):]
    assert 'viewErrors.mappings =' in tail and 'isServiceDown(e)' in tail, \
        '映射页的 catch 又退回只 console.error'
