"""
新浪博文爬虫模块
抓取新浪财经博客板块的文章
"""
import requests
import re
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import time
import random


class SinaBlogCrawler:
    """新浪博文爬虫"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })

        # 新浪博文基础地址
        self.base_url = "https://finance.sina.com.cn/roll/feed.d.html"

    def fetch_blog_posts(self, max_posts: int = 20) -> List[Dict]:
        """
        抓取新浪博文

        Args:
            max_posts: 最多抓取文章数

        Returns:
            文章列表
        """
        print(f"[SinaBlog] 开始抓取新浪博文，目标数量: {max_posts}")

        all_posts = []
        page = 1

        while len(all_posts) < max_posts:
            # 构建URL
            url = f"{self.base_url}?lid=1001&page={page}"
            print(f"[SinaBlog] 抓取第 {page} 页: {url}")

            try:
                response = self.session.get(url, headers=self.session.headers, timeout=15)
                response.raise_for_status()
                response.encoding = 'utf-8'

                # 解析HTML
                soup = BeautifulSoup(response.text, 'html.parser')

                # 查找 class="list_009" 的博文列表
                blog_lists = soup.find_all('ul', class_='list_009')

                if not blog_lists:
                    print(f"[SinaBlog] 第 {page} 页未找到博文列表")
                    break

                print(f"[SinaBlog] 找到 {len(blog_lists)} 个博文列表")

                for blog_list in blog_lists:
                    if len(all_posts) >= max_posts:
                        break

                    items = blog_list.find_all('li', recursive=False)

                    for item in items:
                        if len(all_posts) >= max_posts:
                            break

                        post_detail = self._extract_post_info(item)
                        if post_detail:
                            all_posts.append(post_detail)

                        # 随机延迟
                        time.sleep(random.uniform(0.2, 0.5))

                # 检查是否还有下一页
                if len(blog_lists) == 0 or len(all_posts) >= max_posts:
                    break

                page += 1

                # 限制最大页数
                if page > 5:
                    break

            except Exception as e:
                print(f"[SinaBlog] 抓取第 {page} 页失败: {e}")
                break

        print(f"[SinaBlog] 成功抓取 {len(all_posts)} 篇文章")
        return all_posts

    def _extract_post_info(self, item) -> Optional[Dict]:
        """
        从博文元素中提取信息
        """
        try:
            # 查找所有链接
            links = item.find_all('a', href=True)

            if len(links) < 2:
                return None

            # 第一个链接是标题
            title_link = links[0]
            title = title_link.get_text(strip=True)
            post_url = title_link.get('href', '')

            # 第二个链接是作者
            author_link = links[1]
            author = author_link.get_text(strip=True)

            # 查找时间
            time_span = item.find('span')
            publish_time = ''
            if time_span:
                time_text = time_span.get_text(strip=True)
                # 提取时间，格式: (03月07日 01:00)
                match = re.search(r'\((\d{2}月\d{2}日 \d{2}:\d{2})\)', time_text)
                if match:
                    time_str = match.group(1)
                    # 转换为标准格式
                    current_year = datetime.now().year
                    publish_time = f"{current_year}年{time_str}"

            # 提取文章ID
            article_id = self._extract_article_id(post_url)

            # 构建博文详情
            post_detail = {
                'article_id': article_id,
                'title': title,
                'content': title,  # 博文列表页只有标题
                'author': author,
                'publish_time': publish_time,
                'url': post_url,
                'source': 'sina_blog',
            }

            # 获取文章详情（内容）
            content = self._fetch_post_content(post_url)
            if content:
                post_detail['content'] = content

            # AI分析
            ai_result = self._analyze_post(post_detail)
            post_detail['ai_analysis'] = ai_result

            return post_detail

        except Exception as e:
            print(f"[SinaBlog] 解析博文失败: {e}")
            return None

    def _extract_article_id(self, url: str) -> str:
        """从URL中提取文章ID"""
        try:
            # 从微博文章URL中提取ID
            # 格式: https://weibo.com/ttarticle/x/m/show#/id=2309405273636578918630
            match = re.search(r'id=(\d+)', url)
            if match:
                return match.group(1)
            return url.split('/')[-1] if '/' in url else url
        except:
            return url

    def _fetch_post_content(self, url: str) -> str:
        """获取博文正文内容"""
        try:
            # 微博文章使用API获取
            if 'weibo.com' in url:
                return self._fetch_weibo_content(url)
            
            response = self.session.get(url, headers=self.session.headers, timeout=10)
            response.raise_for_status()
            response.encoding = 'utf-8'

            soup = BeautifulSoup(response.text, 'html.parser')

            # 查找文章正文
            content_selectors = [
                '.article-content',
                '#article_content',
                '.content',
                '.detail-content',
                'article',
            ]

            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    return content_elem.get_text(strip=True)

            return ''

        except Exception as e:
            print(f"[SinaBlog] 获取博文详情失败: {e}")
            return ''
    
    def _fetch_weibo_content(self, url: str) -> str:
        """通过API获取微博文章内容"""
        try:
            import re
            
            # 从URL提取文章ID
            match = re.search(r'id[=/](\d+)', url)
            if not match:
                return ''
            
            article_id = match.group(1)
            
            # 调用微博文章API
            api_url = f"https://weibo.com/ttarticle/x/m/aj/detail?id={article_id}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://weibo.com/',
            }
            
            response = self.session.get(api_url, headers=headers, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # code可能是字符串或整数
            code = data.get('code')
            if code == 100000 or code == '100000':
                article_data = data.get('data', {})
                content = article_data.get('content', '')
                
                # 清理HTML标签
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, 'html.parser')
                text = soup.get_text(strip=True)
                
                return text if text else ''
            
            return ''
            
        except Exception as e:
            print(f"[SinaBlog] 获取微博文章失败: {e}")
            return ''

    def _analyze_post(self, post: Dict) -> Dict:
        """分析博文情绪（使用关键词匹配）"""
        try:
            title = post.get('title', '')
            content = post.get('content', '')
            text = f"{title} {content}".lower()

            if not text:
                return {
                    'sentiment': 'neutral',
                    'confidence': 0,
                    'sentiment_score': 0.5,
                    'sectors': []
                }

            # 关键词匹配
            bullish_words = ['上涨', '涨停', '大涨', '看好', '买入', '增持', '反弹', '突破', '利好', '强劲', '增长', '上升', '牛市', '看好', '机会']
            bearish_words = ['下跌', '跌停', '大跌', '看空', '卖出', '减持', '回调', '跌破', '利空', '疲软', '下滑', '下降', '熊市', '风险', '警惕']

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
                '科技': ['科技', '芯片', '半导体', '人工智能', 'AI', '软件', '硬件', '算力'],
                '医药': ['医药', '医疗', '药品', '疫苗', '医院', '生物'],
                '金融': ['银行', '保险', '证券', '金融', '券商', '基金', 'ETF'],
                '新能源': ['新能源', '光伏', '风电', '储能', '锂电池', '电动车', '核聚变'],
                '消费': ['消费', '零售', '白酒', '食品', '饮料', '家电'],
                '地产': ['地产', '房地产', '建筑', '建材', '水泥'],
                '军工': ['军工', '航天', '国防', '军事'],
                '能源': ['能源', '石油', '天然气', '煤炭', '电力'],
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
            print(f"[SinaBlog] 分析失败: {e}")
            return {
                'sentiment': 'neutral',
                'confidence': 0,
                'sentiment_score': 0.5,
                'sectors': []
            }


# 全局爬虫实例
_blog_crawler = None


def get_blog_crawler() -> SinaBlogCrawler:
    """获取新浪博文爬虫实例"""
    global _blog_crawler
    if _blog_crawler is None:
        _blog_crawler = SinaBlogCrawler()
    return _blog_crawler


if __name__ == '__main__':
    # 测试
    crawler = SinaBlogCrawler()
    posts = crawler.fetch_blog_posts(max_posts=5)
    print(f"\n获取到 {len(posts)} 篇文章")
    for post in posts[:3]:
        print(f"\n标题: {post['title']}")
        print(f"作者: {post['author']}")
        print(f"时间: {post['publish_time']}")
        print(f"AI分析: {post.get('ai_analysis', {})}")
