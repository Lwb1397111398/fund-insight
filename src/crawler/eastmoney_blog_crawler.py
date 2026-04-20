"""
东方财富博客爬虫 - 抓取热门博主文章
使用网页解析方式，避免API限制
"""
import requests
import json
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import time
import random


class EastmoneyBlogCrawler:
    """东方财富博客爬虫"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        # 东方财富博客基础地址
        self.base_url = "https://blog.eastmoney.com"
        
    def fetch_hot_articles(self, max_articles: int = 20) -> List[Dict]:
        """
        抓取热门博主文章
        
        Args:
            max_articles: 最多抓取文章数
            
        Returns:
            文章列表
        """
        print(f"[EastmoneyBlog] 开始抓取热门文章，目标数量: {max_articles}")
        
        all_articles = []
        
        try:
            # 访问博客首页获取热门文章
            response = self.session.get(self.base_url, timeout=15)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 查找"热门博主文章"板块
            hot_articles_div = soup.find('div', class_='b2p1list')
            
            if hot_articles_div:
                print(f"[EastmoneyBlog] 找到热门博主文章板块")
                
                # 查找所有文章项
                article_items = hot_articles_div.find_all('li', class_='cl')
                print(f"[EastmoneyBlog] 找到 {len(article_items)} 篇文章")
                
                for item in article_items[:max_articles]:
                    article_detail = self._extract_article_info(item)
                    if article_detail:
                        all_articles.append(article_detail)
                    
                    # 随机延迟
                    time.sleep(random.uniform(0.3, 0.8))
            else:
                print(f"[EastmoneyBlog] 未找到热门博主文章板块")
                
        except Exception as e:
            print(f"[EastmoneyBlog] 抓取失败: {e}")
            import traceback
            traceback.print_exc()
                
        print(f"[EastmoneyBlog] 成功抓取 {len(all_articles)} 篇文章")
        return all_articles
    
    def _extract_article_info(self, item) -> Optional[Dict]:
        """
        从文章元素中提取信息
        """
        try:
            # 查找标题链接
            title_elem = item.find('span', class_='l2').find('a') if item.find('span', class_='l2') else None
            if not title_elem:
                return None
                
            title = title_elem.get_text(strip=True)
            article_url = title_elem.get('href', '')
            
            # 补全URL
            if article_url and not article_url.startswith('http'):
                article_url = 'https:' + article_url if article_url.startswith('//') else self.base_url + article_url
            
            # 提取文章ID (从URL中提取)
            article_id_match = re.search(r'/news/(\d+)', article_url)
            article_id = article_id_match.group(1) if article_id_match else ''
            
            # 查找作者
            author_span = item.find('span', class_='l3')
            author = '未知'
            is_vip = False
            
            if author_span:
                author_link = author_span.find('a')
                if author_link:
                    author = author_link.get_text(strip=True)
                
                # 检查是否有VIP标记
                vip_icon = author_span.find('span', class_='jv')
                if vip_icon:
                    is_vip = True
            
            # 获取文章详情（内容）
            content = self._fetch_article_content(article_url) if article_url else title
            
            return {
                'article_id': article_id,
                'title': title,
                'content': content if content else title,
                'author': author,
                'is_vip': is_vip,
                'publish_time': '',
                'read_count': 0,
                'comment_count': 0,
                'like_count': 0,
                'url': article_url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'eastmoney_blog',
                'quality_score': 80 if is_vip else 60
            }
            
        except Exception as e:
            print(f"[EastmoneyBlog] 提取文章信息失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _fetch_article_content(self, article_url: str) -> str:
        """
        获取文章详情内容
        """
        try:
            # 添加随机延迟，避免触发反爬虫
            time.sleep(random.uniform(0.5, 1.5))
            
            # 添加Referer头，模拟从博客首页跳转
            headers = {
                'Referer': self.base_url,
                'Cache-Control': 'max-age=0',
            }
            
            response = self.session.get(article_url, timeout=10, headers=headers)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 尝试多种可能的内容容器
            content_selectors = [
                'div.blog-content',
                'div.article-content',
                'div.content-detail',
                'div.post-content',
                'article',
            ]
            
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    return content_elem.get_text(strip=True)
            
            # 如果没找到，返回整个body文本（限制长度）
            body = soup.find('body')
            if body:
                return body.get_text(strip=True)[:3000]
                
            return ''
            
        except requests.exceptions.HTTPError as e:
            if '403' in str(e):
                print(f"[EastmoneyBlog] 文章访问被拒绝（反爬虫），跳过: {article_url}")
            else:
                print(f"[EastmoneyBlog] 获取文章内容失败: {e}")
            return ''  # 返回空字符串，不影响其他文章抓取
        except Exception as e:
            print(f"[EastmoneyBlog] 获取文章内容失败: {e}")
            return ''


# 全局爬虫实例
crawler = EastmoneyBlogCrawler()


if __name__ == '__main__':
    # 测试爬虫
    print("测试东方财富博客爬虫...")
    
    # 抓取热门文章
    articles = crawler.fetch_hot_articles(max_articles=5)
    
    print(f"\n抓取到 {len(articles)} 篇文章:")
    for i, article in enumerate(articles, 1):
        print(f"\n{i}. {article['title']}")
        print(f"   作者: {article['author']} {'(VIP)' if article['is_vip'] else ''}")
        print(f"   质量分: {article['quality_score']}")
        print(f"   链接: {article['url']}")
