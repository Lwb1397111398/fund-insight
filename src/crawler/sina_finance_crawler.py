"""
新浪财经爬虫模块
抓取新浪财经的财经新闻、股票新闻、基金新闻
"""
import requests
import json
import time
from datetime import datetime
from typing import List, Dict, Any, Optional
try:
    from src.core.ai_analyzer import get_analyzer
except ImportError:
    try:
        # 如果在爬虫模块内部，使用相对导入
        from .ai_analyzer import get_analyzer
    except ImportError:
        # 直接导入analyzer
        from src.analyzer.llm_analyzer import get_analyzer

class SinaFinanceCrawler:
    """新浪财经爬虫"""
    
    # API端点映射
    API_ENDPOINTS = {
        'finance': 'https://feed.sina.com.cn/api/roll/get?pageid=153&lid=2516&num={num}&page={page}',
        'stock': 'https://feed.sina.com.cn/api/roll/get?pageid=153&lid=2513&num={num}&page={page}',
        'fund': 'https://feed.sina.com.cn/api/roll/get?pageid=153&lid=2514&num={num}&page={page}',
    }
    
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Referer': 'https://finance.sina.com.cn',
        }
        self.llm_analyzer = get_analyzer()
    
    def fetch_articles(self, category: str = 'finance', num: int = 20, page: int = 1) -> List[Dict[str, Any]]:
        """
        获取文章列表
        
        Args:
            category: 类别 ('finance'|'stock'|'fund')
            num: 获取数量
            page: 页码
            
        Returns:
            文章列表
        """
        url = self.API_ENDPOINTS.get(category, self.API_ENDPOINTS['finance'])
        url = url.format(num=num, page=page)
        
        try:
            response = self.session.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('result', {}).get('status', {}).get('code') != 0:
                print(f"API返回错误: {data}")
                return []
            
            articles = data['result'].get('data', [])
            return self._parse_articles(articles)
            
        except Exception as e:
            print(f"抓取失败: {e}")
            return []
    
    def _parse_articles(self, articles: List[Dict]) -> List[Dict[str, Any]]:
        """解析文章数据"""
        parsed = []
        for article in articles:
            try:
                # 转换时间戳
                ctime = article.get('ctime', '')
                publish_time = datetime.fromtimestamp(int(ctime)).strftime('%Y-%m-%d %H:%M:%S') if ctime else ''
                
                parsed_article = {
                    'article_id': article.get('docid', '').replace('comos:', ''),
                    'title': article.get('title', ''),
                    'content': article.get('intro', ''),
                    'summary': article.get('intro', ''),
                    'author': article.get('media_name', '新浪财经'),
                    'publish_time': publish_time,
                    'url': article.get('url', ''),
                    'source': 'sina_finance',
                    'category': self._get_category_name(article.get('lids', '')),
                }
                
                # AI分析
                ai_result = self._analyze_article(parsed_article)
                parsed_article['ai_analysis'] = ai_result
                
                parsed.append(parsed_article)
                
            except Exception as e:
                print(f"解析文章失败: {e}")
                continue
        
        return parsed
    
    def _get_category_name(self, lids: str) -> str:
        """根据lids获取类别名称"""
        lid_map = {
            '2513': '股票',
            '2514': '基金',
            '2516': '财经',
        }
        for lid, name in lid_map.items():
            if lid in lids:
                return name
        return '财经'
    
    def _analyze_article(self, article: Dict) -> Dict[str, Any]:
        """分析文章情绪（使用关键词匹配，快速稳定）"""
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
                'sectors': sectors[:3]  # 最多3个板块
            }
            
        except Exception as e:
            print(f"分析失败: {e}")
            return {
                'sentiment': 'neutral',
                'confidence': 0,
                'sentiment_score': 0.5,
                'sectors': []
            }
    
    def fetch_article_detail(self, url: str) -> str:
        """获取文章详情"""
        try:
            response = self.session.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            
            # 简单提取正文（实际可能需要更复杂的解析）
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 尝试找到正文内容
            content_selectors = ['#artibody', '.article-content', '#article_content', '.main-content']
            for selector in content_selectors:
                content_div = soup.select_one(selector)
                if content_div:
                    return content_div.get_text(strip=True)
            
            return ''
            
        except Exception as e:
            print(f"获取文章详情失败: {e}")
            return ''


# 全局爬虫实例
_sina_crawler = None

def get_sina_crawler() -> SinaFinanceCrawler:
    """获取新浪财经爬虫实例"""
    global _sina_crawler
    if _sina_crawler is None:
        _sina_crawler = SinaFinanceCrawler()
    return _sina_crawler


if __name__ == '__main__':
    # 测试
    crawler = SinaFinanceCrawler()
    articles = crawler.fetch_articles(category='finance', num=5)
    print(f"获取到 {len(articles)} 篇文章")
    for article in articles[:3]:
        print(f"\n标题: {article['title']}")
        print(f"作者: {article['author']}")
        print(f"时间: {article['publish_time']}")
        print(f"AI分析: {article.get('ai_analysis', {})}")
