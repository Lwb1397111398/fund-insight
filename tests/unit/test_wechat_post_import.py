import asyncio
from unittest.mock import AsyncMock

from fastapi import BackgroundTasks

from src.api.routes import crawler as crawler_routes
from src.api.routes.crawler import WeChatArticleRequest
from src.models.database import Blogger, Post


def test_wechat_fetch_keeps_real_title_and_normalizes_duplicate_url(monkeypatch, test_db):
    article = {
        "title": "真实的微信文章标题",
        "author": "测试公众号",
        "content": "这是一篇长度足够的微信文章正文，用来确认抓取保存时不会用日期和博主名称覆盖真实文章标题。",
        "publish_time": "2026-07-20",
    }
    monkeypatch.setattr(
        "src.crawler.wechat_fetcher.wechat_fetcher.fetch",
        AsyncMock(return_value=article),
    )

    first = asyncio.run(crawler_routes.fetch_wechat_article(
        WeChatArticleRequest(
            url="http://mp.weixin.qq.com/s/article-key?utm_source=test&scene=1#wechat_redirect",
            enqueue=False,
        ),
        BackgroundTasks(),
        test_db,
    ))
    second = asyncio.run(crawler_routes.fetch_wechat_article(
        WeChatArticleRequest(
            url="https://mp.weixin.qq.com/s/article-key?scene=9&utm_medium=share",
            enqueue=False,
        ),
        BackgroundTasks(),
        test_db,
    ))

    assert first["success"] is True
    assert first["message"] == "文章已抓取，等待加入分析队列"
    assert first["data"]["title"] == article["title"]
    assert first["data"]["queued"] is False
    assert second["data"]["already_exists"] is True
    assert test_db.query(Blogger).count() == 1
    assert test_db.query(Post).count() == 1
    saved = test_db.query(Post).one()
    assert saved.title == article["title"]
    assert saved.source_url == "https://mp.weixin.qq.com/s/article-key"

