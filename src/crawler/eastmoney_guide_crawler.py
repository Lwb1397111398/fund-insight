"""
东方财富博客导读爬虫 - 抓取首页导读文章

2026-07 适配说明：
- blog.eastmoney.com/guide_1.html 列表页面结构不变
- 文章链接全部指向 caifuhao.eastmoney.com（纯前端SPA，requests无法获取内容）
- 不再逐篇抓取文章详情，改为从列表页提取信息 + 快讯API补充摘要
- 保留关键词情绪分析，但不再依赖文章详情内容
"""
import requests
import re
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import time
import random

logger = logging.getLogger(__name__)


class EastmoneyGuideCrawler:
    """东方财富博客导读爬虫"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })

        # 东方财富博客基础地址
        self.base_url = "https://blog.eastmoney.com"

    def fetch_guide_articles(self, max_articles: int = 20) -> List[Dict]:
        """
        抓取博客导读文章

        Args:
            max_articles: 最多抓取文章数

        Returns:
            文章列表
        """
        logger.info(f"[EastmoneyGuide] 开始抓取博客导读，目标数量: {max_articles}")

        all_articles = []

        try:
            # 访问博客首页
            response = self.session.get(self.base_url, timeout=15)
            response.raise_for_status()
            response.encoding = 'utf-8'

            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            # 查找文章列表 - 导读在 ul.list 里
            guide_list = soup.find('ul', class_='list')

            if guide_list:
                logger.info("[EastmoneyGuide] 找到博客导读列表")

                # 查找所有文章项
                article_items = guide_list.find_all('li', recursive=False)
                if not article_items:
                    # 可能 li 有 class
                    article_items = guide_list.find_all('li', class_='cl')
                logger.info(f"[EastmoneyGuide] 找到 {len(article_items)} 篇文章")

                for item in article_items[:max_articles]:
                    article_detail = self._extract_article_info(item)
                    if article_detail:
                        all_articles.append(article_detail)

                    # 随机延迟
                    time.sleep(random.uniform(0.3, 0.8))
            else:
                logger.warning("[EastmoneyGuide] 未找到博客导读列表")

        except Exception as e:
            logger.error(f"[EastmoneyGuide] 抓取失败: {e}")

        # 内容为空时回退到标题（caifuhao SPA无法请求内容，标题本身已足够LLM分析）
        for article in all_articles:
            if not article.get('content'):
                article['content'] = article['title']

        # 对每篇文章做关键词情绪分析
        for article in all_articles:
            article['ai_analysis'] = self._analyze_article(article)

        logger.info(f"[EastmoneyGuide] 成功抓取 {len(all_articles)} 篇文章")
        return all_articles

    def _extract_article_info(self, item) -> Optional[Dict]:
        """
        从文章元素中提取信息
        """
        try:
            # 博客首页的 ul.list 里 li 结构和 b2p1list 类似
            # span.l2 是标题，span.l3 是作者
            title_span = item.find('span', class_='l2')
            author_span = item.find('span', class_='l3')

            if title_span:
                title_link = title_span.find('a')
                title = title_link.get_text(strip=True) if title_link else ''
                article_url = title_link.get('href', '') if title_link else ''
            else:
                # 兜底：从所有链接中提取
                links = item.find_all('a', href=True)
                if len(links) < 1:
                    return None
                title_link = links[0]
                title = title_link.get_text(strip=True)
                article_url = title_link.get('href', '')

            if not title:
                return None

            # 提取作者
            author = '未知作者'
            is_vip = False
            if author_span:
                author_link = author_span.find('a')
                if author_link:
                    author = author_link.get_text(strip=True)
                vip_icon = author_span.find('span', class_='jv')
                if vip_icon:
                    is_vip = True

            # 补全URL（caifuhao链接以//开头）
            if article_url and not article_url.startswith('http'):
                article_url = 'https:' + article_url if article_url.startswith('//') else self.base_url + article_url

            # 提取文章ID
            article_id = self._extract_article_id(article_url)

            return {
                'article_id': article_id,
                'title': title,
                'content': '',  # caifuhao SPA无法抓取，后续由 _enrich_with_newsapi 补充
                'author': author,
                'is_vip': is_vip,
                'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'url': article_url,
                'source': 'eastmoney_guide',
                'read_count': 0,
                'comment_count': 0,
            }

        except Exception as e:
            logger.error(f"[EastmoneyGuide] 解析文章失败: {e}")
            return None

    def _extract_article_id(self, url: str) -> str:
        """从URL中提取文章ID"""
        try:
            match = re.search(r'/news/(\d+)', url)
            if match:
                return match.group(1)
            return url.split('/')[-1] if '/' in url else url
        except:
            return url

    def _analyze_article(self, article: Dict) -> Dict:
        """分析文章情绪（使用关键词匹配）"""
        try:
            title = article.get('title', '')
            content = article.get('content', '')
            text = f"{title} {content}".lower()

            if not text:
                return {
                    'sentiment': 'neutral',
                    'confidence': 0,
                    'sentiment_score': 0.5,
                    'sectors': []
                }

            # 关键词匹配
            bullish_words = ['上涨', '涨停', '大涨', '看好', '买入', '增持', '反弹', '突破', '利好', '强劲', '增长', '上升', '牛市']
            bearish_words = ['下跌', '跌停', '大跌', '看空', '卖出', '减持', '回调', '跌破', '利空', '疲软', '下滑', '下降', '熊市']

            bullish_count = sum(1 for word in bullish_words if word in text)
            bearish_count = sum(1 for word in bearish_words if word in text)

            # 判断情绪
            if bullish_count > bearish_count:
                sentiment = 'bullish'
                confidence = min(50 + bullish_count * 10, 90)
                sentiment_score = 0.5 + min(bullish_count * 0.1, 0.4)
            elif bearish_count > bullish_count:
                sentiment = 'bearish'
                confidence = min(50 + bearish_count * 10, 90)
                sentiment_score = 0.5 - min(bearish_count * 0.1, 0.4)
            else:
                sentiment = 'neutral'
                confidence = 30
                sentiment_score = 0.5

            # 识别板块
            sectors = []
            sector_keywords = {
                '科技': ['科技', '芯片', '半导体', '人工智能', 'AI', '软件', '硬件'],
                '医药': ['医药', '医疗', '药品', '疫苗', '医院', '生物'],
                '金融': ['银行', '保险', '证券', '金融', '券商', '基金'],
                '新能源': ['新能源', '光伏', '风电', '储能', '锂电池', '电动车'],
                '消费': ['消费', '零售', '白酒', '食品', '饮料', '家电'],
                '地产': ['地产', '房地产', '建筑', '建材', '水泥'],
            }

            for sector, keywords in sector_keywords.items():
                if any(kw in text for kw in keywords):
                    sectors.append(sector)

            return {
                'sentiment': sentiment,
                'confidence': confidence,
                'sentiment_score': round(sentiment_score, 2),
                'sectors': sectors[:3]
            }

        except Exception as e:
            logger.error(f"[EastmoneyGuide] 分析失败: {e}")
            return {
                'sentiment': 'neutral',
                'confidence': 0,
                'sentiment_score': 0.5,
                'sectors': []
            }


# 全局爬虫实例
_guide_crawler = None


def get_guide_crawler() -> EastmoneyGuideCrawler:
    """获取博客导读爬虫实例"""
    global _guide_crawler
    if _guide_crawler is None:
        _guide_crawler = EastmoneyGuideCrawler()
    return _guide_crawler


if __name__ == '__main__':
    # 测试
    crawler = EastmoneyGuideCrawler()
    articles = crawler.fetch_guide_articles(max_articles=5)
    print(f"\n获取到 {len(articles)} 篇文章")
    for article in articles[:3]:
        print(f"\n标题: {article['title']}")
        print(f"作者: {article['author']}")
        print(f"VIP: {article['is_vip']}")
        if article.get('content'):
            print(f"摘要: {article['content'][:80]}")
        print(f"AI分析: {article.get('ai_analysis', {})}")
