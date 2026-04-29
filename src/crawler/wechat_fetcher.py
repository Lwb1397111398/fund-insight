"""
微信文章抓取服务
用于从微信公众号文章URL抓取内容
Render 等云平台不支持 Playwright，直接使用 HTTP 模式
"""
import re
import random
from datetime import datetime
from typing import Optional, Dict
from bs4 import BeautifulSoup
import requests


class WeChatArticleFetcher:
    """微信文章抓取器，使用 HTTP 模式"""

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    HTTP_HEADERS = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0',
    }

    def __init__(self):
        pass

    async def fetch(self, url: str) -> Optional[Dict]:
        if not url or 'mp.weixin.qq.com' not in url:
            return None

        return await self._fetch_with_http(url)

    async def _fetch_with_http(self, url: str) -> Optional[Dict]:
        try:
            headers = dict(self.HTTP_HEADERS)
            headers['User-Agent'] = random.choice(self.USER_AGENTS)
            headers['Referer'] = 'https://mp.weixin.qq.com/'

            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()

            resp.encoding = 'utf-8'
            html = resp.text
            soup = BeautifulSoup(html, 'html.parser')

            title = self._extract_title(soup)
            author = self._extract_author(soup)
            publish_time = self._extract_publish_time(soup)
            article_content = self._extract_content(soup)

            if not title or not article_content:
                var_content = self._extract_from_js_var(html)
                if var_content and var_content.get('content'):
                    title = title or var_content.get('title', '')
                    article_content = var_content['content']
                    author = author or var_content.get('author', '未知博主')

            if not title or not article_content:
                print("[WeChatFetcher] HTTP 模式无法提取文章内容（可能被反爬拦截）")
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
            print(f"[WeChatFetcher] HTTP 请求失败: {e}")
            return None
        except Exception as e:
            print(f"[WeChatFetcher] HTTP 解析失败: {e}")
            return None

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

        content_match = re.search(r'var\s+msg_content\s*=\s*[\'"](.+?)[\'"]\s*;', html, re.DOTALL)
        if content_match:
            raw = content_match.group(1)
            raw = re.sub(r'<[^>]+>', '', raw)
            raw = raw.replace('\\n', '\n').replace('\\t', '').replace('&nbsp;', ' ')
            raw = re.sub(r'\n{3,}', '\n\n', raw)
            raw = raw.strip()
            if len(raw) > 50:
                result['content'] = raw

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
