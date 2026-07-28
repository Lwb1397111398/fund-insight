"""
股吧爬虫 - 抓取股票讨论区的观点

数据源: 东方财富股吧
- 和基金吧结构相同，但讨论的是股票和指数
- 用户活跃度高，观点密度大
- 技术实现复用基金吧的经验
"""
import requests
import re
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import sys
import os

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config


class StockGubaCrawler:
    """股吧爬虫 - 复用基金吧的成熟经验"""

    def __init__(self):
        self.base_url = "https://guba.eastmoney.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://guba.eastmoney.com/',
        }
        self.timeout = 15
        self.request_delay = 1.0
        self.max_posts = 20

        # 筛选参数 - 适度宽松，优先保证观点数量
        self.min_click_count = 50      # 最小阅读数（比基金吧的100低）
        self.min_comment_count = 2      # 最小评论数（比基金吧的5低）
        self.min_title_length = 6       # 最小标题长度

        self._last_request_time = 0

    def _rate_limit(self):
        """频率限制"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_time = time.time()

    def _fix_encoding(self, text: str) -> str:
        """修复乱码"""
        try:
            return text.encode('latin1').decode('utf-8')
        except:
            try:
                return text.encode('latin1').decode('gbk')
            except:
                return text

    def _is_quality_post(self, item: Dict) -> Tuple[bool, str]:
        """
        判断是否为高质量帖子，并返回质量等级

        Returns:
            tuple: (是否通过筛选, 质量等级)
            质量等级: 'elite'(精华) / 'good'(优质) / 'normal'(普通) / None(不合格)
        """
        click_count = item.get('post_click_count', 0)
        comment_count = item.get('post_comment_count', 0)
        title = item.get('post_title', '')
        v_user_code = item.get('v_user_code', 0)
        is_essence = item.get('is_essence', False) or item.get('post_essence', False)

        # 基础筛选：排除短标题
        if len(title) < self.min_title_length:
            return False, None

        # 判断条件
        is_vip = v_user_code and v_user_code != 0
        is_hot = click_count > 300 or comment_count > 15

        # 精华帖标准（满足任一即可）
        if is_essence or (is_vip and is_hot) or (click_count > 500 and comment_count > 30):
            return True, 'elite'

        # 优质帖标准
        if is_vip or is_hot or (click_count > 100 and comment_count > 5):
            return True, 'good'

        # 普通帖标准（放宽条件）
        if click_count > self.min_click_count and comment_count > self.min_comment_count:
            return True, 'normal'

        return False, None

    def fetch_stock_posts(self, stock_code: str, quality_filter: bool = True) -> List[Dict]:
        """
        抓取指定股票/指数的热门帖子

        Args:
            stock_code: 股票代码（如 000001 上证指数，或 600519 茅台）
            quality_filter: 是否启用质量筛选

        Returns:
            帖子列表
        """
        posts = []

        try:
            url = f"{self.base_url}/list,zssh{stock_code}.html"

            self._rate_limit()
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'

            if response.status_code != 200:
                print(f"[StockGuba] 抓取股票{stock_code}吧失败：HTTP {response.status_code}")
                return []

            # 使用正则表达式提取 JSON 数据
            json_match = re.search(r'var article_list=(\{.*?\});', response.text, re.DOTALL)

            if json_match:
                try:
                    article_data = json.loads(json_match.group(1))
                    post_items = article_data.get('re', [])

                    print(f"[StockGuba] 解析到 {len(post_items)} 条帖子，启用质量筛选: {quality_filter}")

                    # 筛选高质量帖子并分类
                    elite_posts = []
                    good_posts = []
                    normal_posts = []

                    for item in post_items:
                        should_include, quality_level = self._is_quality_post(item)
                        if should_include:
                            item['_quality_level'] = quality_level
                            if quality_level == 'elite':
                                elite_posts.append(item)
                            elif quality_level == 'good':
                                good_posts.append(item)
                            else:
                                normal_posts.append(item)

                    # 按优先级排序：精华 > 优质 > 普通
                    filtered_posts = elite_posts + good_posts + normal_posts

                    print(f"[StockGuba] 筛选结果：精华 {len(elite_posts)} 条，优质 {len(good_posts)} 条，普通 {len(normal_posts)} 条")

                    for item in filtered_posts[:self.max_posts]:
                        try:
                            post_data = self._parse_json_post(item, stock_code)
                            if post_data:
                                posts.append(post_data)
                        except Exception as e:
                            print(f"[StockGuba] 解析帖子失败：{e}")
                            continue

                except json.JSONDecodeError as e:
                    print(f"[StockGuba] 解析 JSON 失败：{e}")

            print(f"[StockGuba] 成功抓取股票{stock_code}吧 {len(posts)} 条高质量帖子")

        except requests.exceptions.Timeout:
            print(f"[StockGuba] 抓取股票{stock_code}吧超时")
        except requests.exceptions.RequestException as e:
            print(f"[StockGuba] 抓取股票{stock_code}吧网络错误：{e}")
        except Exception as e:
            print(f"[StockGuba] 抓取股票{stock_code}吧异常：{e}")

        return posts

    def _parse_json_post(self, item: Dict, stock_code: str) -> Optional[Dict]:
        """解析 JSON 格式的帖子数据"""
        try:
            post_id = str(item.get('post_id', ''))
            title = item.get('post_title', '')

            # 修复编码
            title = self._fix_encoding(title)

            # 匿名处理
            author = "网友"

            # 获取热度
            read_count = item.get('post_click_count', 0)
            reply_count = item.get('post_comment_count', 0)

            post_time_str = item.get('post_publish_time', '')
            post_time = self._parse_time(post_time_str)

            # 获取达人标记
            v_user_code = item.get('v_user_code', 0)
            is_vip = v_user_code and v_user_code != 0

            # 获取精华标记
            is_essence = item.get('is_essence', False) or item.get('post_essence', False)

            # 获取质量等级
            quality_level = item.get('_quality_level', 'normal')

            # 构建 URL
            url = f"https://guba.eastmoney.com/news,zssh{stock_code},{post_id}.html"

            # 默认使用标题作为内容
            content = title

            return {
                'post_id': post_id,
                'stock_code': stock_code,
                'title': title,
                'content': content,
                'author': author,
                'is_vip': is_vip,
                'is_essence': is_essence,
                'quality_level': quality_level,
                'read_count': read_count,
                'reply_count': reply_count,
                'post_time': post_time,
                'url': url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'stock_guba'
            }

        except Exception as e:
            print(f"[StockGuba] 解析 JSON 帖子失败：{e}")
            return None

    def _parse_time(self, time_str: str) -> str:
        """解析时间字符串"""
        if not time_str:
            return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']

            for fmt in formats:
                try:
                    dt = datetime.strptime(time_str, fmt)
                    return dt.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    continue

            return time_str

        except:
            return time_str

    def fetch_hot_stocks(self, stock_codes: List[str]) -> Dict[str, List[Dict]]:
        """
        批量抓取多个热门股票的帖子

        Args:
            stock_codes: 股票代码列表

        Returns:
            {股票代码: 帖子列表}
        """
        results = {}

        print(f"[StockGuba] 开始抓取 {len(stock_codes)} 个热门股票...")

        for i, stock_code in enumerate(stock_codes, 1):
            print(f"[StockGuba] [{i}/{len(stock_codes)}] 抓取股票 {stock_code}")
            posts = self.fetch_stock_posts(stock_code)
            results[stock_code] = posts

            if i < len(stock_codes):
                time.sleep(self.request_delay)

        print(f"[StockGuba] 批量抓取完成")
        return results


# 单例
crawler = StockGubaCrawler()


if __name__ == '__main__':
    # 测试：抓取上证指数、深证成指、创业板指
    hot_stocks = ['000001', '399001', '399006']

    print(f"测试抓取热门指数股吧...")
    results = crawler.fetch_hot_stocks(hot_stocks)

    total = sum(len(posts) for posts in results.values())
    print(f"\n共抓取 {total} 条帖子：")

    for stock, posts in results.items():
        print(f"\n{stock}: {len(posts)} 条")
        for i, post in enumerate(posts[:3], 1):
            print(f"  {i}. {post['title']} (阅读:{post['read_count']}, 评论:{post['reply_count']})")
