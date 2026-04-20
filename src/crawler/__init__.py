"""
天天基金吧爬虫模块 - 完全独立，不影响现有功能

功能：
- 抓取天天基金网专业文章（推荐）
- 抓取天天基金吧热门帖子
- AI 分析帖子价值
- 一键采纳为观点

使用方式：
from src.crawler import tiantian_crawler, article_crawler, ai_analyzer
articles = article_crawler.fetch_home_articles(max_articles=10)
posts = tiantian_crawler.fetch_fund_posts('000001')
"""

from .tiantian_crawler import TiantianCrawler, crawler
from .article_crawler import EastMoneyArticleCrawler, article_crawler
from .sentiment_analyzer import SentimentAnalyzer, analyzer
from .ai_analyzer import AIPostAnalyzer, ai_analyzer

__all__ = ['TiantianCrawler', 'EastMoneyArticleCrawler', 
           'SentimentAnalyzer', 'AIPostAnalyzer',
           'crawler', 'article_crawler', 'analyzer', 'ai_analyzer']
