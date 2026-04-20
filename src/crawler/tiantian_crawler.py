"""
天天基金吧爬虫 - 优化版

改进：
1. 优先抓取高质量帖子（高热度 + 长内容）
2. 匿名处理：统称为网友
3. 添加 AI 筛选和分析功能
4. 支持采纳为观点

安全特性：
- 异常完全隔离
- 频率限制（可配置）
- 手动触发，不自动运行
"""
import requests
import re
import json
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


class TiantianCrawler:
    """天天基金吧爬虫 - 优化版"""
    
    def __init__(self):
        self.base_url = "https://guba.eastmoney.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://guba.eastmoney.com/',
        }
        self.timeout = config.CRAWLER_TIMEOUT
        self.request_delay = config.CRAWLER_REQUEST_DELAY
        self.max_posts = config.MAX_POSTS_PER_FUND
        
        # 筛选参数
        self.min_click_count = 100  # 最小阅读数
        self.min_comment_count = 5   # 最小评论数
        self.min_title_length = 8    # 最小标题长度（排除短评论）
        
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
            # 先尝试 UTF-8
            return text.encode('latin1').decode('utf-8')
        except:
            try:
                # 尝试 GBK
                return text.encode('latin1').decode('gbk')
            except:
                return text
    
    def _is_quality_post(self, item: Dict) -> tuple:
        """
        判断是否为高质量帖子，并返回质量等级
        
        Returns:
            tuple: (是否通过筛选, 质量等级)
            质量等级: 'elite'(精华) / 'good'(优质) / 'normal'(普通) / None(不合格)
        """
        # 获取阅读数和评论数
        click_count = item.get('post_click_count', 0)
        comment_count = item.get('post_comment_count', 0)
        
        # 获取标题
        title = item.get('post_title', '')
        
        # 获取达人标记
        v_user_code = item.get('v_user_code', 0)
        
        # 获取精华标记
        is_essence = item.get('is_essence', False) or item.get('post_essence', False)
        
        # 基础筛选：排除短标题
        if len(title) < self.min_title_length:
            return False, None
        
        # 判断条件
        is_vip = v_user_code and v_user_code != 0
        is_hot = click_count > 500 or comment_count > 20
        is_moderate = click_count > 100 and comment_count > 5 and len(title) > 10
        
        # 精华帖标准（满足任一即可）
        if is_essence or (is_vip and is_hot) or (click_count > 1000 and comment_count > 50):
            return True, 'elite'
        
        # 优质帖标准
        if is_vip or is_hot or (is_moderate and len(title) > 15):
            return True, 'good'
        
        # 普通帖标准（放宽条件）
        if click_count > 50 and comment_count > 2 and len(title) > 10:
            return True, 'normal'
        
        return False, None
    
    def fetch_fund_posts(self, fund_code: str, quality_filter: bool = True) -> List[Dict]:
        """
        抓取指定基金的热门帖子
        
        Args:
            fund_code: 基金代码
            quality_filter: 是否启用高质量筛选
        
        Returns:
            帖子列表
        """
        if not config.CRAWLER_ENABLED:
            print("[Crawler] 爬虫模块未启用，跳过抓取")
            return []
        
        posts = []
        
        try:
            url = f"{self.base_url}/list,{fund_code}.html"
            
            self._rate_limit()
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                print(f"[Crawler] 抓取基金{fund_code}吧失败：HTTP {response.status_code}")
                return []
            
            # 使用正则表达式提取 JSON 数据
            json_match = re.search(r'var article_list=(\{.*?\});', response.text, re.DOTALL)
            
            if json_match:
                try:
                    article_data = json.loads(json_match.group(1))
                    post_items = article_data.get('re', [])
                    
                    print(f"[Crawler] 解析到 {len(post_items)} 条帖子，启用质量筛选: {quality_filter}")
                    
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
                    
                    print(f"[Crawler] 筛选结果：精华 {len(elite_posts)} 条，优质 {len(good_posts)} 条，普通 {len(normal_posts)} 条")
                    
                    for item in filtered_posts[:self.max_posts]:
                        try:
                            post_data = self._parse_json_post(item, fund_code)
                            if post_data:
                                posts.append(post_data)
                        except Exception as e:
                            print(f"[Crawler] 解析帖子失败：{e}")
                            continue
                            
                except json.JSONDecodeError as e:
                    print(f"[Crawler] 解析 JSON 失败：{e}")
            
            print(f"[Crawler] 成功抓取基金{fund_code}吧 {len(posts)} 条高质量帖子")
            
        except requests.exceptions.Timeout:
            print(f"[Crawler] 抓取基金{fund_code}吧超时")
        except requests.exceptions.RequestException as e:
            print(f"[Crawler] 抓取基金{fund_code}吧网络错误：{e}")
        except Exception as e:
            print(f"[Crawler] 抓取基金{fund_code}吧异常：{e}")
        
        return posts
    
    def _parse_json_post(self, item: Dict, fund_code: str) -> Optional[Dict]:
        """解析 JSON 格式的帖子数据"""
        try:
            post_id = str(item.get('post_id', ''))
            title = item.get('post_title', '')
            
            # 修复编码
            title = self._fix_encoding(title)
            
            # 匿名处理：统称为网友
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
            url = f"https://guba.eastmoney.com/news,{fund_code},{post_id}.html"
            
            # 默认使用标题作为内容
            content = title
            
            return {
                'post_id': post_id,
                'fund_code': fund_code,
                'title': title,
                'content': content,
                'author': author,
                'is_vip': is_vip,  # 达人标记
                'is_essence': is_essence,  # 精华标记
                'quality_level': quality_level,  # 质量等级
                'read_count': read_count,
                'reply_count': reply_count,
                'post_time': post_time,
                'url': url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"[Crawler] 解析 JSON 帖子失败：{e}")
            return None
    
    def _parse_time(self, time_str: str) -> str:
        """解析时间字符串"""
        if not time_str:
            return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # 尝试多种格式
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
    
    def fetch_all_active_funds(self, fund_codes: List[str], quality_filter: bool = True) -> Dict[str, List[Dict]]:
        """批量抓取多个基金的帖子"""
        results = {}
        
        print(f"[Crawler] 开始抓取 {len(fund_codes)} 个基金的帖子...")
        
        for i, fund_code in enumerate(fund_codes, 1):
            print(f"[Crawler] [{i}/{len(fund_codes)}] 抓取基金 {fund_code}")
            posts = self.fetch_fund_posts(fund_code, quality_filter)
            results[fund_code] = posts
            
            if i < len(fund_codes):
                time.sleep(self.request_delay)
        
        print(f"[Crawler] 批量抓取完成")
        return results


# 单例
crawler = TiantianCrawler()


if __name__ == '__main__':
    import os
    os.environ['CRAWLER_ENABLED'] = 'true'
    
    test_fund = '000001'
    print(f"测试抓取基金 {test_fund} 吧（启用质量筛选）...")
    
    posts = crawler.fetch_fund_posts(test_fund, quality_filter=True)
    
    print(f"\n抓取到 {len(posts)} 条高质量帖子:\n")
    for i, post in enumerate(posts[:5], 1):
        vip_mark = "【达人】" if post.get('is_vip') else ""
        print(f"{i}. {vip_mark}{post['title']}")
        print(f"   阅读：{post['read_count']}, 回复：{post['reply_count']}")
        print(f"   作者：{post['author']}")
        print()
