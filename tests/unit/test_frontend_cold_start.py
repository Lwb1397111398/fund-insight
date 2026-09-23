# -*- coding: utf-8 -*-
"""冷启动与"取不到数据"时的前端诚实性（任务 #27 / #26 / #30 / #32 的页面那一半）。

判据形态：这个仓库测前端的方式就是扫 `web/index.html` 文本（`test_frontend_loading.py`
一系都是这么写的）。**文本断言的天花板很低**，第 29 轮两份复评当场证给我看：
- 一条 `'「」' not in text` 是恒真的 —— 渲染出来的空格子来自 `{{ }}` 插值，
  而字面 `「」` 只出现在注释里，两者永不相等；
- 一条 `<th[^>]*>` 把属性整个吃掉，于是 `not any('title=' in h)` 结构上不可能响。
所以下面每条判据都配了一个**可复跑的变异**：`python scripts/mutation_proof_frontend.py`
（35 处变异、覆盖本文件 16 条判据；把源码逐处退回"修复前的形状"，对应判据必须红，
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
    # 裸 `v-bind="{a: b}"`（不带参数名）也要扫 —— 第 32 轮 A 用它骗过了闸
    exprs += [m.group(1) for m in re.finditer(r'(?:^|\s)v-bind="(.*?)"', tpl, flags=re.S)]
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
            # `x:` 只有紧跟在 `{`/`,` 后面才是对象字面量的键；`cond ? ghostTern: false` 里
            # 那个 `:` 是三元运算符（第 32 轮 A 用它骗过了这道闸）
            prev = e[:m.start()].rstrip()
            if (e[m.end():m.end() + 1] == ':' and prev.endswith(('{', ','))) \
                    or tok in builtins or tok in locals_:
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
    # 翻页条是同一族漏得更狠的那一半：表体还挂着上一轮的行时，"共 N 条"要改口说"这是旧数据"
    # （第 34 轮把服务真停掉点页面才照出来：观点页 71 行旧数据 + "共 71 条"，一句失败都没有）
    for view in ('posts', 'predictions', 'viewpoints'):
        line = [l for l in html.split('\n') if '第 {{' in l and 'viewErrors.%s' % view in l]
        assert len(line) == 1, '%s 页的翻页条没找到或不止一条（现 %d）' % (view, len(line))
        assert '条数没取到，下面是上一次取到的' in line[0], '%s 页翻页条不承认这是旧数据' % view
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
    # `catch` 只挡"没答话"；200 + success:false 是另一半失败（第 32 轮 A 数出的那一族）
    seg = ls[0][ls[0].index('if (res.data.success)'):ls[0].rindex('} catch')]
    assert 'else {' in seg and '板块映射没取到' in seg, '映射页的 success:false 又静默了'


def test_the_weighted_column_shows_what_it_is_counted_over():
    """加权评分要有自己的基数（第 31 轮 A 残留，任务 #41）。

    "已验证"那一列显示的是**存活**分母（`hit_verified`），而加权评分的分母是
    `total_predictions`（含物理清理归档）。镜像 27 个博主里 18 行两个分母不相等、
    8 行差 >5pp ⇒ 老板看到的是"两列在打脸"。现在分母不同时把基数标在数后面。
    """
    html = _html()
    assert '（基数 {{ b.total_predictions' in html, '加权评分没有自己的分母'
    assert 'b.total_predictions !== b.hit_verified' in html, \
        '基数无条件显示：两列分母一样时会变成噪音'
    assert '标出自己的' in _visible_text(html), '这列的口径说明没写进正文'


def test_first_login_says_the_service_is_waking():
    """任务 #40：第一次输口令也可能撞上唤醒，不能只转"验证中…"。"""
    html = _html()
    start = html.index('v-if="showPasswordModal"')
    # 只看这个弹窗自己：下一处 `modal-overlay` 是"连不上服务"那个，它也写着 serviceWaking，
    # 用固定长度窗口会把两个弹窗混在一起看（第一版就是这么假绿的）
    modal = html[start:html.index('class="modal-overlay"', start + 40)]
    assert 'v-if="serviceWaking"' in modal, '密码弹窗里没有等待唤醒的说明'
    assert "serviceWaking ? '唤醒中…'" in modal, '按钮文案没跟着等待状态走'


def test_no_page_claims_a_number_it_never_measured():
    """第 32 轮两份复评共同指到：规矩③只铺了一半，五处还在报 0 / "没有"。

    - 帖子/预测的分页条就挂在"拉取失败"下面 3 行，无条件写"共 0 条"（镜像 657 帖 / 1616 预测）；
    - 观点洞察四张卡在 `/api/viewpoints/insights` 单独失败时全渲染 0，而表体非空、
      `emptyText` 根本不渲染；
    - 投资建议页与"历史建议"弹窗整页零失败态（镜像 `investment_advice` 实有 43 行）；
    - 配置弹窗"板块别名" tab 失败即"暂无自定义别名"；
    - 映射表体那一行 `暂无数据` 落在顶栏失败守卫之外。
    """
    html = _html()
    # 两条分页条的规矩写在 `test_every_list_page_shares_the_same_honesty_rule` 里（三条腿一起循环），
    # 这里不重复一遍同一条规则 —— 上次两处各写一份，改文案时就只红了一处。
    assert 'numOrDash(viewpointInsights.direction_total)' in html, '洞察卡还在 `|| 0`'
    assert "adviceError || '暂无历史建议'" in html, '历史建议失败时说"没有"'
    assert "aliasError || '暂无自定义别名'" in html, '别名 tab 失败时说"没有"'
    assert "emptyText('mappings')" in html, '映射表体还在无条件"暂无数据"'
    assert '这次没有生成建议' in html, '生成建议被 200+success:false 拒绝时仍然整屏没反应'


def test_polling_gives_up_loudly_instead_of_locking_the_ui():
    """轮询数到上限必须放开全局锁并说一句话（第 32 轮 A-M1：我上一批修出来的回归）。

    `analyzing` 跨 5 个视图禁用 13 个按钮。上一批我把"401/500 不再丢句柄"改对之后，
    耗尽分支既不清 `analyzing` 也不解释 ⇒ 一次 15 分钟以上的服务异常就把整个页面冻住。
    """
    html = _html()
    assert html.count('onPollStalled:') == 2, '两个 manager 都要接停摆回调（现 %d）' % html.count('onPollStalled:')
    assert 'taskStalled[label] =' in html and 'v-if="stallText"' in html, '停摆要有一句话给老板看'
    pm = (PROJECT_ROOT / 'web' / 'post-manager.js').read_text(encoding='utf-8')
    assert 'postAnalysisRunning.value = false;' in pm and 'onPollStalled' in pm, \
        '帖子轮询耗尽后没放开锁'
    vm = (PROJECT_ROOT / 'web' / 'viewpoint-manager.js').read_text(encoding='utf-8')
    assert 'onPollStalled' in vm, '观点轮询耗尽后没有回调'


_NODE_PRELUDE = """
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null),
                       setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
const ref = (v) => ({ value: v });
const reactive = (o) => o;
const computed = (f) => ({ value: null });
const window = { addEventListener: () => {}, removeEventListener: () => {}, setTimeout: () => 0,
                 clearTimeout: () => {}, location: { href: '', search: '' } };
const document = { addEventListener: () => {}, visibilityState: 'visible',
                   querySelector: () => null, createElement: () => ({ style: {} }) };
let impl = null;
const axios = { defaults: { headers: { common: {} } },
                get: async (u, c) => impl(u, c), post: async (u, b) => impl(u, b),
                delete: async (u) => impl(u, null) };
const seen = [];
const isServiceDown = (e) => !!e && !e.response;
%(src)s
const opts = { axios, ref, reactive, computed, localStorage, alert: () => {}, confirm: () => true,
               analyzing: ref(false), onFetchFailure: (k, m) => seen.push([k, m]), isServiceDown,
               viewpoints: ref([]), viewpointDetail: ref(null), showViewpointDetail: ref(false),
               predictions: ref([]), predictionDetail: ref(null), showPredictionDetail: ref(false),
               showEditPrediction: ref(false), editingPrediction: reactive({}) };
"""


def _run_node(src_body, fname):
    """把 `web/<fname>` 那份真实源码接在桩环境后面，交给 node 跑。"""
    src = (PROJECT_ROOT / 'web' / fname).read_text(encoding='utf-8')
    script = _NODE_PRELUDE % {'src': src} + src_body
    p = subprocess.run([NODE, '-e', script], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', cwd=str(PROJECT_ROOT))
    line = [l for l in (p.stdout or '').splitlines() if l.startswith('RESULT')]
    assert line, 'node 没跑出结果：%s' % ((p.stdout or '') + (p.stderr or ''))[-400:]
    return json.loads(line[0][len('RESULT'):])


def _run_manager_js(fname, factory, fetch_name, list_key):
    """把 `web/<fname>` 那份**真实源码**在 node 里跑一遍 `fetch_name`，看三种形状各报了什么。

    第 33 轮 A-MAJOR-2 的教训：上一批我用文本判据声称"失败态由取数成功自己清"，
    而它挂在一条永远不触发的 `watch` 上 —— 文本判据看不见这种事，所以这里执行源码。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    body = """
(async () => {
    const m = %(factory)s(opts);
    const fetch = m['%(fetch_name)s'];
    impl = () => ({ data: { success: true, data: [], meta: { total: 5 } } });
    await fetch();
    impl = () => ({ data: { success: false, message: '清理开关没开' } });
    await fetch();
    impl = () => { throw { message: 'Network Error' }; };
    try { await fetch(); } catch (e) { /* 往上抛是 loadView 的事，这里只看报没报 */ }
    impl = () => ({ data: { success: true, data: [{ id: 1 }], meta: { total: 5 } } });
    await fetch();
    console.log('RESULT' + JSON.stringify({ seen, n: m['%(list_key)s'].value.length }));
})();
""" % {'factory': factory, 'fetch_name': fetch_name, 'list_key': list_key}
    return _run_node(body, fname)


def test_failure_state_is_reported_and_cleared_by_the_fetch_itself():
    """列表页的取数点要自己把"没取到 / 取到了"报给页面，而不是靠一条 watcher 猜。

    上一批写的是 `watch(() => postMeta.value?.total, ...)`：`postMeta` 是 `reactive()` 出来的，
    `postMeta.value` 恒为 undefined ⇒ 回调永不触发，翻页失败后页面继续报上一页"共 N 条"
    （第 33 轮 A-MAJOR-2 用 node 实测把它照出来）。现在跑真实源码看三种形状。
    """
    for fname, factory, fetch_name, key in (
            ('post-manager.js', 'window.createPostManager', 'fetchPosts', 'posts'),
            ('prediction-manager.js', 'window.createPredictionManager', 'fetchPredictions', 'predictions')):
        out = _run_manager_js(fname, factory, fetch_name, key)
        seen = [v for _k, v in out['seen']]
        assert len(seen) == 4, '%s 只报了 %d 次，四种形状各该一次：%s' % (fname, len(seen), out['seen'])
        assert seen[0] == '', '取到数据时不该留着失败说明：%s' % out['seen']
        assert '没取到' in seen[1] and '清理开关没开' in seen[1], \
            '%s 的 200 + success:false 仍然静默：%s' % (fname, seen[1])
        assert '拉取失败' in seen[2], '%s 抛错时（断网/唤醒失败）没报原因：%s' % (fname, seen[2])
        assert seen[3] == '', '%s 再取成功却没把失败说明清掉：%s' % (fname, seen[3])
    html = _html()
    assert 'onFetchFailure: (key, msg) => { viewErrors[key] = msg; }' in html, '页面没接这个回调'
    assert 'if (!viewErrors[key])' in html, 'loadView 的兜底会把取数点自己报的话盖掉'



def test_the_fund_view_says_so_when_the_api_says_no():
    """基金页与它头上的三个数以前只认 `catch`：200 + success:false 时整页照旧。

    第 32 轮 A 数到 16 处 `if (res.data.success)` 没有 else。基金页是其中最有欺骗性的
    一处：列表停在上一页、三个筛选按钮继续报"本页有几只"，而接口明明回了"失败"。
    另：筛选按钮括号里的数一直是**本页**（每页 100 只）的，与下方"共 N 只"不是同一口径。
    """
    html = _html()
    body = _body(html, 'fetchFunds = async () =>')
    assert 'else {' in body and '基金列表没取到' in body, 'fetchFunds 又只剩一条 catch 腿'
    assert "res.data.message || '接口未给出原因'" in body, '失败原因没带上接口的原话'
    assert html.count("fundError || fundLoading ? '—'") == 3, \
        '三个筛选按钮里应有 3 处在失败/加载中报"—"（现 %d）' % html.count("fundError || fundLoading ? '—'")
    assert '本页' in _visible_text(html) and '不是全库' in _visible_text(html), \
        '"本页 vs 全库"这个口径只活在代码里，页面上看不见'


def test_a_destructive_button_cannot_outlive_its_own_preview():
    """预览取不到时必须把"执行清理"的开关关掉（它是删数据用的）。

    `fetchRetentionPreview` 的 catch 早就这么做了，但 `success:false` 那条腿没有 else
    ⇒ 上一轮预览成功留下的 `cleanupEnabled=true` 会继续放行删除，而页面显示的数已经变了。
    """
    html = _html()
    for decl, err in (('fetchRetentionPreview = async () =>', 'retentionPreviewError'),
                      ('fetchCleanupPreview = async () =>', 'cleanupPreviewError')):
        body = _body(html, decl)
        assert 'else {' in body, '%s 的 success:false 没人接' % decl
        # 只看 else 那一段：截到函数末尾会让 `catch` 里的两行替 else 说话（第 33 轮变异实测）
        i = body.index('else {')
        seg = body[i:i + body[i:].index('\n                        }')]
        assert 'cleanupEnabled.value = false;' in seg and err in seg, \
            '%s 的失败分支没关删除开关或没报错' % decl


def test_the_config_modal_says_which_tab_failed():
    """配置弹窗两个 tab 以前失败即整块空白（`v-if="configTab === 'llm' && configData"`）。

    老板点"系统配置"看到一片白，不知道是没配过还是没取到；后者要去改的东西不一样。
    """
    html = _html()
    assert "v-if=\"configTab === 'llm' && configError\"" in html, 'LLM tab 没有失败态'
    assert 'v-if="testDataError"' in html, '测试数据区没有失败态'
    assert html.count('重新加载') >= 1 and '重新扫描' in html, '失败态要能一键再取一次'
    for decl, err in (('loadConfig = async () =>', 'configError'),
                      ('loadTestData = async () =>', 'testDataError')):
        head = _expr(html, 'const ' + decl)
        assert 'else {' in head and err in head, '%s 还是一次 200 失败就静默' % decl


def test_the_top_blogger_modal_puts_its_calibers_in_text_not_hover():
    """TOP 榜弹窗：口径要能看见，"已验证"不许拿另一个分母顶。

    第 30/31 轮四条判据都说过"手机没有 hover"，但弹窗那版还留着 `:title="命中率 = …"`；
    而"已验证"列在 `hit_verified` 缺失时回落到 `total_predictions`（含已删帖的口径），
    等于挂着头图的名换了一个分母。
    """
    html = _html()
    i = html.index('v-if="topBloggers.length > 0"')
    modal = html[i - 2600:i + 1600]
    assert '命中率（判对 / 已验证）' in _visible_text(modal), '弹窗命中率口径没写在表头'
    assert '加权评分（含归档，非命中率）' in _visible_text(modal), '弹窗加权评分没标口径'
    assert ':title="b.hit_rate' not in modal, '关键口径又退回 hover 才看得见'
    assert 'b.hit_correct' in modal and "numOrDash(b.hit_verified)" in modal, \
        '命中率没有分子分母 / "已验证"还能回落到别的分母'
    assert 'topNote' in modal, '接口自己的 metric_note 没渲染'


def test_a_failed_insights_call_takes_the_four_cards_down_with_it():
    """洞察失败时四张卡必须一起变 `—`：红字说"没取到"、卡上却挂着上一轮的 3/2/0 是两句假话。

    第 34 轮浏览器实测（把服务停掉再点页面）抓到：我上一批只给"待汇总"那张卡加了
    `insightsLoaded` 守卫，另外三张还在读没被清空的 `viewpointInsights`，
    而注释里写着"取不到就不许再摆上一轮的数"。这条判据跑真实源码，四种形状各看一次卡面。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    body = """
(async () => {
    const m = window.createViewpointManager(opts);
    const dash = (v) => (v === undefined || v === null) ? '—' : v;
    const cards = () => {
        const i = m.viewpointInsights.value || {};
        return [dash(i.direction_total), dash(i.directions && i.directions.bullish),
                dash(i.directions && i.directions.bearish),
                (m.insightsLoaded.value && i.pending_summary) ? '有' : '—',
                m.insightsError.value];
    };
    const GOOD = { data: { success: true, data: { direction_total: 9, directions: { bullish: 5, bearish: 1 },
                                                   pending_summary: [{ count: 2 }] } } };
    const failings = [
        { data: { success: false, message: '服务忙' } },
        { data: { success: true, data: null } },
        'throw',
    ];
    const on = (payload) => (u) => (u.indexOf('/insights') < 0
        ? { data: { success: true, data: [], meta: { total: 5 } } }
        : (payload === 'throw' ? (() => { throw { message: 'Network Error' }; })() : payload));
    const out = [];
    for (const s of failings) {
        // 每种失败形状都**先跑一次成功**再跑它：三种形状连着跑的话，前一种已经把卡清空了，
        // 后面那两种就算什么都没做也照样"看着对"（第 34 轮变异体检就是这么抓到我这行的）
        impl = on(GOOD);
        await m.loadViewpoints();
        const before = cards();
        impl = on(s);
        await m.loadViewpoints();
        out.push([before, cards()]);
    }
    console.log('RESULT' + JSON.stringify(out));
})();
"""
    out = _run_node(body, 'viewpoint-manager.js')
    assert len(out) == 3, '三种失败形状各该一组卡面：%s' % out
    for failing, (before, after) in zip(('success:false', 'success:true 无 data', '抛错'), out):
        assert before[:4] == [9, 5, 1, '有'], '%s：先跑成功这一笔就没拿到真数 %s' % (failing, before)
        assert after[:4] == ['—', '—', '—', '—'], \
            '%s 这一种失败形状下卡面还摆着上一轮的数：%s' % (failing, after)
    assert '没取到' in out[0][1][4] and '服务忙' in out[0][1][4], out[0][1][4]
    assert '没取到' in out[1][1][4], '接口回了 success 但没带数据，也不能算"取到了"：%s' % out[1][1][4]
    assert '拉取失败' in out[2][1][4], out[2][1][4]


def test_the_insight_cards_cannot_report_zero_before_the_insights_arrive():
    """四张洞察卡里"待汇总"那张以前是死分支：初值 `pending_summary: []` 恒真 ⇒ 没取到也报 0。

    第 33 轮 B-MAJOR-7 用 node 实测：`/api/viewpoints/insights` 还没回来（或单独失败）时，
    卡片渲染的是 0，而表体有 71 条观点 —— "0 条待汇总"是一句编的话。
    现在要 `insightsLoaded` 参与判断，并且洞察单独失败时要在卡下说一句原因。
    """
    html = _html()
    vm = (PROJECT_ROOT / 'web' / 'viewpoint-manager.js').read_text(encoding='utf-8')
    assert 'const insightsLoaded = ref(false);' in vm, '没有"到底取到没取到"这个事实'
    body = _body(vm, 'fetchInsights = async () =>', close='\n        };')
    assert 'insightsLoaded.value = true;' in body, '成功时没登记"已取到"'
    assert 'else {' in body and '观点洞察没取到' in body, '洞察的 success:false 又没人接'
    assert 'catch (error)' in body and '观点洞察拉取失败' in body, '洞察单独失败时只剩 console.error'
    card = [l for l in html.split('\n') if '待汇总' in l][0]
    assert 'insightsLoaded &&' in card, '那张卡还是把"没取到"当 0 报'
    assert '上面四张卡的数是"没取到"' in _visible_text(html), '失败原因没写在卡片旁边'


def test_a_failed_preview_explains_why_the_clean_up_buttons_are_held():
    """预览取不到 ⇒ 删除按钮被按住，但页面上要有一句人话解释（第 33 轮 A-MINOR-8）。"""
    html = _html()
    i = html.index('v-if="retentionPreviewError" class="action-btn small"')
    seg = html[i:i + 1400]
    assert 'retentionPreviewError || cleanupPreviewError' in seg, '只有一个预览失败时会静默按住按钮'
    assert '不能凭上一轮的预览数' in _visible_text(seg), '按钮为什么按住了，页面上没话说'


def test_the_stall_banner_is_taken_down_when_polling_recovers():
    """`taskStalled` 不能一次停摆就永挂（第 33 轮 A-MINOR-10 / B-MINOR-10）。"""
    html = _html()
    assert 'taskStalled[label] =' in html and 'v-if="stallText"' in html, '停摆要有一句话给老板看'
    assert html.count("onPollRecovered: (label) => { delete taskStalled[label]; },") == 2, \
        '两个 manager 都要接"恢复了就把这一条擦掉"（现 %d）' % html.count('onPollRecovered')
    assert 'const taskStalled = reactive({});' in html, '横幅必须按任务分槽（一条恢复不许擦另一条的警告）'
    assert 'const stallText = computed(' in html, '分槽之后要有一个人读的汇总话术'


def test_a_button_must_not_claim_the_opposite_of_what_happened():
    """删除/保存/归档成功、只是刷新失败时，不许弹「失败」（第 34 轮 B-MAJOR-5）。

    这仓库为"按钮说假话"付过账（一个按钮清掉 515 条结论那次）。同一族里还有一条轻的：
    翻页失败后页码已经加过去了，页面会出现「第 4 页」配第 3 页的数据（B-MINOR-15）。
    """
    pm = (PROJECT_ROOT / 'web' / 'post-manager.js').read_text(encoding='utf-8')
    vm = (PROJECT_ROOT / 'web' / 'viewpoint-manager.js').read_text(encoding='utf-8')
    pdm = (PROJECT_ROOT / 'web' / 'prediction-manager.js').read_text(encoding='utf-8')
    for name, src in (('post-manager.js', pm), ('prediction-manager.js', pdm)):
        assert '只是列表没刷新出来' in src, '%s 的写操作还是把"刷新失败"说成"操作失败"' % name
    for src in (pm, vm, pdm):
        assert re.search(r'catch \(error\) \{ \w+Filters\.page = back; \}', src), \
            '翻页失败没把页码退回去'
    html = _html()
    assert '博主已添加，只是列表没刷新出来' in html and "没加上：" in html, \
        '管理博主弹窗仍然把"已加上"说成"添加失败"'
    assert 'try { await fetchPosts(); await fetchPredictions(); await fetchStats(); }' in html, \
        '微信批量抓取的收尾刷新没包住：一失败「抓取中…」就永久卡住'


def test_the_top_modal_says_who_is_excluded():
    """TOP 榜空列表不等于"没有博主"：门槛是"至少 5 条已验证结论"（第 34 轮 B-MAJOR-3）。"""
    html = _html()
    i = html.index('v-if="topBloggers.length > 0"')
    tail = html[i:i + 6000]
    assert '至少 5 条已验证结论' in _visible_text(tail), '空榜被渲染成"暂无数据"'
    line = [l for l in tail.split('\n') if '至少 5 条已验证结论' in l][0]
    assert line.lstrip().startswith('<div v-else'), '空榜说明必须和表格互斥渲染，否则有数据时也在解释"为什么没人上榜"'
    assert ':title=' not in line, '门槛又跑回只有 hover 才看得见的地方'


def test_the_wiring_gate_covers_both_directions():
    """接线闸必须两头都在（第 34 轮：解构到 undefined、options 漏注入，两种都真实发生过）。"""
    gate = (PROJECT_ROOT / 'tests' / 'unit' / 'test_frontend_wiring.py')
    assert gate.exists(), '接线闸文件不见了'
    src = gate.read_text(encoding='utf-8')
    assert 'def test_everything_the_page_destructures_is_actually_exported' in src
    assert 'def test_every_option_the_manager_reads_is_actually_injected' in src
    html = _html()
    for factory in ('createPostManager', 'createViewpointManager', 'createPredictionManager'):
        i = html.index('window.%s(' % factory)
        seg = html[i:i + 900]
        assert 'isServiceDown' in seg and 'onFetchFailure' in seg, '%s 的 options 少了这两样' % factory
    for fname in ('post-manager.js', 'viewpoint-manager.js'):
        src = (PROJECT_ROOT / 'web' / fname).read_text(encoding='utf-8')
        assert 'options.onPollRecovered' in src, '%s 轮询成功后不收那句话' % fname
