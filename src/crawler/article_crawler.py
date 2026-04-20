"""
天天基金网文章爬虫 - 抓取专业文章，而非评论

改进：
1. 抓取天天基金网首页的专业文章
2. 文章都是编辑/基金公司发布的，质量很高
3. 支持 AI 分析和采纳为观点
"""
import requests
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from bs4 import BeautifulSoup

import sys
import os

# Fix: 只在直接运行此文件时添加路径
if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config


class EastMoneyArticleCrawler:
    """天天基金网文章爬虫"""
    
    def __init__(self):
        self.base_url = "https://fund.eastmoney.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://fund.eastmoney.com/',
        }
        self.timeout = config.CRAWLER_TIMEOUT
        self.request_delay = config.CRAWLER_REQUEST_DELAY
        # Fix: 减少默认抓取数量，避免超时 (BUG-002)
        self.max_articles = 5  # 从20改为5，减少总耗时
        self.article_detail_timeout = 5  # 单篇文章详情超时时间（秒）
        self._last_request_time = 0
    
    def _rate_limit(self):
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_time = time.time()
    
    def _fix_encoding(self, text: str) -> str:
        try:
            return text.encode('latin1').decode('utf-8')
        except:
            try:
                return text.encode('latin1').decode('gbk')
            except:
                return text
    
    def fetch_home_articles(self, max_articles: Optional[int] = None) -> List[Dict]:
        """
        从天天基金网首页抓取文章
        
        Args:
            max_articles: 最大文章数
        
        Returns:
            文章列表
        """
        if not config.CRAWLER_ENABLED:
            print("[Article Crawler] 爬虫模块未启用")
            return []
        
        articles = []
        
        try:
            self._rate_limit()
            response = requests.get(self.base_url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                print(f"[Article Crawler] 访问首页失败：HTTP {response.status_code}")
                return []
            
            # 提取所有文章链接
            article_links = re.findall(r'href="(//fund\.eastmoney\.com/a/([0-9]+)\.html)"', response.text)
            
            print(f"[Article Crawler] 找到 {len(article_links)} 个文章链接")
            
            limit = max_articles or self.max_articles
            for i, (url_part, article_id) in enumerate(article_links[:limit], 1):
                try:
                    url = f"https:{url_part}"
                    article = self._fetch_article_detail(url, article_id)
                    if article:
                        articles.append(article)
                except Exception as e:
                    print(f"[Article Crawler] 获取文章 {i} 失败：{e}")
                    continue
            
            print(f"[Article Crawler] 成功抓取 {len(articles)} 篇文章")
            
        except requests.exceptions.Timeout:
            print(f"[Article Crawler] 请求超时")
        except Exception as e:
            print(f"[Article Crawler] 异常：{e}")
        
        return articles
    
    def _fetch_article_detail(self, url: str, article_id: str) -> Optional[Dict]:
        """
        获取文章详情
        
        Fix: 添加单篇文章超时控制，避免整体超时 (BUG-002)
        """
        try:
            self._rate_limit()
            # Fix: 使用单独的超时时间，避免单篇文章耗时过长
            response = requests.get(url, headers=self.headers, timeout=self.article_detail_timeout)
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 获取标题
            title_elem = soup.find('h1')
            if not title_elem:
                return None
            
            title = title_elem.get_text(strip=True)
            
            # 获取发布时间
            publish_time = ''
            time_elem = soup.find('span', class_=re.compile(r'time|date|publish'))
            if time_elem:
                publish_time = time_elem.get_text(strip=True)
            
            # 获取文章内容
            content = ''
            content_elem = soup.find('div', class_=re.compile(r'article.*content|content.*article|txt|text'))
            if content_elem:
                # 移除 script 和 style
                for tag in content_elem(['script', 'style']):
                    tag.decompose()
                content = content_elem.get_text(strip=True)
            
            # 如果没有找到内容，尝试多个 class
            if not content:
                for class_name in ['txt1', 'txt2', 'content', 'article-content', 'article_body', 'detail']:
                    elem = soup.find('div', class_=class_name)
                    if elem:
                        content = elem.get_text(strip=True)
                        if len(content) > 50:
                            break
            
            # 构造摘要
            summary = content[:500] if content else title[:500]
            
            return {
                'article_id': article_id,
                'title': title,
                'content': content,
                'summary': summary,
                'publish_time': publish_time,
                'url': url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'author': '编辑',  # 编辑发布的专业文章
                'source': 'fund.eastmoney.com',
            }
            
        except requests.exceptions.Timeout:
            print(f"[Article Crawler] 文章 {article_id} 超时，跳过")
            return None
        except Exception as e:
            print(f"[Article Crawler] 获取文章详情失败：{e}")
            return None


# 单例
article_crawler = EastMoneyArticleCrawler()


if __name__ == '__main__':
    import os
    os.environ['CRAWLER_ENABLED'] = 'true'
    
    print("测试抓取天天基金网文章...")
    articles = article_crawler.fetch_home_articles(max_articles=5)
    
    print(f"\n抓取到 {len(articles)} 篇文章:\n")
    for i, article in enumerate(articles, 1):
        print(f"{i}. {article['title']}")
        print(f"   来源：{article['source']}")
        print(f"   发布时间：{article['publish_time']}")
        print(f"   摘要：{article['summary'][:80]}...")
        print()
