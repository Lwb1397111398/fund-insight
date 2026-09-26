# -*- coding: utf-8 -*-
"""冷启动与"取不到数据"时的前端诚实性（任务 #27 / #26 / #30 / #32 的页面那一半）。

判据形态：这个仓库测前端的方式就是扫 `web/index.html` 文本（`test_frontend_loading.py`
一系都是这么写的）。**文本断言的天花板很低**，第 29 轮两份复评当场证给我看：
- 一条 `'「」' not in text` 是恒真的 —— 渲染出来的空格子来自 `{{ }}` 插值，
  而字面 `「」` 只出现在注释里，两者永不相等；
- 一条 `<th[^>]*>` 把属性整个吃掉，于是 `not any('title=' in h)` 结构上不可能响。
所以下面每条判据都配了一个**可复跑的变异**：`python scripts/mutation_proof_frontend.py`
（处数与判据数**不要写在这里**，跑 `--list` 看末行；把源码逐处退回"修复前的形状"，对应判据必须红，
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
    # 第 38 轮 A 席 MAJOR-1：上一版写死 `<div class="value">`，而帖子页那 4 张迷你卡用的是
    # `<span class="value">` ⇒ 判据**结构上看不见它们**（同屏表体正在老实说"列表没取到"）。
    cards = re.findall(r'<(?:div|span) class="value">\{\{((?:[^{}])*?)\}\}(?:</div>|</span>)',
                       html, flags=re.S)
    assert len(cards) >= 10, '数字卡只扫到 %d 张（div+span 两个形状），正则或结构变了' % len(cards)
    assert all('statVal(' in c for c in cards if 'stats.overall' in c), \
        ['还是 `|| 0` 形态的卡：%s' % c for c in cards if 'stats.overall' in c and 'statVal(' not in c]
    # 第 6 张"待清理"卡不读 `stats.overall`，上一版判据对它结构上不可能响（第 30 轮两份复评同点）。
    # 注意不能写成"'|| 0' 且没有 '?'"——`retentionPreview?.total || 0` 里的 `?.` 会把它糊过去。
    # 只管**会报出一个数**的卡（读 stats/ meta / 预览计数）；历史建议那两张 `<span class="value">`
    # 印的是日期与表情文本，它们由建议列表自己的空态/失败态守着，不归这条判据。
    numeric = lambda c: ('stats.overall' in c or 'Meta.' in c or 'retentionPreview' in c
                         or 'Count' in c or 'count' in c)
    unguarded = [c.strip() for c in cards if numeric(c) and 'statVal(' not in c and "'—'" not in c]
    assert not unguarded, '这些卡把"没取到"报成 0：%s' % unguarded
    assert sum(1 for c in cards if numeric(c)) >= 10, \
        '带数的卡只剩 %d 张 ⇒ 扫描面又缩回去了（帖子页 4 张 span 卡曾被漏看，第 38 轮 A-MAJOR-1）' % (
            sum(1 for c in cards if numeric(c)))
    assert 'statsError' in _body(html, 'fetchStats = async () =>'), 'fetchStats 不记失败原因'


def test_the_three_prediction_queues_explain_themselves_without_hover():
    """「待验证」与「待验证到期」是两个不同队列，区别必须能在正文里读到（第 38 轮 A-MINOR-6）。

    规矩④/③说过"关键信息不许只活在 `title` 里 —— 手机没有 hover"，而 title 闸当时只扫 `<th>`
    与 TOP 弹窗，按钮一个都不管：选中错的按钮＝今天白跑一批验证（那 141 条到期未判在后者）。
    """
    html = _html()
    text = _visible_text(html)
    assert '已到目标日、仍在验证窗口内' in text, '「待验证到期」的定义只在 title 里'
    assert '别把它当成' in text, '「待验证」含观望/未到期这件事没写进正文'
    assert 'class="filter-caliber-note"' in html, '这行说明没有自己的样式类（会被当成临时文案删掉）'
    css = (PROJECT_ROOT / 'web' / 'common.css').read_text(encoding='utf-8')
    assert '.filter-caliber-note' in css


def test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have():
    """帖子页 4 张迷你卡 + 预测页 8 个筛选按钮的括号数：取不到 / 还没取到时报 `—`。

    第 38 轮 A 席 MAJOR-1：这两页是老板最常用的两个列表，而它们把 `postMeta.total || 0`
    直接印在卡上 —— 唤醒期进预测页就是"全部 (0) / 待验证到期 (0) / 回收站 (0)"（镜像真值
    1616 条活预测），同屏表体却在老实说"列表没取到"。上一版判据看不见它们（见上一条的正则注释）。
    """
    html = _html()
    minis = re.findall(r'<span class="value">\{\{((?:[^{}])*?)\}\}</span>', html, flags=re.S)
    posts_cards = [c for c in minis if 'postMeta' in c]
    assert len(posts_cards) >= 4, '帖子页迷你卡只扫到 %d 张' % len(posts_cards)
    for c in posts_cards:
        assert 'viewErrors.posts' in c and 'numOrDash(' in c, '帖子页这张卡还会报 0：%s' % c.strip()

    buttons = re.findall(r'\(\{\{((?:[^{}])*?)\}\}\)', html, flags=re.S)
    faceted = [b for b in buttons if 'predictionMeta.facets' in b]
    assert len(faceted) >= 8, '预测页按钮括号数只扫到 %d 个（应为 8）' % len(faceted)
    for b in faceted:
        assert 'viewErrors.predictions' in b and 'numOrDash(' in b, \
            '这个按钮把"没取到"报成 0：(%s)' % b.strip()

    # 初值这一侧也要钉：模板守住了、初值退回 0 的话，首屏仍然是一片假的 0
    post_js = (PROJECT_ROOT / 'web' / 'post-manager.js').read_text(encoding='utf-8')
    pred_js = PREDICTION_JS.read_text(encoding='utf-8')
    assert 'total: null' in post_js, 'postMeta.total 初值又退回 0 —— 首屏那一次渲染没人替它说话'
    assert 'total: null' in pred_js and 'all: null' in pred_js and 'total: 0' not in pred_js


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
        assert '条数没取到，表里是上一次取到的' in line[0], '%s 页翻页条不承认这是旧数据' % view
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
let wake = 0;      // 记"列表取数有没有先问唤醒门"，见 test_every_list_fetch_point_asks_the_wake_gate
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
    const opts2 = Object.assign({}, opts, { withWakeRetry: async (fn) => { wake += 1; return await fn(); } });
    const m = %(factory)s(opts2);
    const fetch = m['%(fetch_name)s'];
    impl = () => ({ data: { success: true, data: [], meta: { total: 5 } } });
    await fetch();
    impl = () => ({ data: { success: false, message: '清理开关没开' } });
    await fetch();
    impl = () => { throw { message: 'Network Error' }; };
    try { await fetch(); } catch (e) { /* 往上抛是 loadView 的事，这里只看报没报 */ }
    impl = () => ({ data: { success: true, data: [{ id: 1 }], meta: { total: 5 } } });
    await fetch();
    console.log('RESULT' + JSON.stringify({ seen, n: m['%(list_key)s'].value.length, wake }));
})();
""" % {'factory': factory, 'fetch_name': fetch_name, 'list_key': list_key}
    return _run_node(body, fname)


def test_every_list_fetch_point_asks_the_wake_gate_before_giving_up():
    """三个列表取数点（帖子/预测/观点）都得先问 `withWakeRetry`，不许裸 `axios.get`。

    这条就是任务 #58 欠的那一半：AGENTS 一直写着"取数点分两类钉，进视图才打的也要过
    `withWakeRetry`"，而实测 `post-manager.js` / `viewpoint-manager.js` 里它是 **0 处**、
    `prediction-manager.js` 只有 2 处且只用在 `verify-all/status` ⇒ Render 睡着时这三个列表
    要老板自己再点一次，而另外五个取数点会自己等 90 秒。
    判据是**跑真实源码数调用**：把 `wake(() => axios.get(...))` 改回裸 `axios.get(...)`，
    `wake` 就变 0 ⇒ 红（不是 grep 到关键字就算）。
    """
    for fname, factory, fetch_name, key in (
            ('post-manager.js', 'window.createPostManager', 'fetchPosts', 'posts'),
            ('prediction-manager.js', 'window.createPredictionManager', 'fetchPredictions', 'predictions'),
            ('viewpoint-manager.js', 'window.createViewpointManager', 'fetchViewpoints', 'viewpoints')):
        out = _run_manager_js(fname, factory, fetch_name, key)
        assert out['wake'] >= 4, '%s 的列表取数没走唤醒门（四次 fetch 只记到 %s 次）' % (
            fname, out.get('wake'))
    html = _html()
    for factory in ('createPostManager', 'createViewpointManager', 'createPredictionManager'):
        at = html.index(factory + '({')
        block = html[at:html.index('});', at)]
        assert 'withWakeRetry' in block, '页面构造 %s 时没把唤醒门递进去' % factory


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


def test_the_summary_button_does_not_claim_there_is_nothing_to_summarize():
    """「汇总观点」这个按钮：统计没取到时，不许说"没有待汇总的观点"（第 36 轮 A-MAJOR-1）。

    上一批我把 `summaryStatsLoaded` / `summaryStatsError` 写进了 manager，页面话术也确实分了三档，
    但 `grep -rn summaryStats tests/` = 0 —— **除了体检里那几个锚点字符串，没有任何判据读过这两列**。
    A 在副本里把话术退回 `alert('没有待汇总的观点')`、又把 `fetchSummaryStats` 退回"只写成功不清旧值"，
    32 条判据全绿。这里补上真判据：跑的是 `web/viewpoint-manager.js` 那份源码，
    三种失败形状**各自先跑一次成功**（否则前一种已经把状态清了，后几种是空的）。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    body = """
(async () => {
    const alerts = [];
    opts.alert = (m) => alerts.push(m);
    const m = window.createViewpointManager(opts);
    const GOOD = { data: { success: true, data: { total_pending_viewpoints: 3 } } };
    const ZERO = { data: { success: true, data: { total_pending_viewpoints: 0 } } };
    const failings = [
        { data: { success: false, message: '统计接口挂了' } },
        { data: { success: true, data: null } },
        'throw',
    ];
    const route = (payload) => (u) => {
        if (u.indexOf('/summary/stats') >= 0) {
            return payload === 'throw' ? (() => { throw { message: 'Network Error' }; })() : payload;
        }
        return { data: { success: true, data: [], meta: { total: 5 } } };
    };
    const out = [];
    for (const s of failings) {
        impl = route(GOOD);                 // 先成功一笔：让"取到了"这件事真实发生过
        await m.showSummaryModal();
        const taken = alerts.splice(0, alerts.length);
        impl = route(s);                    // 再跑这一种失败形状
        await m.showSummaryModal();
        out.push([taken, alerts.splice(0, alerts.length),
                  { loaded: m.summaryStatsLoaded.value, err: m.summaryStatsError.value }]);
    }
    // 最后再看一次"确实取到、且为 0"：这时才允许说"没有"
    impl = route(ZERO);
    await m.showSummaryModal();
    out.push([[], alerts.slice(0), { loaded: m.summaryStatsLoaded.value }]);
    console.log('RESULT' + JSON.stringify(out));
})();
"""
    out = _run_node(body, 'viewpoint-manager.js')
    assert len(out) == 4, '三种失败形状 + 一种"取到且为 0"：%s' % out
    for i, shape in enumerate(('success:false', 'success:true 但没 data', '抛错')):
        first_visit, failing_alerts, state = out[i]
        assert first_visit == [], '先跑成功这一笔该直接开弹窗，不该说任何话：%s' % first_visit
        assert failing_alerts, '%s 这一种形状下按钮一句话都没说' % shape
        said = ' '.join(failing_alerts)
        # 注意不能写成"不许出现『没有待汇总的观点』这几个字"——
        # 诚实的那句本身就引用它（`没敢断定"没有待汇总的观点"：…`）。要判的是**句式**：
        # 必须是"我没敢断定 + 因为没取到/拉取失败"，而不是光秃秃一句结论。
        assert '没敢断定' in said and re.search(r'没取到|拉取失败', said), \
            '%s：统计没取到时这句话没在否认自己的结论：%s' % (shape, said)
        assert state['loaded'] is False, '%s：取不到却把 loaded 记成真：%s' % (shape, state)
    assert '服务忙' not in str(out[2]), out[2]
    assert out[2][2]['err'], '抛错那一档也要留下原因话术：%s' % out[2][2]
    truthy, said_zero, state = out[3]
    assert said_zero == ['没有待汇总的观点'], '真取到且为 0 时该说"没有"：%s' % said_zero
    assert state['loaded'] is True, state


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


def _arrow_fns(src):
    """产出 (函数名, 函数体)：用花括号配对切 `const NAME = async (…) => { … }`。

    第 35 轮 A-M1/M2 的根因不是"某处没改"，是**判据的形态**：旧版只做文件级 substring，
    一个文件里有一处写对了就整文件通过，而且没有任何变异打向它。改成"扫所有写函数"。
    """
    out = []
    for m in re.finditer(r'const (\w+) = (?:async )?\(([^)]*)\) => \{', src):
        i, depth = m.end() - 1, 0
        while i < len(src):
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.group(1), src[m.end():i]))
    return out


_WRITE_RE = re.compile(r'axios\.(?:post|put|delete)\(')
# 第 36 轮 A-M3：以前这里是**白名单**（只认 `fetchX/refreshX/Promise.all/onStatsChanged`），
# 于是把 `await fetchPredictions()` 改名成 `await loadEverything()` 就整条腿隐身、闸不响。
# 判据反过来：**写之后的任何 await 都是一条腿**，白名单反而要写注释说明。
# 但"另一笔写"不算刷新腿：`previewPredictionMaintenance` 那种"三个分支各一笔 post"的形状，
# 腿要的是**读回列表**，不是第二笔写。
_LEG_RE = re.compile(r'await\s+(?!axios\.)(?:[\w.$]+\s*\(|Promise\.all\()')
_TRY_CATCH_RE = re.compile(r'try \{[\s\S]*?\}\s*catch \((\w+)\)')
_LIE_WORDS = re.compile(r'失败|出错|错误')
_TRUTH_PHRASE = re.compile(r'没刷新|刷新页面|重新加载')
# catch 体里这些不算"对老板说的反话"：`console.error('刷新失败')` 是给开发者看的日志、
# `/* 轮询失败忽略 */` 是注释 —— 把它们一起当假话，只会让闸在噪声上响、在真话上哑。
_DEV_NOISE_RE = re.compile(r'console\.\w+\([^)]*\)|//[^\n]*|/\*[\s\S]*?\*/')


def _said_words(catch_body):
    return _DEV_NOISE_RE.sub('', catch_body)


def _brace_body(src, start):
    """从 `{` 起做配对，返回 (体, 结束位置)。"""
    i, depth = start, 0
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start + 1:i], i
        i += 1
    return src[start:], i


def _catch_bodies(text):
    """每个 `try { … } catch (X) { … }` 里 catch 那段话。"""
    out = []
    for m in _TRY_CATCH_RE.finditer(text):
        brace = text.find('{', m.end() - 1)
        if brace >= 0:
            body, _end = _brace_body(text, brace)
            out.append(body)
    return out


def _swallows_only(fn_bodies, name):
    """这个函数自己有没有 catch 且不 re-throw ⇒ `await 它()` 不会 reject，外层 catch 收不到它。

    第 36 轮 #53 的更正：我先前用"刷新腿没单独包 try"就判它说假话，是**只看形状不看 callee**；
    `index.html` 里 `deleteFund` 的 `await fetchFunds()` 就是这样——`fetchFunds` 自己吞错，
    那条腿永远走不到外层 catch。所以闸要按"能不能 reject"判，而不是按"有没有包"判。
    """
    body = fn_bodies.get(name)
    if body is None:
        return False
    return bool(re.search(r'\}\s*catch', body)) and not re.search(r'\bthrow\b', body)


def _scan_write_then_refresh(fname, src, fn_bodies):
    """返回 (带写后刷新的函数数, 违规清单)。"""
    offenders, seen = [], 0
    for name, body in _arrow_fns(src):
        w = _WRITE_RE.search(body)
        if not w:
            continue
        tail = body[w.end():]
        legs = list(_LEG_RE.finditer(tail))
        if not legs:
            continue
        seen += 1
        regions = []
        for m in _TRY_CATCH_RE.finditer(tail):
            brace = tail.find('{', m.end() - 1)
            if brace < 0:
                continue
            inner, end = _brace_body(tail, brace)
            regions.append((m.start(), end + 1, inner))
        for leg in legs:
            callee = re.search(r'await\s+([\w.$]+)\s*\(', tail[leg.start():leg.start() + 120])
            if callee and _swallows_only(fn_bodies, callee.group(1).split('.')[-1]):
                continue                      # 这条腿自己吞错，永远不会惊动外层 catch
            pos = leg.start()
            region = next(((a, b, cb) for a, b, cb in regions if a <= pos < b), None)
            if region is None:
                offenders.append('%s::%s 刷新腿没单独 try（外层 catch 会替一件成功的事说"失败"）'
                                 % (fname, name))
                break
            catch_text = _said_words(region[2])
            if _LIE_WORDS.search(catch_text) and not _TRUTH_PHRASE.search(catch_text):
                # 第 36 轮 A-M2：只要求"结构上包了 try"等于没要求 ——
                # catch 里写着「保存失败: …」的那句假话照样能过。
                offenders.append('%s::%s 刷新腿单独包了，可 catch 里说的是"%s"'
                                 % (fname, name, catch_text.strip()[:40]))
                break
    return seen, offenders


def test_a_write_that_succeeded_is_never_reported_as_a_failure():
    """**每一个**"写成功之后还要刷新"的函数都必须让那条腿单独收口，且 catch 不许说反话。

    覆盖范围第 36 轮起扩到 `web/index.html`（老板真正点的那个文件）。A 手抄它时逐条核过
    26 处写调用"今天不说假话"，那是**人读出来的**；现在由闸自己判"能不能 reject"。
    """
    offenders, seen_mgr, seen_html = [], 0, 0
    files = ('post-manager.js', 'prediction-manager.js', 'viewpoint-manager.js', 'index.html')
    sources = {f: (PROJECT_ROOT / 'web' / f).read_text(encoding='utf-8') for f in files}
    # callee 表要跨整个页面包：`index.html` 里的写函数会去调 manager 里定义的刷新函数
    # （反之亦然）。只看本文件会把"其实吞错了的腿"误判成假话。
    bundle = {}
    for src in sources.values():
        bundle.update(dict(_arrow_fns(src)))
    for fname in files:
        n, bad = _scan_write_then_refresh(fname, sources[fname], bundle)
        offenders += bad
        if fname == 'index.html':
            seen_html = n
        else:
            seen_mgr += n
    assert seen_mgr == 12, \
        ('三个 manager 里"写后刷新"的函数实测 12 个（旧白名单只看见 10 个：'
         'archivePrediction / restorePrediction 的 `await afterChange()` 是白名单漏掉的腿），'
         '现在看见 %d 个 ⇒ 扫描器退化' % seen_mgr)
    assert seen_html >= 19, 'index.html 只看见 %d 个写后刷新的函数 ⇒ 这条扫描已经失效' % seen_html
    assert not offenders, '这些函数会把已成功的写报成失败：\n  %s' % '\n  '.join(sorted(set(offenders)))


class _PreviewButtonScan(HTMLParser):
    """按**祖先链**判"确认执行"这颗按钮活在哪条 v-if 下（第 36 轮 A-MAJOR-4）。

    本机没有 Chromium，"浏览器里看过"只是我的人工动作。这条闸不需要浏览器：
    它读的就是页面里那份模板源码，问一句 Vue 会问的问题 —— 这个元素被渲染的前提是什么。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []            # [(tag, attrs)]
        self.buttons = []          # {'text','conds','depth'}
        self.error_blocks = []     # {'text','clicks','depth'}

    VOID = {'br', 'img', 'input', 'hr', 'meta', 'link', 'source'}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        entry = (tag, a)
        if tag not in self.VOID:          # 空元素没有闭合标签：入栈会把栈撑歪
            self.stack.append(entry)
        if tag == 'button':
            # **祖先链**与"自己的 v-if"要分开存：混在一起判就等于没判 ——
            # 把包裹层从 `maintenancePreview` 改成 `showPredictionMaintenance` 时，
            # 按钮自己的条件里仍然写着 `maintenancePreview.type`，混合判据照样通过（实测 GREEN 过一次）。
            conds = ' | '.join(str(at.get('v-if') or at.get('v-show') or '') for _t, at in self.stack[:-1])
            self.buttons.append({'text': '', 'conds': conds, 'own': str(a.get('v-if') or ''),
                                 'marker': len(self.stack), 'entry': entry})
        if a.get('v-if') == 'maintenanceError':
            self.error_blocks.append({'text': '', 'clicks': [], 'marker': len(self.stack), 'entry': entry})
        for e in self.error_blocks:
            click = a.get('@click') or ''
            if self._open(e) and click.startswith('previewPredictionMaintenance'):
                e['clicks'].append(click)

    def _open(self, rec):
        """这条记录还"张着嘴"吗：栈里那个位置**还是它自己**。

        只比标签名不行 —— 后一颗 `<button>` 会落在同一个槽位上，前一颗就一直在收字
        （实测扫出过"取消扫描重复候选…确认执行"这种串起来的假按钮名）。
        """
        return len(self.stack) >= rec['marker'] and self.stack[rec['marker'] - 1] is rec['entry']

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        for b in self.buttons:
            if self._open(b):
                b['text'] += text
        for e in self.error_blocks:
            if self._open(e):
                e['text'] += ' ' + text

    def handle_endtag(self, tag):
        # 只弹栈，不清 `self.buttons`：按钮记录留着，出栈后它的 `depth != len(stack)`，
        # 自然就不再收字了。（旧版在这里过滤按钮记录，一个空元素把栈撑歪过一次就全清了。）
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_the_execute_button_cannot_outlive_its_own_preview():
    """「确认执行」必须活在 `maintenancePreview` 这条 v-if 底下 —— 用祖先链判，不是数子串。

    第 35 轮我加的那道"预览失败就不许有可确认的东西"，判据是**行为**（node 跑源码）。
    话术里那句「没有可信的预览，"确认执行"不会出现」以前只是我在**描述**这段模板长什么样：
    A 把 `v-if="maintenancePreview"` 改成 `v-if="showPredictionMaintenance"`（按钮脱离预览），
    再把红字里那句说明删掉 —— 全仓不红。现在这两个形状各对应一条断言、各配一处变异。
    """
    html = (PROJECT_ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    scan = _PreviewButtonScan()
    scan.feed(html)
    executors = [b for b in scan.buttons if b['text'].strip() == '确认执行']
    assert executors, '模板里找不到"确认执行"那颗按钮 ⇒ 扫描器或模板变了形状'
    for b in executors:
        assert 'maintenancePreview' in b['conds'], \
            '"确认执行"现在挂在 %s 底下 —— 脱离预览就是"没有可信清单也能真写"' % b['conds']

    assert scan.error_blocks, '红字那一行（v-if="maintenanceError"）没被扫到 ⇒ 它被删了或改了形状'
    line = scan.error_blocks[0]
    assert '重新预览' in line['text'], '取不到预览时要给出路：那行得有「重新预览」：%s' % line['text'][:90]
    assert line['clicks'], '「重新预览」必须真去重新预览（@click=previewPredictionMaintenance）'
    assert '不会出现' in line['text'], \
        '那行还得把"为什么红色按钮不见了"说明白：%s' % line['text'][:90]


def test_a_failed_preview_leaves_nothing_to_confirm():
    """预览取不到 ⇒ 手里那张"确认执行"必须一起消失（因果，不是文案）。

    第 35 轮 A-M3：`previewPredictionMaintenance` 以前既不查 `success`（后端很多"被护栏按住"
    走的就是 200 + success:false），抛错时也不清 `maintenancePreview` —— 于是红色按钮
    活在上一轮的预览上，老板按下去执行的是他没看到的那份清单。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    body = """
(async () => {
    const m = window.createPredictionManager(opts);
    const seen = [];
    const shapes = [
        { data: { success: false, message: '清理开关没开' } },
        'throw',
        { data: { success: true, data: { would_rollback: 3 }, message: '预览：3 条' } },
        { data: { message: '这份回执压根没带 success 字段' } },
    ];
    for (const s of shapes) {
        impl = (u) => (s === 'throw' ? (() => { throw { message: 'Network Error' }; })() : s);
        m.maintenancePreview.value = { type: 'rollback', message: '上一轮的旧预览', data: { would_rollback: 99 } };
        await m.previewPredictionMaintenance('rollback');
        seen.push([m.maintenancePreview.value ? m.maintenancePreview.value.message : null,
                   m.maintenanceError.value]);
    }
    console.log('RESULT' + JSON.stringify(seen));
})();
"""
    out = _run_node(body, 'prediction-manager.js')
    assert out[0][0] is None, '后端回 200 + success:false，预览却还是"可执行"状态：%s' % out[0]
    assert '没开' in out[0][1] or '拒绝' in out[0][1], out[0][1]
    assert out[1][0] is None, '预览抛错之后红色按钮还活在上一轮的清单上：%s' % out[1]
    assert '失败' in out[1][1], out[1][1]
    assert out[2][0] == '预览：3 条', '真正取到的预览要留下：%s' % out[2]
    assert out[2][1] == '', out[2][1]
    # 第 36 轮 A-MINOR-3：回执**压根没有 `success` 字段**也是"没成功"，不能武装红色按钮
    assert out[3][0] is None, '接口没回 success 字段却当成预览成功：%s' % out[3]
    assert 'success:true' in out[3][1] or '没' in out[3][1], out[3][1]


def test_a_button_must_not_claim_the_opposite_of_what_happened():
    """翻页失败要退页码；几个说人话的位置必须还在（第 34 轮 B-MAJOR-5 / B-MINOR-11/15）。"""
    pm = (PROJECT_ROOT / 'web' / 'post-manager.js').read_text(encoding='utf-8')
    vm = (PROJECT_ROOT / 'web' / 'viewpoint-manager.js').read_text(encoding='utf-8')
    pdm = (PROJECT_ROOT / 'web' / 'prediction-manager.js').read_text(encoding='utf-8')
    for name, src in (('post-manager.js', pm), ('prediction-manager.js', pdm), ('viewpoint-manager.js', vm)):
        assert '只是列表没刷新出来' in src, '%s 的写操作还是把"刷新失败"说成"操作失败"' % name
    # 翻页函数逐个查（**不是文件级 substring**：一个文件里两处翻页只改对一处，
    # 文件级判据就放行 —— 第 35 轮 A 就是这样把我上一条"已修"打回的，三处变异当时全 GREEN）
    pagers = 0
    for fname, src in (('post-manager.js', pm), ('prediction-manager.js', pdm), ('viewpoint-manager.js', vm)):
        assert 'return false;' in src, '%s 的列表取数函数没把"软失败"告诉调用方' % fname
        for name, body in _arrow_fns(src):
            if 'Filters.page = back' not in body:
                continue
            pagers += 1
            assert re.search(r'=== false\) \w+Filters\.page = back;', body), \
                '%s::%s 只在抛错时退页码，200 + success:false 时页码还挂着' % (fname, name)
            assert re.search(r'catch \(error\) \{ \w+Filters\.page = back; \}', body), \
                '%s::%s 抛错那一腿没把页码退回去' % (fname, name)
    assert pagers >= 6, '只看见 %d 个翻页函数 ⇒ 这条闸的扫描失效了' % pagers
    html = _html()
    assert '博主已添加，只是列表没刷新出来' in html and "没加上：" in html,         '管理博主弹窗仍然把"已加上"说成"添加失败"'
    assert 'try { await fetchPosts(); await fetchPredictions(); await fetchStats(); }' in html,         '微信批量抓取的收尾刷新没包住：一失败「抓取中…」就永久卡住'


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


def test_the_evidence_line_does_not_keep_yesterdays_report_on_a_failed_refresh():
    """/api/stats/evidence 失败时必须把**上一轮的报告**放下（第 40 轮 A 席 MAJOR-1）。

    模板是 `v-if="evidenceReport"` ⇒ 旧报告不清，第 98-101 行那句"证据体检拉取失败、
    先别当结论用"结构上永远渲染不出来，页面上继续印着
    "已判 1110 / ⚠ 197 / 区间 43.96%~61.71% · 本地镜像库 · 截至 …"。
    触发路径不是理论：`fetchStats()` 每次都顺带刷 evidence，而每次写操作后都刷 stats，
    唤醒期一次 502 就命中。跑的是 index.html 里那份真源码；
    并且按第 34 轮的推论，**每种失败形状前都先跑一次成功**，否则后几种是空的。
    """
    html = _html()
    body = _decl(html, 'fetchEvidence = async () =>')
    assert 'evidenceReport.value = res.data.data' in body, '取数点被改了形 ⇒ 这条判据要重核'
    helpers = _wake_helpers(html) + [_expr(html, 'const spanText'),
                                     _decl(html, 'fetchEvidence = async () =>')]
    good = {'data': {'success': True, 'data': {'span_low_pct': 43.96, 'span_high_pct': 61.71,
                                               'judged_rows': 1110, 'stale_rows': 197}}}
    shapes = [{'data': {'success': False, 'message': '证据体检返回失败'}},
              {'data': {'success': True, 'data': None}},
              'throw']
    snippet = ("""
(async () => {
    const GOOD = %s;
    const failings = %s;
    const snap = () => ({ report: evidenceReport.value ? 'in' : 'out',
                          span: spanText(), err: evidenceError.value });
    const others = (u) => (u !== '/api/stats/evidence' ? { data: { success: true, data: [] } } : null);
    const out = [];
    for (const s of failings) {
        impl = (u) => others(u) || GOOD;
        await fetchEvidence();
        const before = snap();
        impl = (u) => {
            const other = others(u); if (other) return other;
            if (s === 'throw') throw { message: 'Network Error' };
            return s;
        };
        await fetchEvidence();
        out.push([before, snap()]);
    }
    console.log(JSON.stringify(out));
})();
""" % (json.dumps(good), json.dumps(shapes)))
    out = _run_page_js(snippet.replace("'RESULT' + JSON.stringify", '"RESULT" + JSON.stringify', 1), helpers)
    assert isinstance(out, list) and len(out) == 3, out
    for before, after in out:
        assert before['report'] == 'in' and before['span'] == '43.96%~61.71%', \
            '成功那一轮没先把报告摆上 ⇒ 后面几种形状是空的（第 34 轮的老错）：%s' % before
        assert after['report'] == 'out', '失败后旧区间还在页上：红字永远出不去（%s）' % after
        assert after['span'] == '-', after
        assert after['err'], '失败却没写可见原因：' + str(after)


def test_the_audit_counters_hang_up_when_the_list_was_not_fetched():
    """四个体检筛选按钮读的是**上一轮的行数**（第 40 轮 A 席 m6）。

    `loadSectorMappings` 失败只写 `viewErrors.mappings`、不清 `sectorMappings`，
    而表体自己在 `v-if/v-else` 里已经换成"拉取失败"——同屏一边说失败、一边继续报
    "体检不可服务 N / 名册无对口 N"，等于把昨天的数当今天的用。
    """
    html = _html()
    buttons = re.findall(r'<button v-if="([^"]*)" class="action-btn small" '
                         r'@click="identityFilter', html)
    assert len(buttons) == 4, '体检按钮只扫到 %d 个（应为 4）⇒ 结构变了，这条要重核' % len(buttons)
    for cond in buttons:
        assert 'viewErrors.mappings' in cond, '这个按钮还在摆上一轮的计数：%s' % cond


def _element_spans(tpl):
    """模板里每个元素的 (attrs, start, end)。

    属性正则必须是 `(?:"[^"]*"|[^>"])*` —— 用 `[^>]*` 会在 `v-if="a.length > 0"` 的
    引号内被 `>` 截断，整段区间就塌了（第一版我踩了这个坑，探针当场全报 NO-HOST）。
    """
    void = {'br', 'input', 'img', 'hr', 'meta', 'link', 'area', 'base', 'col'}
    spans, stack = [], []
    for m in re.finditer(r'<(/?)([a-zA-Z][\w-]*)((?:"[^"]*"|[^>"])*?)(/?)>', tpl):
        closing, tag, attrs, self_close = m.group(1), m.group(2), m.group(3), m.group(4)
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    _t, _s, a = stack.pop(i)
                    spans.append((a, _s, m.end()))
                    break
            continue
        if tag not in void and not self_close:
            stack.append((tag, m.start(), attrs))
        else:
            spans.append((attrs, m.start(), m.end()))
    return spans


def test_an_error_notice_must_be_reachable_while_its_list_is_still_on_screen():
    """`XError` 与 `X.length` 同名的那一对，红字必须写在**列表非空那条分支里**。

    第 41 轮 A-M2：博主榜失败时 `bloggers.value` 留着上一轮那 27 位，于是渲染走的是
    `v-if="bloggers.length > 0"` 那条；而那句 `bloggersError` 只写在配对的 `v-else`
    空状态里 ⇒ **结构上永远到不了**，老板看到的是一份没有任何标记的旧榜。
    这与第 34 轮 `fetchEvidence` 那一族同一个形状，只是这次是"文案放对了变量、放错了分支"。

    范围要说清（不许念成"全页失败态都钉住了"）：这条只覆盖**同名的一对**
    （`bloggersError`↔`bloggers`、将来的 `postsError`↔`posts`…）。
    `adviceError`/`statsError` 这些没有同名列表的，由 `viewErrors` 与空状态那几条判据管。
    """
    html = _html()
    tpl = html[html.index('<div id="app"'):html.index('<script src="/web/post-manager.js">')]
    tpl = re.sub(r'<!--.*?-->', '', tpl, flags=re.S)
    spans = _element_spans(tpl)
    checked = []
    for m in re.finditer(r'v-if="([A-Za-z][\w]*)\.length[^"]*"', tpl):
        name = m.group(1)
        err = name + 'Error'
        if not re.search(r'const %s = ref\(' % err, html):
            continue                        # 没有配对的错误态名字，不归这条管
        host = [(a, s, e) for a, s, e in spans
                if ('v-if="%s.length' % name) in a.replace(' ', '') or ('v-if="%s.length' % name) in a]
        assert host, '模板里读得到 %s.length，却找不到承载它的元素' % name
        inside = any(err in tpl[s:e] for _a, s, e in host)
        checked.append((name, inside))
        assert inside, ('%s 只在列表为空的 `v-else` 里出现 ⇒ 列表非空（挂着上一轮数据）时'
                        '这句失败说明永不渲染' % err)
    assert checked, '一条同名对都没查到 ⇒ 这条判据已经空转（列表改名了要同步改这条）'



def test_fetch_bloggers_counts_the_rows_it_leaves_on_screen():
    """`fetchBloggers` 失败时必须**数出**表上还挂着几位 —— 跑页面里那份真源码。

    第 41 轮 A-M2 的另一半：文案已经写在正确的分支里了，但它插的是 `{{ bloggersStale }}`；
    这个数到底有没有被赋值、成功后有没有清零，只有把函数执行一遍才知道（文本判据测不到）。
    四种形状：抛错 / 200+success:false / 成功清零 / 一开始就是空表（反向对照）。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    body = _decl(_html(), 'fetchBloggers = async () =>').replace(
        'const fetchBloggers =', 'const fetchBloggersUnderTest =', 1)   # prelude 里有个同名桩
    out = _run_page_js(
        body + """
const run = async () => {
    const shapes = {};
    bloggers.value = [{id:1},{id:2},{id:3}];
    impl = async () => { const e = new Error('boom'); e.response = {status: 503}; throw e; };
    await fetchBloggersUnderTest();
    shapes.throw_ = {err: !!bloggersError.value, stale: bloggersStale.value, kept: bloggers.value.length};
    impl = async () => ({ data: { success: false, message: '接口拒绝' } });
    await fetchBloggersUnderTest();
    shapes.soft_ = {err: !!bloggersError.value, stale: bloggersStale.value};
    impl = async () => ({ data: { success: true, data: [{id:9}] } });
    await fetchBloggersUnderTest();
    shapes.ok_ = {err: !!bloggersError.value, stale: bloggersStale.value, kept: bloggers.value.length};
    // 反向对照：空表 + 失败 ⇒ 不许说"上一次取到的 3 位"（那是真的没数据，走另一条分支）
    bloggers.value = []; bloggersError.value = ''; bloggersStale.value = 0;
    impl = async () => { const e = new Error('boom'); e.response = {status: 503}; throw e; };
    await fetchBloggersUnderTest();
    shapes.empty_ = {err: !!bloggersError.value, stale: bloggersStale.value};
    return shapes;
};
run().then((r) => console.log(JSON.stringify(r)));
""",
        helpers=["const bloggersStale = ref(0);",
                 "const withWakeRetry = async (fn) => fn();",
                 # 失败原因那一档走的是页面里那份真 `isServiceDown`，不是我照抄的副本
                 _expr(_html(), 'const isServiceDown')])
    assert out['throw_'] == {'err': True, 'stale': 3, 'kept': 3}, out['throw_']
    assert out['soft_'] == {'err': True, 'stale': 3}, out['soft_']
    assert out['ok_'] == {'err': False, 'stale': 0, 'kept': 1}, out['ok_']
    assert out['empty_'] == {'err': True, 'stale': 0}, out['empty_']


def _run_chain_js(snippet, prelude_js):
    """比 `_run_page_js` 更窄的一个跑法：让用例自己指定桩，好把**真实的那条调用链**装进来。

    为什么需要它（第 41 轮 A/B 共同抓到）：`deleteBlogger` 那句"刷新没成功"原先写在
    `catch (refreshError)` 里，而页面 `_run_page_js` 的 prelude 里 `fetchBloggers` 是**死桩**
    —— 拿死桩测出来的"会抛错"是替身的性质，不是真页面的性质。
    这条判据必须跑**真的 `fetchBloggers`**（它会吞掉错误并写 `bloggersError`），
    只有这样才能问出"那句话到底到不到得了"。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    src = prelude_js + '\n' + snippet
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run([NODE, path], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=120)
        assert r.returncode == 0, 'node 跑挂：%s' % (r.stderr or r.stdout)[-500:]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        Path(path).unlink(missing_ok=True)


def test_delete_blogger_endings_are_driven_by_state_that_is_reachable():
    """删除博主的结局必须由**到得了的**证据决定（第 41 轮 A-m1/B-MAJOR-1 的那一条）。

    我上一轮把这条写成"四种结局各说各话"，其中"刷新时抛错 ⇒ 说没刷新出来"那一种
    **在真页面上到不了**：`fetchBloggers`/`fetchStats`/`fetchCleanupPreview` 三个取数点
    各自 `try/catch` 且不重新抛出（先证实这一点，再判函数）。所以判据改成两件事：
    ① 结构事实：那个 `catch (refreshError)` 不许再当作结局的依据；
    ② 行为：把**真的 `fetchBloggers`** 装进来跑，让 GET 失败 ⇒ 它吞错并写 `bloggersError`
      ⇒ `deleteBlogger` 必须读那个状态说"已删除，但列表没刷新出来"。
    """
    html = _html()
    # ① 先量"被调函数会不会 reject"——这是第 36 轮立过的规矩，不能只看形状
    for fname in ('fetchBloggers', 'fetchStats', 'fetchCleanupPreview'):
        body = _decl(html, '%s = async () =>' % fname)
        assert 'catch' in body, '%s 没有失败分支' % fname
        assert not re.search(r'catch\s*\([^)]*\)\s*\{[^}]*\bthrow\b', body, re.S), \
            '%s 现在会重新抛出了 ⇒ 下面那条"靠 catch"的老判据要重新讨论' % fname

    real_fetch = _decl(html, 'fetchBloggers = async () =>')
    out = _run_chain_js(
        real_fetch + "\n" + _decl(html, 'deleteBlogger = async (id) =>') + """
const probe = async (mode) => {
    alerts.length = 0; calls.length = 0; mode_ = mode;
    bloggersError.value = '上一轮遗留的错（必须先清掉，否则这句是假话）';
    statsError.value = '上一轮遗留的错';
    await deleteBlogger(7);
    return {alerts: alerts.slice(), calls: calls.slice(),
            stale: bloggersStale.value, err: bloggersError.value.slice(0, 12)};
};
(async () => {
    const r = { ok: await probe('ok'), soft: await probe('soft'), http: await probe('http'),
                refreshFails: await probe('refresh-fails') };
    console.log(JSON.stringify(r));
})();
""",
        prelude_js="""
const ref = (v) => ({ value: v });
let mode_ = 'ok';
const alerts = []; const calls = [];
const alert = (m) => alerts.push(m);
const confirm = () => true;
const currentView = ref('bloggers');
const bloggers = ref([{id:1},{id:2}]); const bloggersError = ref(''); const bloggersStale = ref(0);
const statsError = ref(''); const stats = ref(null); const evidenceReport = ref(null);
const cleanupPreviewError = ref('');
const fetchStats = async () => { calls.push('stats'); statsError.value = ''; };
const fetchCleanupPreview = async () => { calls.push('cleanup'); };
const withWakeRetry = async (fn) => fn();
const isServiceDown = (e) => !!(e && e.response && [502, 503, 504].includes(e.response.status));
const axios = {
  get: async (u) => { calls.push('GET ' + u);
      if (mode_ === 'refresh-fails' && u === '/api/bloggers') {
          const e = new Error('500'); e.response = { status: 500 }; throw e; }
      return { data: { success: true, data: [{id:9}] } }; },
  delete: async (u) => { calls.push('DELETE ' + u);
      if (mode_ === 'soft') return { data: { success: false, message: '还有 3 条帖子' } };
      if (mode_ === 'http') { const e = new Error('x'); e.response = { data: { detail: '内部错误' } }; throw e; }
      return { data: { success: true } }; },
};
""")
    assert out['ok']['alerts'] == ['删除成功'], out['ok']
    assert out['ok']['calls'] == ['DELETE /api/bloggers/7', 'GET /api/bloggers', 'stats'], out['ok']
    assert out['soft']['alerts'] == ['没删掉：还有 3 条帖子'], out['soft']
    assert out['soft']['calls'] == ['DELETE /api/bloggers/7'], '200+success:false 也去刷新 ⇒ 抹掉了没删成的行'
    assert out['http']['alerts'] == ['删除失败: 内部错误'], out['http']
    # 到得了的那一种：取数点自己吞了错，函数读它写下的状态说话（上一版是靠 catch ⇒ 到不了）
    assert out['refreshFails']['alerts'] == [
        '博主已删除，但列表没刷新出来：博主榜拉取失败：接口报错 —— 刷新页面即可'], out['refreshFails']
    assert out['refreshFails']['stale'] == 1, \
        '失败时表上挂着的是**上一轮那 1 位**（`ok` 那一跑把列表换成了 1 行），' \
        '`bloggersStale` 必须跟着数出来 —— 横幅那句"上一次取到的 N 位"靠它'


# 模板里静态 class 在 css / 页面 style 块里查无定义的（第 42 轮自己踩出来后量的）
DEAD_CLASSES_ALLOWED = {
    'ap-stat', 'count', 'meta-sub', 'mt-10', 'task-running', 'test-data-summary',
}
# ↑ 这 6 个**早于本轮**就在页面里，属"写了类名却没人定义"的存量脏（样式静默缺失）。
#   这里不是给它们发免检牌：名单由下面第一条断言逐名核过"确实还在用"，
#   谁把它们清掉，名单里少一名就会红；新增一个没定义的类名同样直接红。


def test_no_new_class_name_is_used_without_being_defined():
    """模板里写的每个静态 class 都必须真有人定义（第 42 轮我自己犯的：`notice-inline`）。

    一句"失败提示"如果类名查无定义，它就只是**一段正文颜色的字** ——
    "看得见是错误"这件事根本没成立。文本判据看得见这种错，所以配一条闸。
    范围：静态 `class="…"`；排除 `ri-*`（图标类在 CDN 那份 `remixicon.css` 里）与
    含 `{}` 的动态片段（`:class="{...}"` 归 `v-bind:class` 那条判据管）。
    """
    html = _html()
    css = (PROJECT_ROOT / 'web' / 'common.css').read_text(encoding='utf-8')
    inline = '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', html, flags=re.S))
    defined = set(re.findall(r'\.([a-zA-Z][\w-]*)', css + '\n' + inline))
    tpl = html[html.index('<div id="app"'):html.index('<script src="/web/post-manager.js">')]
    tpl = re.sub(r'<!--.*?-->', '', tpl, flags=re.S)
    used = set()
    for m in re.finditer(r'(?<![:\-\w])class="([^"]*)"', tpl):
        for token in m.group(1).split():
            if token.startswith('ri-') or any(ch in token for ch in '{}\''):
                continue
            used.add(token)
    undefined = sorted(used - defined)
    assert undefined == sorted(DEAD_CLASSES_ALLOWED), \
        '页面里"写了类名却没人定义"的集合变了：多出 %s，少了 %s' % (
            sorted(set(undefined) - DEAD_CLASSES_ALLOWED),
            sorted(DEAD_CLASSES_ALLOWED - set(undefined)))
    # 反向护栏：这条判据不许因为"整页一个类都没有"而空转
    assert len(used) > 100, '只扫到 %d 个静态 class ⇒ 选择器或模板形状变了' % len(used)
    # 我自己那一条失败横幅必须用**有定义**的写法（钉住具体那一句，不只钉集合）
    banner = re.search(r'<div v-if="bloggersError"[^>]*class="([^"]*)"', tpl)
    assert banner, '博主榜那条失败横幅不见了（那 A-M2 就没修）'
    assert all(tok in defined for tok in banner.group(1).split()), \
        '博主榜失败横幅用的类名查无定义：%s' % banner.group(1)


def test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran():
    """净值停在十一天前时，页面那行"截至 <今天>"会替旧数据撒谎（第 48 轮 A-9 / B-8）。

    判据形态：在 node 里**执行页面那份 `navFreshNote`**，喂五种真实形状。
    文本断言在这里毫无价值 —— 把 `lag >= 4` 改成 `lag >= 400` 页面照旧一句"净值截至 X"，
    而文字判据看不见任何变化（这一族的标准死法）。
    """
    if not NODE:
        pytest.skip('本机没有 node')
    out = _run_page_js("""
const pick = (report) => { evidenceReport.value = report; return navFreshNote(); };
const cases = {
  noReport: pick(null),
  noNavAtAll: pick({nav_as_of: null, nav_lag_days: null, nav_future_rows: 0, nav_stale: false}),
  fresh: pick({nav_as_of: '2026-09-25', nav_lag_days: 1, nav_future_rows: 0, nav_stale: false}),
  stale: pick({nav_as_of: '2026-09-13', nav_lag_days: 13, nav_future_rows: 0, nav_stale: true}),
  futureOnly: pick({nav_as_of: '2026-09-13', nav_lag_days: 13, nav_future_rows: 4, nav_stale: true}),
  // 阈值只有一个出处（后端 NAV_LAG_WARN_DAYS）：页面必须照 `nav_stale` 说，不自己比大小。
  serverSaysFresh: pick({nav_as_of: '2026-09-23', nav_lag_days: 3, nav_future_rows: 0, nav_stale: false}),
};
console.log(JSON.stringify(cases));
""", [_decl(_html(), 'navFreshNote = () =>')])
    assert '净值截至' not in out['noReport'] and '没取到' in out['noReport'], out['noReport']
    assert '没取到' in out['noNavAtAll'] and 'null' not in out['noNavAtAll'], out['noNavAtAll']
    assert out['fresh'] == '净值截至 2026-09-25', '当天的数据不许喊落后（喊早了老板会开始忽略这条提示）'
    assert '落后' not in out['serverSaysFresh'], '页面自己比大小 = 第二把尺子：%s' % out['serverSaysFresh']
    assert '落后 13 天' in out['stale'] and '旧净值' in out['stale'], out['stale']
    assert '另有 4 行' in out['futureOnly'], '预签发的未来净值行必须看得见（存量脏数据不能静默回来）'
