"""
天天基金吧爬虫 - 新版实现

为什么重写（2026-10-13 实测结论）：
1. 基金吧的正确地址是 list,of<基金代码>.html。旧版抓裸代码页（list,161725.html），
   000001 会撞到平安银行股票吧，ETF（512880）裸码页直接 0 条 —— 抓到的根本不是
   该基金的讨论区，这就是"热门基金吧抓不到"的根因。
2. 新版 of 吧列表页不再内嵌 `var article_list=` JSON，而是服务端渲染
   div.articleh 行（实测每页 80 条、当天帖为主，阅读/回复/时间/昵称都在行里），
   旧版正则匹配不到 JSON 就整源空转。
3. 基金吧帖详情正文容器是 .gbbody，旧版选择器只有股票吧的 .newstext 等，
   正文取不到 → 内容只剩标题 → 观点门禁判"内容过短"，采纳归零。
4. 列表按当天过滤挪到爬虫侧：观点流水线本来就会把"非当天观点"拦掉，
   旧版不筛日期导致 15 个额度一半被旧帖占据、白耗详情请求。

保留的安全特性：异常完全隔离、频率限制、手动触发。
"""
import requests
import re
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup

import sys
import os

# Fix: 只在直接运行此文件时添加路径
if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config

# 帖子时间按北京时间判断"是否当天"（与观点流水线的北京日门禁一致）
_BEIJING_TZ = timezone(timedelta(hours=8))


class TiantianCrawler:
    """天天基金吧爬虫"""

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
        self.fetch_detail = True
        # 列表阶段就只保留当天（北京日）帖子；解析失败按当天放行，交给门禁终审。
        self.today_only = True

        # 筛选参数 - 适度宽松，保证观点数量
        self.min_click_count = 30   # 最小阅读数
        self.min_comment_count = 1  # 最小评论数
        self.min_title_length = 6   # 最小标题长度

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
        except Exception:
            try:
                return text.encode('latin1').decode('gbk')
            except Exception:
                return text

    @staticmethod
    def _parse_count(value) -> int:
        """把 '310' / '1.2万' / '399,677' 之类的计数统一成整数。"""
        text = str(value or "").strip().replace(",", "")
        if not text:
            return 0
        try:
            if text.endswith('万'):
                return int(float(text[:-1]) * 10000)
            return int(float(text))
        except ValueError:
            return 0

    def _board_time_to_full(self, time_text: str, now: Optional[datetime] = None) -> str:
        """
        列表行时间 'MM-DD HH:MM'（无年份）补全为 'YYYY-MM-DD HH:MM:00'。
        跨年修复：元旦后抓到 11/12 月的帖子会拼成未来日期，退回上一年。
        解析失败返回 ''（调用方按当天放行）。now 仅供测试注入"今天"。
        """
        text = str(time_text or "").strip()
        m = re.match(r'(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?', text)
        if not m:
            return ''
        month, day, hm = int(m.group(1)), int(m.group(2)), m.group(3) or '00:00'
        if len(hm) == 5:
            hm += ':00'
        if now is None:
            now = datetime.now(_BEIJING_TZ)
        try:
            candidate = datetime.strptime(
                f"{now.year}-{month:02d}-{day:02d} {hm}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=_BEIJING_TZ)
        except ValueError:
            return ''
        if candidate > now + timedelta(days=1):
            candidate = candidate.replace(year=now.year - 1)
        return candidate.strftime('%Y-%m-%d %H:%M:%S')

    def _is_before_today(self, time_str: str) -> bool:
        """时间是否早于当天（北京日）。空串/解析失败按"不是旧帖"放行。"""
        if not time_str:
            return False
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return dt.date() < datetime.now(_BEIJING_TZ).date()

    def _is_quality_post(self, item: Dict) -> tuple:
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

        if len(title) < self.min_title_length:
            return False, None

        is_vip = v_user_code and v_user_code != 0
        is_hot = click_count > 300 or comment_count > 15

        # 精华帖标准（满足任一即可）
        if is_essence or (is_vip and is_hot) or (click_count > 500 and comment_count > 20):
            return True, 'elite'

        # 优质帖标准（降低阈值）
        if is_vip or is_hot or (click_count > 100 and comment_count > 3):
            return True, 'good'

        # 普通帖标准（大幅放宽条件）
        if click_count > self.min_click_count and comment_count >= self.min_comment_count:
            return True, 'normal'

        return False, None

    def _fetch_post_detail(self, url: str) -> Tuple[str, str]:
        """
        抓取帖子详情，返回 (正文, 权威发布时间)。

        优先读详情页内嵌的 `var post_article=` JSON（正文在 post.post_content，
        时间是真实发帖时间）；列表行的 .l5 往往是"最后回复时间"，把昨天的
        顶帖显示成今天，必须以详情 JSON 为准。DOM 选择器只做兜底
        （.gbbody 实测是页头导航，不能用）。拿不到返回 ('', '')。
        """
        if not url:
            return "", ""
        try:
            self._rate_limit()
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = "utf-8"
            if response.status_code != 200 or not response.text:
                return "", ""
            m = re.search(r'var post_article\s*=\s*(\{.*?\});', response.text, re.DOTALL)
            if m:
                try:
                    post = json.loads(m.group(1)).get('post', {})
                except json.JSONDecodeError:
                    post = {}
                if post:
                    raw = post.get('post_content', '') or ''
                    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if raw else ''
                    time_str = str(post.get('post_publish_time', '') or '')[:19]
                    return text, time_str
            soup = BeautifulSoup(response.text, "html.parser")
            for selector in (".zwcontentmain", ".newstext", "#zwconbody"):
                node = soup.select_one(selector)
                if not node:
                    continue
                text = node.get_text("\n", strip=True)
                if text:
                    return text, ""
        except Exception as exc:
            print(f"[Crawler] 获取基金吧正文失败: {exc}")
        return "", ""

    @staticmethod
    def _absolutize_href(href: str) -> str:
        """'/news,of161725,123.html'、'//caifuhao...' 都变可请求的绝对地址。"""
        href = (href or "").strip()
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return "https://guba.eastmoney.com" + href
        return href

    def _parse_dom_posts(self, html: str) -> List[Dict]:
        """解析新版列表页的服务端渲染行（div.articleh）。"""
        soup = BeautifulSoup(html, 'html.parser')
        posts = []
        for row in soup.select('div.articleh'):
            link = row.select_one('.l3 a')
            if not link:
                continue
            title = link.get_text(strip=True)
            href = self._absolutize_href(link.get('href', ''))
            post_id = ''
            m = re.search(r'(\d{6,})(?:\.html)?$', href)
            if m:
                post_id = m.group(1)
            l1 = row.select_one('.l1')
            l2 = row.select_one('.l2')
            l4 = row.select_one('.l4')
            l5 = row.select_one('.l5')
            time_str = self._board_time_to_full(l5.get_text(strip=True) if l5 else '')
            posts.append({
                'post_id': post_id or href,
                'post_title': title,
                'post_click_count': self._parse_count(l1.get_text(strip=True) if l1 else 0),
                'post_comment_count': self._parse_count(l2.get_text(strip=True) if l2 else 0),
                'post_publish_time': time_str,
                'post_author': l4.get_text(strip=True) if l4 else '',
                'post_url': href,
                '_from_dom': True,
            })
        return posts

    def _fetch_via_json(self, fund_code: str, html: str) -> List[Dict]:
        """旧版兜底：列表页内嵌 var article_list= JSON（部分老页仍生效）。
        直接解析已取回的 HTML，不再重复发请求（避免撞反爬时双倍打点）。"""
        json_match = re.search(r'var article_list=(\{.*?\});', html, re.DOTALL)
        if not json_match:
            return []
        try:
            article_data = json.loads(json_match.group(1))
            return [item for item in article_data.get('re', []) if isinstance(item, dict)]
        except json.JSONDecodeError as e:
            print(f"[Crawler] 解析 JSON 失败：{e}")
            return []

    def fetch_fund_posts(self, fund_code: str, quality_filter: bool = True) -> List[Dict]:
        """
        抓取指定基金的热门帖子（天天基金吧 list,of<code>.html）

        Args:
            fund_code: 基金代码
            quality_filter: 是否启用高质量筛选

        Returns:
            帖子列表（字段形状与旧版一致，观点流水线无需改动）
        """
        # 不在这里挡 CRAWLER_ENABLED：
        # 观点流水线 fetch 是用户手动触发，需与 stock_guba/sina 行为一致。
        posts: List[Dict] = []

        try:
            url = f"{self.base_url}/list,of{fund_code}.html"
            self._rate_limit()
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'

            if response.status_code != 200:
                print(f"[Crawler] 抓取基金{fund_code}吧失败：HTTP {response.status_code}")
                return []

            raw_items = self._parse_dom_posts(response.text)
            if not raw_items:
                raw_items = self._fetch_via_json(fund_code, response.text)
            if not raw_items:
                # 区分两种空列表：触发反爬验证页（稍后重试/降频）vs 吧本身没帖
                if len(response.text) < 10000 or 'captcha' in response.text.lower():
                    print(f"[Crawler] 基金{fund_code}吧疑似触发东财反爬验证（非帖子为空），稍后重试或降低频率")
                    return []
                print(f"[Crawler] 基金{fund_code}吧列表解析为空（DOM 与 JSON 都没有）")
                return []

            # 当天过滤（列表阶段做，别让旧帖占详情请求和额度）
            if self.today_only:
                before = len(raw_items)
                raw_items = [
                    item for item in raw_items
                    if not self._is_before_today(item.get('post_publish_time', ''))
                ]
                if before != len(raw_items):
                    print(f"[Crawler] 基金{fund_code}吧当天过滤：{before} → {len(raw_items)} 条")

            # 筛选高质量帖子并分类
            elite_posts, good_posts, normal_posts = [], [], []
            for item in raw_items:
                should_include, quality_level = self._is_quality_post(item) if quality_filter else (True, 'normal')
                if should_include:
                    item['_quality_level'] = quality_level
                    if quality_level == 'elite':
                        elite_posts.append(item)
                    elif quality_level == 'good':
                        good_posts.append(item)
                    else:
                        normal_posts.append(item)

            filtered_posts = elite_posts + good_posts + normal_posts
            print(
                f"[Crawler] 基金{fund_code}吧筛选结果："
                f"精华 {len(elite_posts)} 条，优质 {len(good_posts)} 条，普通 {len(normal_posts)} 条"
            )

            # 逐条补详情再决定收不收：详情 JSON 的发帖时间是权威值，
            # "今天被回复顶起的昨天旧帖"在这里拦掉，并继续用后面的候选补满额度。
            for item in filtered_posts:
                if len(posts) >= self.max_posts:
                    break
                try:
                    url = item.get('post_url') or \
                        f"https://guba.eastmoney.com/news,of{fund_code},{item.get('post_id', '')}.html"
                    detail_text, detail_time = ("", "")
                    if self.fetch_detail:
                        detail_text, detail_time = self._fetch_post_detail(url)
                    authoritative = detail_time or item.get('post_publish_time', '')
                    if self.today_only and self._is_before_today(authoritative):
                        continue
                    post_data = self._build_post(
                        item, fund_code,
                        content_override=detail_text, time_override=authoritative,
                    )
                    if post_data:
                        posts.append(post_data)
                except Exception as e:
                    print(f"[Crawler] 解析帖子失败：{e}")
                    continue

            print(f"[Crawler] 成功抓取基金{fund_code}吧 {len(posts)} 条高质量帖子")

        except requests.exceptions.Timeout:
            print(f"[Crawler] 抓取基金{fund_code}吧超时")
        except requests.exceptions.RequestException as e:
            print(f"[Crawler] 抓取基金{fund_code}吧网络错误：{e}")
        except Exception as e:
            print(f"[Crawler] 抓取基金{fund_code}吧异常：{e}")

        return posts

    def _build_post(self, item: Dict, fund_code: str,
                    content_override: str = "", time_override: str = "") -> Optional[Dict]:
        """把列表行（DOM 或 JSON）组装成观点流水线的帖子形状。"""
        try:
            post_id = str(item.get('post_id', '') or '')
            title = self._fix_encoding(item.get('post_title', ''))
            # 匿名处理：社区帖作者没有历史可信度，统一称"网友"
            author = "网友"

            read_count = item.get('post_click_count', 0) or 0
            reply_count = item.get('post_comment_count', 0) or 0

            post_time_str = time_override or item.get('post_publish_time', '')
            if item.get('_from_dom'):
                post_time = post_time_str or datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')
            else:
                post_time = self._parse_time(post_time_str)

            url = item.get('post_url') or f"https://guba.eastmoney.com/news,of{fund_code},{post_id}.html"

            is_vip = bool(item.get('v_user_code', 0))
            is_essence = bool(item.get('is_essence', False) or item.get('post_essence', False))
            quality_level = item.get('_quality_level', 'normal')

            # 正文由调用方补好（详情 JSON/DOM 兜底）；取不到退回标题，
            # 门禁会按"内容过短"处理纯标题帖。
            content = content_override or title

            return {
                'post_id': post_id,
                'article_id': post_id,
                'fund_code': fund_code,
                'title': title,
                'content': content,
                'author': author,
                'is_vip': is_vip,
                'is_essence': is_essence,
                'quality_level': quality_level,
                'read_count': read_count,
                'reply_count': reply_count,
                'post_time': post_time,
                'publish_time': post_time,
                'url': url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'fund_guba',
            }

        except Exception as e:
            print(f"[Crawler] 组装帖子失败：{e}")
            return None

    def _parse_time(self, time_str: str) -> str:
        """解析时间字符串（旧版 JSON 路径用）"""
        if not time_str:
            return datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')
        try:
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
                try:
                    dt = datetime.strptime(str(time_str), fmt)
                    return dt.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
            return str(time_str)
        except Exception:
            return str(time_str)

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
    os.environ['CRAWLER_ENABLED'] = 'true'

    test_fund = '161725'
    print(f"测试抓取基金 {test_fund} 吧（启用质量筛选）...")

    posts = crawler.fetch_fund_posts(test_fund, quality_filter=True)

    print(f"\n抓取到 {len(posts)} 条高质量帖子:\n")
    for i, post in enumerate(posts[:5], 1):
        print(f"{i}. {post['title']}")
        print(f"   阅读：{post['read_count']}, 回复：{post['reply_count']}, 时间：{post['publish_time']}")
        print(f"   正文前80字：{post['content'][:80]}")
        print()
