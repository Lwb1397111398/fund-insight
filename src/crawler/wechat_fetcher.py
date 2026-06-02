"""
微信文章抓取服务
支持重试、反爬检测、多种内容提取策略
"""
import re
import random
import time
import logging
from datetime import datetime
from typing import Optional, Dict
from bs4 import BeautifulSoup
import requests

logger = logging.getLogger(__name__)


class WeChatArticleFetcher:
    """微信文章抓取器 - 增强版"""

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    ]

    def __init__(self):
        self.session = requests.Session()

    def _get_headers(self) -> Dict:
        """生成随机请求头"""
        return {
            'User-Agent': random.choice(self.USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
            'Referer': 'https://mp.weixin.qq.com/',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'DNT': '1',
        }

    async def fetch(self, url: str) -> Optional[Dict]:
        if not url or 'mp.weixin.qq.com' not in url:
            return None

        # 重试最多3次，每次换不同UA
        for attempt in range(3):
            if attempt > 0:
                wait = 1.5 * attempt + random.uniform(0.5, 1.5)
                logger.info(f"[WeChatFetcher] 第 {attempt + 1} 次尝试，等待 {wait:.1f} 秒")
                time.sleep(wait)

            result = self._fetch_once(url, attempt)
            if result:
                return result

            logger.warning(f"[WeChatFetcher] 第 {attempt + 1} 次尝试失败")

        logger.warning(f"[WeChatFetcher] 3次尝试全部失败: {url}")
        return None

    def _fetch_once(self, url: str, attempt: int) -> Optional[Dict]:
        """单次抓取尝试"""
        try:
            headers = self._get_headers()

            # 第二次尝试加 referer 变化
            if attempt == 1:
                headers['Referer'] = 'https://weixin.qq.com/'
            elif attempt == 2:
                headers['Referer'] = 'https://mp.weixin.qq.com/s?__biz='

            resp = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            html = resp.text

            # 检测反爬拦截页面
            if self._is_blocked_page(html):
                logger.warning(f"[WeChatFetcher] 检测到反爬拦截页面")
                return None

            soup = BeautifulSoup(html, 'html.parser')

            title = self._extract_title(soup)
            author = self._extract_author(soup)
            publish_time = self._extract_publish_time(soup)
            article_content = self._extract_content(soup)

            # 主提取失败时，尝试JS变量提取
            if not title or not article_content:
                var_content = self._extract_from_js_var(html)
                if var_content and var_content.get('content'):
                    title = title or var_content.get('title', '')
                    article_content = var_content['content']
                    author = author or var_content.get('author', '未知博主')

            # JS变量也失败时，尝试从og标签提取
            if not title or not article_content:
                og_content = self._extract_from_meta(soup)
                if og_content:
                    title = title or og_content.get('title', '')
                    if not article_content and og_content.get('description'):
                        # og:description 通常只是摘要，但聊胜于无
                        article_content = og_content['description']

            if not title or not article_content:
                return None

            return {
                'title': title,
                'author': author,
                'content': article_content,
                'publish_time': publish_time,
                'url': url,
                'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mode': 'http',
            }

        except requests.exceptions.RequestException as e:
            logger.warning(f"[WeChatFetcher] HTTP 请求失败: {e}")
            return None
        except Exception as e:
            logger.warning(f"[WeChatFetcher] 解析失败: {e}")
            return None

    def _is_blocked_page(self, html: str) -> bool:
        """检测是否为反爬拦截页面"""
        blocked_signals = [
            '请在微信客户端打开链接',
            '环境异常',
            '访问过于频繁',
            '操作频率过快',
            '请稍后再试',
            '该内容已被发布者删除',
            '此内容因违规无法查看',
            '链接已过期',
        ]
        for signal in blocked_signals:
            if signal in html:
                return True

        # 检测是否返回了验证页面（页面内容过短且无文章结构）
        if len(html) < 2000 and 'js_content' not in html and 'rich_media_content' not in html:
            # 可能是验证页面或空页面
            if 'verify' in html.lower() or 'captcha' in html.lower():
                return True

        return False

    def _extract_from_meta(self, soup: BeautifulSoup) -> Optional[Dict]:
        """从meta标签提取信息（备用方案）"""
        result = {}
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            result['title'] = og_title['content'].strip()

        og_desc = soup.find('meta', property='og:description')
        if og_desc and og_desc.get('content'):
            result['description'] = og_desc['content'].strip()

        og_author = soup.find('meta', {'name': 'author'})
        if og_author and og_author.get('content'):
            result['author'] = og_author['content'].strip()

        return result if result else None

    def _extract_from_js_var(self, html: str) -> Optional[Dict]:
        result = {}

        title_match = re.search(r"var\s+msg_title\s*=\s*'([^']*)'", html)
        if not title_match:
            title_match = re.search(r'var\s+msg_title\s*=\s*"([^"]*)"', html)
        if title_match:
            result['title'] = title_match.group(1).strip()

        author_match = re.search(r"var\s+nickname\s*=\s*'([^']*)'", html)
        if not author_match:
            author_match = re.search(r'var\s+nickname\s*=\s*"([^"]*)"', html)
        if author_match:
            result['author'] = author_match.group(1).strip()

        # 尝试多种内容提取模式
        for pattern in [
            r'var\s+msg_content\s*=\s*[\'"](.+?)[\'"]\s*;',
            r'var\s+content\s*=\s*[\'"](.+?)[\'"]\s*;',
            r'id="js_content"[^>]*>(.*?)</div>',
        ]:
            content_match = re.search(pattern, html, re.DOTALL)
            if content_match:
                raw = content_match.group(1)
                raw = re.sub(r'<[^>]+>', '', raw)
                raw = raw.replace('\\n', '\n').replace('\\t', '').replace('&nbsp;', ' ')
                raw = re.sub(r'\n{3,}', '\n\n', raw)
                raw = raw.strip()
                if len(raw) > 50:
                    result['content'] = raw
                    break

        return result if result else None

    def _extract_title(self, soup: BeautifulSoup) -> str:
        for selector in [
            ('h1', {'class_': 'rich_media_title'}),
            ('h1', {'id': 'activity-name'}),
            ('h1', {}),
        ]:
            elem = soup.find(*selector) if selector[1] else soup.find(selector[0])
            if elem:
                return elem.get_text(strip=True)
        return ''

    def _extract_author(self, soup: BeautifulSoup) -> str:
        for selector in [
            ('a', {'id': 'js_name'}),
            ('span', {'class_': 'rich_media_meta_nickname'}),
            ('a', {'class_': 'rich_media_nickname'}),
        ]:
            elem = soup.find(*selector)
            if elem:
                text = elem.get_text(strip=True)
                if text:
                    return text
        return '未知博主'

    def _extract_publish_time(self, soup: BeautifulSoup) -> str:
        for selector in [
            ('em', {'id': 'publish_time'}),
            ('span', {'class_': 'rich_media_meta_date'}),
        ]:
            elem = soup.find(*selector)
            if elem:
                return elem.get_text(strip=True)
        return ''

    def _extract_content(self, soup: BeautifulSoup) -> str:
        content_elem = soup.find('div', id='js_content') or \
                      soup.find('div', class_='rich_media_content')

        if not content_elem:
            return ''

        for tag in content_elem(['script', 'style', 'iframe', 'svg', 'noscript']):
            tag.decompose()

        for img in content_elem.find_all('img'):
            img.decompose()

        for a in content_elem.find_all('a'):
            a.unwrap()

        text = content_elem.get_text(separator='\n', strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)

        return text


wechat_fetcher = WeChatArticleFetcher()


def fetch_wechat_article(url: str) -> Optional[Dict]:
    import asyncio
    return asyncio.run(wechat_fetcher.fetch(url))
