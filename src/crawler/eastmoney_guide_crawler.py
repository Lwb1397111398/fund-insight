"""
东方财富博客导读爬虫 - 抓取首页导读文章
"""
import requests
import re
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import time
import random


class EastmoneyGuideCrawler:
    """东方财富博客导读爬虫"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
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
        print(f"[EastmoneyGuide] 开始抓取博客导读，目标数量: {max_articles}")

        all_articles = []

        try:
            # 访问博客首页
            response = self.session.get(self.base_url, timeout=15)
            response.raise_for_status()
            response.encoding = 'utf-8'

            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            # 查找 class="list" 的文章列表（博客导读）
            guide_list = soup.find('ul', class_='list')

            if guide_list:
                print(f"[EastmoneyGuide] 找到博客导读列表")

                # 查找所有文章项
                article_items = guide_list.find_all('li', recursive=False)
                print(f"[EastmoneyGuide] 找到 {len(article_items)} 篇文章")

                for item in article_items[:max_articles]:
                    article_detail = self._extract_article_info(item)
                    if article_detail:
                        all_articles.append(article_detail)

                    # 随机延迟
                    time.sleep(random.uniform(0.3, 0.8))
            else:
                print(f"[EastmoneyGuide] 未找到博客导读列表")

        except Exception as e:
            print(f"[EastmoneyGuide] 抓取失败: {e}")
            import traceback
            traceback.print_exc()

        print(f"[EastmoneyGuide] 成功抓取 {len(all_articles)} 篇文章")
        return all_articles

    def _extract_article_info(self, item) -> Optional[Dict]:
        """
        从文章元素中提取信息
        """
        try:
            # 查找所有链接
            links = item.find_all('a', href=True)
            if len(links) < 2:
                return None
            
            # 第一个链接是标题
            title_link = links[0]
            title = title_link.get_text(strip=True)
            article_url = title_link.get('href', '')
            
            # 第二个链接是作者
            author = links[1].get_text(strip=True) if len(links) > 1 else '未知作者'
            
            # 补全URL
            if article_url and not article_url.startswith('http'):
                article_url = 'https:' + article_url if article_url.startswith('//') else self.base_url + article_url
            
            # 提取文章ID
            article_id = self._extract_article_id(article_url)

            # 构建文章详情
            article_detail = {
                'article_id': article_id,
                'title': title,
                'content': title,  # 导读页面只有标题，没有摘要
                'author': author,
                'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'url': article_url,
                'source': 'eastmoney_guide',
                'is_vip': False,
                'read_count': 0,
                'comment_count': 0,
            }

            # 获取文章详情（内容）
            content = self._fetch_article_content(article_url)
            if content:
                article_detail['content'] = content

            # AI分析
            ai_result = self._analyze_article(article_detail)
            article_detail['ai_analysis'] = ai_result

            return article_detail

        except Exception as e:
            print(f"[EastmoneyGuide] 解析文章失败: {e}")
            return None

    def _extract_article_id(self, url: str) -> str:
        """从URL中提取文章ID"""
        try:
            # 从URL中提取数字ID
            match = re.search(r'/news/(\d+)', url)
            if match:
                return match.group(1)
            return url.split('/')[-1] if '/' in url else url
        except:
            return url

    def _fetch_article_content(self, url: str) -> str:
        """获取文章正文内容"""
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            response.encoding = 'utf-8'

            soup = BeautifulSoup(response.text, 'html.parser')

            # 查找文章正文
            content_selectors = [
                '.article-body',
                '.article-content',
                '#article_content',
                '.content',
                '.detail-content',
                'article',
            ]

            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    return content_elem.get_text(strip=True)[:500]  # 限制长度

            return ''

        except Exception as e:
            print(f"[EastmoneyGuide] 获取文章详情失败: {e}")
            return ''

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
            print(f"[EastmoneyGuide] 分析失败: {e}")
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
        print(f"AI分析: {article.get('ai_analysis', {})}")
