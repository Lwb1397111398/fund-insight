"""新浪财经爬虫正文抓取单元测试。"""
from unittest.mock import MagicMock

from src.crawler.sina_finance_crawler import SinaFinanceCrawler


def _feed_response(intro: str, url: str = "https://finance.sina.com.cn/x/doc-1.shtml"):
    """构造 feed API 的 JSON 响应。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "result": {
            "status": {"code": 0},
            "data": [
                {
                    "docid": "comos:abc123",
                    "title": "测试标题",
                    "intro": intro,
                    "media_name": "新浪财经",
                    "ctime": "1700000000",
                    "url": url,
                    "lids": "2516",
                }
            ],
        }
    }
    resp.raise_for_status = MagicMock()
    return resp


def _html_response(body_html: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = f"<html><body>{body_html}</body></html>"
    resp.raise_for_status = MagicMock()
    return resp


def test_parse_articles_fetches_detail_and_overrides_intro(monkeypatch):
    """_parse_articles 应调用 fetch_article_detail, 用 #artibody 正文覆盖短 intro。"""
    crawler = SinaFinanceCrawler()
    feed = _feed_response(intro="这是短摘要")
    detail_html = '<div id="artibody"><p>这是完整的正文内容，长度足够超过三十个字符阈值，作为真实抓取到的文章正文。</p></div>'
    detail = _html_response(detail_html)

    # 第一次 GET 取 feed JSON, 第二次 GET 取正文 HTML
    crawler.session.get = MagicMock(side_effect=[feed, detail])

    articles = crawler.fetch_articles(category="finance", num=1)
    assert len(articles) == 1
    a = articles[0]
    assert a["summary"] == "这是短摘要"  # summary 保留 intro 兜底
    assert "这是完整的正文内容" in a["content"]  # content 被正文覆盖
    assert a["content"] != "这是短摘要"
    # 确实调了两次(列表+正文)
    assert crawler.session.get.call_count == 2


def test_parse_articles_falls_back_to_intro_on_detail_failure(monkeypatch):
    """正文抓取失败/为空时回退 intro, 不崩。"""
    crawler = SinaFinanceCrawler()
    feed = _feed_response(intro="兜底摘要")
    # 正文页请求抛异常
    crawler.session.get = MagicMock(side_effect=[feed, RuntimeError("网络错误")])

    articles = crawler.fetch_articles(category="finance", num=1)
    assert len(articles) == 1
    assert articles[0]["content"] == "兜底摘要"


def test_parse_articles_keeps_intro_when_detail_too_short(monkeypatch):
    """正文抓到但<30字(与intro差不多短)时保留 intro, 不用噪声覆盖。"""
    crawler = SinaFinanceCrawler()
    feed = _feed_response(intro="原始摘要")
    detail = _html_response('<div id="artibody">短</div>')
    crawler.session.get = MagicMock(side_effect=[feed, detail])

    articles = crawler.fetch_articles(category="finance", num=1)
    assert articles[0]["content"] == "原始摘要"


def test_fetch_article_detail_sets_encoding_before_reading_text(monkeypatch):
    """fetch_article_detail 必须先设 response.encoding 再读 text, 否则UTF-8正文会被ISO-8859-1解码成乱码。"""
    crawler = SinaFinanceCrawler()
    # 模拟: 响应头缺charset, requests默认encoding=ISO-8859-1, 但body是UTF-8
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/html"}  # 无charset
    resp.encoding = "ISO-8859-1"                 # requests默认(会致乱码)
    resp.apparent_encoding = "utf-8"              # 真实编码
    resp.raise_for_status = MagicMock()
    html = '<div id="artibody"><p>韩国总统政策室长金容范周六表示，这是正确的中文正文内容。</p></div>'
    resp.text = html
    crawler.session.get = MagicMock(return_value=resp)

    crawler.fetch_article_detail("https://finance.sina.com.cn/x/1.shtml")
    # 关键断言: 必须在读text前把encoding设为apparent_encoding(utf-8), 否则中文乱码
    assert resp.encoding == "utf-8"


def test_init_does_not_load_llm_analyzer():
    """__init__ 不再加载 LLM analyzer(死代码已移除)。"""
    crawler = SinaFinanceCrawler()
    assert not hasattr(crawler, "llm_analyzer")
