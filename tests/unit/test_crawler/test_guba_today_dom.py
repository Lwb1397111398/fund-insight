"""
2026-10-13 股吧/基金吧抓取修复的判据。

钉住五件事（全部来自当天实测的病根）：
1. 基金吧必须打 of 前缀地址（list,of<code>.html）——裸代码会抓到同名股票吧；
2. 新版列表页是服务端渲染 div.articleh 行，解析出标题/阅读/回复/时间/链接；
3. "仅当天"过滤挪到爬虫侧（列表阶段），旧帖不再占额度、不再发详情请求；
   时间解析失败按当天放行（宁可放行也不误杀整源）；
4. 正文与权威发帖时间取详情页内嵌 var post_article JSON（.gbbody 实测是导航、
   列表行 .l5 实测是"最后回复时间"——两者都不可信）；
5. "今天被回复顶起的昨天旧帖"在详情时间核实后丢弃，并用后续候选补满额度。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import src.crawler.tiantian_crawler as tiantian_mod
import src.crawler.stock_guba_crawler as guba_mod

_BJT = timezone(timedelta(hours=8))


def _today_str(fmt='%m-%d %H:%M'):
    return datetime.now(_BJT).strftime(fmt)


def _today_full():
    return datetime.now(_BJT).strftime('%Y-%m-%d %H:%M:%S')


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = 'utf-8'


class FakeRequests:
    """按 URL 子串路由返回固定页面，并记录所有请求 URL。"""
    class exceptions:
        class Timeout(Exception):
            pass

        class RequestException(Exception):
            pass

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResponse('', 404)


def _dom_row(title, href, reads, comments, time_text, author='测试网友'):
    return f'''<div class="articleh normal_post">
      <span class="l1">{reads}</span>
      <span class="l2">{comments}</span>
      <span class="l3"><a href="{href}" title="{title}">{title}</a></span>
      <span class="l4"><a href="//i.eastmoney.com/1">{author}</a></span>
      <span class="l5">{time_text}</span>
    </div>'''


def _list_page(*rows):
    return FakeResponse('<html><body><div id="articlelistnew">' + ''.join(rows) + '</div></body></html>')


def _detail_page(content_text, publish_time=''):
    """模拟详情页：正文与发帖时间在 var post_article JSON 里（等号两边带空格）。"""
    payload = {"post": {"post_content": f"<p>{content_text}</p>", "post_publish_time": publish_time}}
    return FakeResponse('<html><body><script>var post_article = '
                        + json.dumps(payload, ensure_ascii=False) + ';</script></body></html>')


@pytest.fixture()
def fund_crawler():
    c = tiantian_mod.TiantianCrawler()
    c.request_delay = 0  # 测试不发真实延迟
    c.max_posts = 5
    return c


def test_fund_board_uses_of_prefixed_url(fund_crawler, monkeypatch):
    """病根①：列表地址必须是 list,of<code>.html，抓错吧等于整源内容都是错的。"""
    fake = FakeRequests({
        'list,of161725.html': _list_page(
            _dom_row('今天大跌要不要清仓止损', '/news,of161725,111.html', 320, 12, _today_str()),
        ),
        'of161725,111': _detail_page('今天大跌要不要清仓止损？仓位重的先减三成。', _today_full()),
    })
    monkeypatch.setattr(tiantian_mod, 'requests', fake)
    posts = fund_crawler.fetch_fund_posts('161725')
    assert any('list,of161725.html' in u for u in fake.calls), fake.calls
    assert 'list,161725.html' not in ''.join(fake.calls)
    assert len(posts) == 1
    p = posts[0]
    assert p['source'] == 'fund_guba'
    assert p['read_count'] == 320 and p['reply_count'] == 12
    assert p['url'] == 'https://guba.eastmoney.com/news,of161725,111.html'
    assert p['publish_time'][:10] == datetime.now(_BJT).strftime('%Y-%m-%d')


def test_fund_board_dom_parsing_and_today_filter(fund_crawler, monkeypatch):
    """病根②③：DOM 行解析 + 列表阶段当天过滤，旧帖不发详情请求。"""
    fake = FakeRequests({
        'list,of161725.html': _list_page(
            _dom_row('今天反弹了可以补一笔', '/news,of161725,201.html', 400, 20, _today_str()),
            _dom_row('去年的旧帖也该被拦', '/news,of161725,202.html', 900, 40, '01-01 09:00'),
            _dom_row('时间解析失败按放行', '/news,of161725,203.html', 500, 25, '置顶'),
        ),
        'of161725,201': _detail_page('反弹确认，回踩就是买点，建议补一笔仓位。', _today_full()),
        'of161725,203': _detail_page('这条列表时间解析不出来，详情也没给时间，仍按当天放行。'),
    })
    monkeypatch.setattr(tiantian_mod, 'requests', fake)
    posts = fund_crawler.fetch_fund_posts('161725')
    titles = {p['title'] for p in posts}
    assert titles == {'今天反弹了可以补一笔', '时间解析失败按放行'}
    # 旧帖 202 连详情都不该请求
    assert not any('of161725,202' in u for u in fake.calls)


def test_fund_detail_prefers_post_article_json(fund_crawler, monkeypatch):
    """病根④：正文取 var post_article JSON（.gbbody 是导航，用了就会全文雷同）。"""
    nav_page = FakeResponse(
        '<html><body><div class="gbbody">东方财富网 移动客户端 天天基金网 股吧首页 基金吧 问董秘</div>'
        '<script>var post_article = ' + json.dumps({
            'post': {'post_content': '<p>白酒又跌了，估值已经回到20倍以下，跌出性价比。</p>',
                     'post_publish_time': _today_full()}}, ensure_ascii=False) + ';</script></body></html>')
    fake = FakeRequests({
        'list,of161725.html': _list_page(
            _dom_row('白酒跌出性价比了吗', '/news,of161725,301.html', 500, 30, _today_str()),
        ),
        'of161725,301': nav_page,
    })
    monkeypatch.setattr(tiantian_mod, 'requests', fake)
    posts = fund_crawler.fetch_fund_posts('161725')
    assert len(posts) == 1
    assert '白酒又跌了' in posts[0]['content']
    assert '东方财富网 移动客户端' not in posts[0]['content']


def test_fund_bumped_old_post_dropped_and_backfilled(fund_crawler, monkeypatch):
    """病根⑤：列表显示"今天回复"、详情 JSON 说是昨天发帖 → 丢弃并用下一候选补位。"""
    yesterday = (datetime.now(_BJT) - timedelta(days=1)).strftime('%Y-%m-%d 17:54:58')
    fake = FakeRequests({
        'list,of161725.html': _list_page(
            _dom_row('走了走了认亏了（昨帖今顶）', '/news,of161725,401.html', 320, 12, _today_str()),
            _dom_row('今天的真观点补位进来', '/news,of161725,402.html', 210, 8, _today_str()),
        ),
        'of161725,401': _detail_page('昨天就想跑了，今天反弹纯属诱多。', yesterday),
        'of161725,402': _detail_page('今天放量收复缺口，短线可以看多做T。', _today_full()),
    })
    monkeypatch.setattr(tiantian_mod, 'requests', fake)
    posts = fund_crawler.fetch_fund_posts('161725')
    titles = [p['title'] for p in posts]
    assert titles == ['今天的真观点补位进来']
    assert posts[0]['publish_time'][:10] == datetime.now(_BJT).strftime('%Y-%m-%d')


def test_fund_time_year_rollover(fund_crawler):
    """跨年：元旦后抓到上年 11/12 月的帖子，年份必须退回上一年，不能拼成未来。"""
    now = datetime(2026, 1, 2, 10, 0, tzinfo=_BJT)
    assert fund_crawler._board_time_to_full('11-20 09:58', now=now).startswith('2025-11-20')
    assert fund_crawler._board_time_to_full('01-02 08:00', now=now).startswith('2026-01-02')
    # 比"明天"还靠后的 MM-DD 只能是去年的（未来时间不存在），同样退回上一年
    assert fund_crawler._board_time_to_full('01-10 08:00', now=now).startswith('2025-01-10')


def test_stock_guba_today_filter_before_quality_ranking(monkeypatch):
    """股吧：热门旧帖（阅读 39 万那种）不再挤占 max_posts 额度、不再发详情。"""
    c = guba_mod.StockGubaCrawler()
    c.request_delay = 0
    c.max_posts = 5
    article_list = {'re': [
        {'post_id': 1, 'post_title': '今天尾盘大跳水主力谨慎', 'post_click_count': 400,
         'post_comment_count': 8, 'post_publish_time': _today_full(), 'v_user_code': 0},
        {'post_id': 2, 'post_title': '银行股牛气冲天创历史新高（昨日旧帖）', 'post_click_count': 399677,
         'post_comment_count': 3057, 'post_publish_time': '2026-01-01 11:31:00', 'v_user_code': 0},
    ]}
    fake = FakeRequests({
        'list,sh600036.html': FakeResponse(f'<!DOCTYPE html><script>var article_list={json.dumps(article_list, ensure_ascii=False)};</script>'),
        'news,sh600036,1': FakeResponse('<div class="newstext">尾盘跳水说明主力确实谨慎，短期还会震荡。</div>'),
    })
    monkeypatch.setattr(guba_mod, 'requests', fake)
    posts = c.fetch_stock_posts('600036')
    assert [p['title'] for p in posts] == ['今天尾盘大跳水主力谨慎']
    assert not any('news,sh600036,2' in u for u in fake.calls)


def test_stock_is_before_today_fails_open():
    """时间缺失/解析失败一律按当天放行——时间源异常不能把整源误杀。"""
    c = guba_mod.StockGubaCrawler()
    assert c._is_before_today('') is False
    assert c._is_before_today('置顶') is False
    assert c._is_before_today((datetime.now(_BJT) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')) is True
