"""
天天基金吧爬虫模块 - 完全独立，不影响现有功能

功能：
- 抓取天天基金网专业文章（推荐）
- 抓取天天基金吧热门帖子
- AI 分析帖子价值
- 一键采纳为观点

使用方式：
from src.crawler import crawler, article_crawler
articles = article_crawler.fetch_home_articles(max_articles=10)
posts = crawler.fetch_fund_posts('161725')   # 天天基金吧（of 前缀地址 + DOM 列表 + post_article 正文）
"""

from .tiantian_crawler import TiantianCrawler, crawler
from .article_crawler import EastMoneyArticleCrawler, article_crawler
from .sentiment_analyzer import SentimentAnalyzer, analyzer

__all__ = ['TiantianCrawler', 'EastMoneyArticleCrawler', 
           'SentimentAnalyzer', 'AIPostAnalyzer',
           'crawler', 'article_crawler', 'analyzer', 'ai_analyzer']


def __getattr__(name):
    if name in {"AIPostAnalyzer", "ai_analyzer"}:
        from .ai_analyzer import AIPostAnalyzer, ai_analyzer
        return {"AIPostAnalyzer": AIPostAnalyzer, "ai_analyzer": ai_analyzer}[name]
    raise AttributeError(f"module 'src.crawler' has no attribute {name!r}")
