"""
东方财富7x24快讯爬虫 - 抓取实时财经快讯

数据源: newsapi.eastmoney.com 7x24快讯
- 返回标题+摘要+时间
- finance.eastmoney.com 域名下部分文章可获取全文
- 快讯内容简短但有明确市场观点和板块指向，适合作为观点来源
"""
import requests
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import time
import random

logger = logging.getLogger(__name__)


class EastmoneyNewsCrawler:
    """东方财富7x24快讯爬虫"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://finance.eastmoney.com/',
        })
        # 7x24快讯API
        self.newsapi_url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_103_ajaxResult_50_1_.html"

    def fetch_news(self, max_articles: int = 20, fetch_content: bool = False) -> List[Dict]:
        """
        抓取7x24快讯

        Args:
            max_articles: 最多抓取条数
            fetch_content: 是否抓取文章全文（较慢，会逐篇请求finance详情页）

        Returns:
            快讯列表
        """
        logger.info(f"[EastmoneyNews] 开始抓取7x24快讯，目标数量: {max_articles}")

        all_articles = []

        try:
            response = self.session.get(self.newsapi_url, timeout=15)
            match = re.search(r'var ajaxResult=(\{.*\})', response.text, re.DOTALL)
            if not match:
                logger.error("[EastmoneyNews] 快讯API返回格式异常")
                return all_articles

            data = json.loads(match.group(1))
            items = data.get('LivesList', [])

            for item in items[:max_articles]:
                article = self._extract_article_info(item)
                if article:
                    all_articles.append(article)

            logger.info(f"[EastmoneyNews] 从API获取 {len(all_articles)} 条快讯")

            # 可选：抓取全文
            if fetch_content and all_articles:
                self._fetch_full_content(all_articles)

        except Exception as e:
            logger.error(f"[EastmoneyNews] 抓取失败: {e}")

        logger.info(f"[EastmoneyNews] 成功抓取 {len(all_articles)} 条快讯")
        return all_articles

    def _extract_article_info(self, item: Dict) -> Optional[Dict]:
        """从快讯API项目提取信息"""
        try:
            title = item.get('title', '').strip()
            if not title:
                return None

            newsid = item.get('newsid', '')
            digest = item.get('digest', '').strip()
            # 去掉摘要中的标题重复【标题】
            if digest.startswith(f'【{title}】'):
                digest = digest[len(f'【{title}】'):].strip()
            elif digest.startswith(f'【{title}'):
                # 部分截断的情况
                idx = digest.find('】')
                if idx > 0:
                    digest = digest[idx + 1:].strip()

            showtime = item.get('showtime', '')
            comment_num = item.get('commentnum', 0)
            column = item.get('column', '')
            newstype = item.get('newstype', '')
            editor = item.get('editor_name', '')

            return {
                'article_id': newsid,
                'title': title,
                'content': digest if digest else title,
                'author': editor if editor else '东方财富',
                'is_vip': False,
                'publish_time': showtime,
                'read_count': 0,
                'comment_count': comment_num,
                'like_count': 0,
                'url': f'https://finance.eastmoney.com/a/{newsid}.html',
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'eastmoney_news',
                'quality_score': 70,  # 快讯来自东方财富官方，质量稳定
                'newstype': newstype,
                'column': column,
            }
        except Exception as e:
            logger.error(f"[EastmoneyNews] 提取快讯信息失败: {e}")
            return None

    def _fetch_full_content(self, articles: List[Dict]) -> None:
        """为finance域名的文章抓取全文"""
        count = 0
        for article in articles:
            try:
                url = article.get('url', '')
                if 'finance.eastmoney.com' not in url:
                    continue

                time.sleep(random.uniform(0.3, 0.8))

                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                response.encoding = 'utf-8'

                soup = BeautifulSoup(response.text, 'html.parser')
                body = soup.select_one('#ContentBody')
                if body:
                    full_text = body.get_text(strip=True)
                    if len(full_text) > len(article.get('content', '')):
                        article['content'] = full_text[:2000]  # 限制长度
                        count += 1

            except Exception as e:
                logger.debug(f"[EastmoneyNews] 获取全文失败 {article.get('url','')}: {e}")

        logger.info(f"[EastmoneyNews] 补充全文 {count} 篇")


# 全局爬虫实例
_news_crawler = None


def get_news_crawler() -> EastmoneyNewsCrawler:
    """获取快讯爬虫实例"""
    global _news_crawler
    if _news_crawler is None:
        _news_crawler = EastmoneyNewsCrawler()
    return _news_crawler


if __name__ == '__main__':
    print("测试东方财富7x24快讯爬虫...")
    crawler = EastmoneyNewsCrawler()
    articles = crawler.fetch_news(max_articles=5)
    print(f"\n抓取到 {len(articles)} 条快讯:")
    for i, article in enumerate(articles, 1):
        print(f"\n{i}. {article['title']}")
        print(f"   摘要: {article['content'][:80]}...")
        print(f"   时间: {article['publish_time']}")
        print(f"   URL: {article['url']}")
