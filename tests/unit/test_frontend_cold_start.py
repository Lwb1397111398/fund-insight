# -*- coding: utf-8 -*-
"""冷启动与"取不到数据"时的前端诚实性（任务 #27 / #26 / #30 / #32 的页面那一半）。

四条判据都是"以前会红、修了才绿"的具体事故，不是样式偏好：
1. 首屏取数在实例唤醒时会失败，页面从此停在"加载中"或空白。
2. 唤醒失败被当成"密码失效" ⇒ 把老板存的密码清掉，逼他重输一个没错的密码。
3. "机器已纠正"横幅无条件插 `realigned.core`，而 ETF 升级行根本没有这个字段
   ⇒ 页面上出现"按板块核心词「」"这种空格子（任务 #26）。
4. 两个口径的"准确率"只活在 `title` 里 ⇒ 手机没有 hover 等于没写（任务 #30）。

判据形态：这个仓库测前端的方式就是扫 `index.html` 文本（`test_frontend_loading.py`
一系都是这么写的）。文本断言挡不住"渲染出来仍然不对"，所以本批另外用真实浏览器
跑了一次首屏与板块匹配页（验证记录见 `docs/模块总览/前端与接口层.md`）。
"""
import re
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / 'web' / 'index.html'


def _html():
    return INDEX_HTML.read_text(encoding='utf-8')


class _Text(HTMLParser):
    """只收正文文字，跳过 script/style；同时把 title 属性单独收着一处都不给。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.titles = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip += 1
        for k, v in attrs:
            if k == 'title' and v:
                self.titles.append(v)

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def _visible_text(html):
    p = _Text()
    p.feed(re.sub(r'<!--.*?-->', '', html, flags=re.S))
    return ' '.join(''.join(p.chunks).split()), ' '.join(p.titles)


def test_first_load_fetches_all_go_through_the_wake_retry():
    """五个首屏取数点必须全部经过 `withWakeRetry`，一个都不能漏。

    数的是"总数 == 被包住的数"，不是"字符串存在" ⇒ 在别处新加一处没包的调用会直接红。
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


def test_a_waking_service_no_longer_wipes_the_saved_password():
    """`checkAuth` 里"清密码"必须排在"服务器确实答过话"那条分支后面。

    以前 `catch` 无条件 `localStorage.removeItem('access_password')` ⇒ 实例还在睡
    就等于把老板的密码删了。判据取顺序：`!e.response` 早于 removeItem，
    且中间必须先 return。
    """
    html = _html()
    body = re.search(r'const checkAuth = async \(\) => \{(.*?)\n                \};',
                     html, flags=re.S)
    assert body, '找不到 checkAuth，判据要跟着改名一起改'
    src = body.group(1)
    guard = src.find("if (!e.response)")
    wipe = src.find("localStorage.removeItem('access_password')")
    assert guard >= 0, '唤醒失败仍与密码失效共用一条分支'
    assert 0 <= wipe and guard < wipe, '清密码的那句必须排在"服务器答过话"之后'
    assert 'serviceUnreachable.value = true' in src[:wipe], '连不上要走独立状态，不是弹密码框'
    assert 'return;' in src[guard:wipe], '!e.response 分支必须先返回，否则会掉进清密码逻辑'


def test_realigned_note_cannot_render_an_unfilled_slot():
    """`realigned` 的每个插槽都得有守卫，缺字段时要有别的说法。

    `etf_upgrade` 那类记录没有 `core`（见 `pick_etf_upgrade` 返回的键），
    所以"机器已按板块核心词「{{ core }}」"对它们是空格子。
    """
    html = _html()
    block = re.search(r'<div v-if="m\.realigned && !m\.reviewed"[^>]*>(.*?)</div>',
                      html, flags=re.S)
    assert block, '找不到"机器已纠正"横幅本体'
    src = block.group(1)
    for field in ('core', 'from_code', 'from_name'):
        assert ('{{ m.realigned.%s }}' % field) not in src or \
               ('m.realigned.%s' % field) in src.split('{{ m.realigned.%s }}' % field)[0], \
            '%s 被无条件插进正文：这一类记录没这个字段，页面上就是空格子' % field
    assert "<template v-else>" in src, '三类说法之外还要有一条兜底'
    # 存量升级记录只有 `replaced`（镜像实测 2 行），没有它就只能说"查不到"
    assert 'm.realigned.replaced' in src, '没读 `replaced`：存量行说不出原来是什么'
    assert 'm.realigned.from_code' in src.split('m.realigned.replaced')[0], \
        '`replaced` 要排在 from_code 之后，别把新行的精确代码换成一句人话'
    text, _ = _visible_text(html)
    assert '「」' not in text, '页面上还有空引号'


def test_blogger_table_calibers_are_readable_without_hover():
    """两个口径的说明必须是正文，不能只挂在 title 上（手机没有 hover）。"""
    html = _html()
    text, _ = _visible_text(html)
    for phrase in ('现算命中率', '含归档', '等级 grade 按它定'):
        assert phrase in text, '%s 不在正文里：窄屏读不到口径' % phrase
    # TOP 弹窗以前一句口径都没有，两列并排裸奔
    assert '排名按' in text and '命中率' in text, 'TOP 博主弹窗没有口径说明'
    # 表头本身也不能再靠 title 表达
    heads = re.findall(r'<th[^>]*>(.*?)</th>', html, flags=re.S)
    assert any('现算命中率' in h for h in heads), '准确率表头没写口径'
    assert not [h for h in heads if 'title=' in h], '表头又把要看的字塞回 hover 里了'


def test_take_data_failure_says_so_instead_of_looking_like_an_empty_database():
    """博主榜取数失败时不能显示"暂无博主数据"——那是假事实。"""
    html = _html()
    assert 'bloggersError' in html, '博主榜没有失败态'
    body = re.search(r'const fetchBloggers = async \(\) => \{(.*?)\n                \};',
                     html, flags=re.S)
    assert body and 'catch' in body.group(1), 'fetchBloggers 没有 catch：唤醒失败只是未处理拒绝'
    block = re.search(r'<div v-else class="empty-state">(.*?)</div>', html, flags=re.S)
    assert block and '暂无博主数据' in block.group(1), '找不到博主榜的空状态'
    assert 'bloggersError' in block.group(1), '空状态没读失败原因'
    text, _ = _visible_text(html)
    assert '结论证据体检拉取失败' in text, '证据体检失败要说出来，不能停在"加载中"'
    assert '下面的准确率没有区间可看' in text, '取不到区间时必须拦住"把准确率当结论"'


def test_relevance_third_state_is_visible_and_filterable():
    """`no_literal_fund`（名册里根本没有对口标的）以前被压成"相关"，永不进待复核。"""
    html = _html()
    text, _ = _visible_text(html)
    assert '名册无对口' in text, '第三态没有计数入口'
    assert "relevance_state === 'no_literal_fund'" in html, '第三态没进筛选/统计'
    assert '名册里没有字面对口的标的' in text, '第三态的行内说法要能和"挂错了"区分开'
