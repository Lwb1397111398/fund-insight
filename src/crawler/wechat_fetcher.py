"""
微信文章抓取服务（简化版）
用于从微信公众号文章URL抓取内容
"""
import asyncio
import re
from datetime import datetime
from typing import Optional, Dict
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class WeChatArticleFetcher:
    """微信文章抓取器（简化版）"""
    
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    
    def __init__(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("请安装 playwright: pip install playwright && playwright install chromium")
        self._playwright = None
        self._browser = None
    
    async def _ensure_browser(self):
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
    
    async def _close_browser(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
    
    async def fetch(self, url: str) -> Optional[Dict]:
        """
        抓取微信文章
        
        Args:
            url: 微信文章URL
            
        Returns:
            文章信息字典，包含 title, author, content, publish_time
        """
        if not url or 'mp.weixin.qq.com' not in url:
            return None
        
        try:
            await self._ensure_browser()
            
            context = await self._browser.new_context(
                user_agent=self.USER_AGENTS[0],
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            
            page = await context.new_page()
            
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                await asyncio.sleep(0.5)
                
                content = await page.content()
                soup = BeautifulSoup(content, 'html.parser')
                
                title = self._extract_title(soup)
                author = self._extract_author(soup)
                publish_time = self._extract_publish_time(soup)
                article_content = self._extract_content(soup)
                
                if not title or not article_content:
                    return None
                
                return {
                    'title': title,
                    'author': author,
                    'content': article_content,
                    'publish_time': publish_time,
                    'url': url,
                    'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                
            finally:
                await page.close()
                await context.close()
                
        except Exception as e:
            print(f"[WeChatFetcher] 抓取失败: {e}")
            return None
    
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
    """
    同步接口：抓取微信文章
    
    Args:
        url: 微信文章URL
        
    Returns:
        文章信息字典
    """
    return asyncio.run(wechat_fetcher.fetch(url))
