"""
爬虫增强模块
包含：去重机制、反反爬策略、内容过滤、限流保护
"""
import os
import re
import json
import time
import random
import hashlib
import logging
import threading
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

import sys
if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config

logger = logging.getLogger(__name__)


USER_AGENT_POOL = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
]


@dataclass
class CrawlerArticle:
    title: str
    content: str
    author: str
    source: str
    url: str
    article_id: str = ""
    publish_time: str = ""
    read_count: int = 0
    comment_count: int = 0
    is_vip: bool = False
    
    def to_dict(self) -> Dict:
        return {
            'title': self.title,
            'content': self.content,
            'author': self.author,
            'source': self.source,
            'url': self.url,
            'article_id': self.article_id,
            'publish_time': self.publish_time,
            'read_count': self.read_count,
            'comment_count': self.comment_count,
            'is_vip': self.is_vip
        }
    
    def get_content_hash(self) -> str:
        text = f"{self.title}:{self.content[:200]}"
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def get_unique_key(self) -> str:
        if self.article_id:
            return f"{self.source}:{self.article_id}"
        return f"{self.source}:{self.get_content_hash()}"


@dataclass
class DeduplicationConfig:
    enabled: bool = True
    use_content_hash: bool = True
    use_article_id: bool = True
    similarity_threshold: float = 0.8
    keep_strategy: str = "latest"


@dataclass 
class RateLimitConfig:
    enabled: bool = True
    min_delay: float = 2.0
    max_delay: float = 5.0
    daily_limit_per_source: int = 100
    retry_delay: float = 600.0
    max_retries: int = 3


@dataclass
class ContentFilterConfig:
    min_content_length: int = 100
    filter_ad_patterns: bool = True
    filter_disclaimer: bool = True
    ad_keywords: List[str] = field(default_factory=lambda: [
        "广告", "推广", "合作", "赞助", "软文", "付费",
        "点击链接", "扫码关注", "关注公众号", "加微信"
    ])
    disclaimer_patterns: List[str] = field(default_factory=lambda: [
        r"本文不构成投资建议",
        r"仅供参考.*?不构成建议",
        r"投资有风险.*?入市需谨慎",
        r"免责声明.*?$",
        r"风险提示.*?$"
    ])


class ArticleDeduplicator:
    """文章去重器"""
    
    def __init__(self, config: DeduplicationConfig = None):
        self.config = config or DeduplicationConfig()
        self._seen_ids: Set[str] = set()
        self._content_hashes: Set[str] = set()
        self._articles_by_hash: Dict[str, CrawlerArticle] = {}
        self._lock = threading.Lock()
        
        self._load_history()
    
    def _get_cache_file(self) -> Path:
        data_dir = Path(config.DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "crawler_dedup.json"
    
    def _load_history(self):
        cache_file = self._get_cache_file()
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._seen_ids = set(data.get('seen_ids', []))
                    self._content_hashes = set(data.get('content_hashes', []))
                logger.info(f"[Dedup] 加载历史记录: {len(self._seen_ids)} 条ID, {len(self._content_hashes)} 条哈希")
            except Exception as e:
                logger.warning(f"[Dedup] 加载历史记录失败: {e}")
    
    def _save_history(self):
        cache_file = self._get_cache_file()
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'seen_ids': list(self._seen_ids),
                    'content_hashes': list(self._content_hashes),
                    'updated_at': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Dedup] 保存历史记录失败: {e}")
    
    def is_duplicate(self, article: CrawlerArticle) -> Tuple[bool, str]:
        if not self.config.enabled:
            return False, ""
        
        with self._lock:
            if self.config.use_article_id and article.article_id:
                unique_key = article.get_unique_key()
                if unique_key in self._seen_ids:
                    return True, f"文章ID重复: {unique_key}"
            
            if self.config.use_content_hash:
                content_hash = article.get_content_hash()
                if content_hash in self._content_hashes:
                    return True, f"内容哈希重复: {content_hash[:16]}..."
            
            return False, ""
    
    def mark_seen(self, article: CrawlerArticle):
        with self._lock:
            if self.config.use_article_id and article.article_id:
                self._seen_ids.add(article.get_unique_key())
            
            if self.config.use_content_hash:
                self._content_hashes.add(article.get_content_hash())
            
            self._save_history()
    
    def check_similarity(self, article: CrawlerArticle, existing_articles: List[CrawlerArticle]) -> Tuple[bool, float]:
        if not self.config.enabled or not existing_articles:
            return False, 0.0
        
        for existing in existing_articles:
            similarity = self._calculate_similarity(article.content, existing.content)
            if similarity >= self.config.similarity_threshold:
                return True, similarity
        
        return False, 0.0
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        if not text1 or not text2:
            return 0.0
        
        words1 = set(text1)
        words2 = set(text2)
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union) if union else 0.0
    
    def clear_expired(self, days: int = 30):
        cutoff = datetime.now() - timedelta(days=days)
        logger.info(f"[Dedup] 清理 {days} 天前的记录")


class RateLimiter:
    """请求限流器"""
    
    def __init__(self, config: RateLimitConfig = None):
        self.config = config or RateLimitConfig()
        self._request_counts: Dict[str, List[float]] = defaultdict(list)
        self._failed_urls: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()
    
    def wait_if_needed(self, source: str):
        if not self.config.enabled:
            return
        
        with self._lock:
            now = time.time()
            requests_today = [
                t for t in self._request_counts[source]
                if now - t < 86400
            ]
            
            if len(requests_today) >= self.config.daily_limit_per_source:
                wait_time = 86400 - (now - requests_today[0])
                logger.warning(f"[RateLimit] {source} 已达日限额，需等待 {wait_time/3600:.1f} 小时")
                return False
            
            self._request_counts[source].append(now)
        
        delay = random.uniform(self.config.min_delay, self.config.max_delay)
        time.sleep(delay)
        return True
    
    def record_failure(self, url: str):
        with self._lock:
            count, last_time = self._failed_urls.get(url, (0, 0))
            self._failed_urls[url] = (count + 1, time.time())
    
    def should_retry(self, url: str) -> Tuple[bool, float]:
        with self._lock:
            if url not in self._failed_urls:
                return True, 0
            
            count, last_time = self._failed_urls[url]
            
            if count >= self.config.max_retries:
                logger.warning(f"[RateLimit] {url} 已达最大重试次数")
                return False, 0
            
            if time.time() - last_time < self.config.retry_delay:
                wait_time = self.config.retry_delay - (time.time() - last_time)
                return True, wait_time
            
            return True, 0
    
    def clear_old_records(self):
        now = time.time()
        with self._lock:
            self._request_counts = defaultdict(list)
            self._failed_urls = {
                url: (count, last_time)
                for url, (count, last_time) in self._failed_urls.items()
                if now - last_time < 3600
            }


class ContentFilter:
    """内容过滤器"""
    
    def __init__(self, config: ContentFilterConfig = None):
        self.config = config or ContentFilterConfig()
    
    def filter(self, article: CrawlerArticle) -> Tuple[bool, str, CrawlerArticle]:
        if len(article.content) < self.config.min_content_length:
            return False, f"内容过短: {len(article.content)} < {self.config.min_content_length}", article
        
        filtered_content = article.content
        filter_reasons = []
        
        if self.config.filter_ad_patterns:
            filtered_content, ad_count = self._filter_ads(filtered_content)
            if ad_count > 0:
                filter_reasons.append(f"过滤{ad_count}处广告内容")
        
        if self.config.filter_disclaimer:
            filtered_content, disclaimer_count = self._filter_disclaimer(filtered_content)
            if disclaimer_count > 0:
                filter_reasons.append(f"过滤{disclaimer_count}处免责声明")
        
        filtered_content = self._clean_content(filtered_content)
        
        filtered_article = CrawlerArticle(
            title=article.title,
            content=filtered_content,
            author=article.author,
            source=article.source,
            url=article.url,
            article_id=article.article_id,
            publish_time=article.publish_time,
            read_count=article.read_count,
            comment_count=article.comment_count,
            is_vip=article.is_vip
        )
        
        return True, "; ".join(filter_reasons) if filter_reasons else "通过", filtered_article
    
    def _filter_ads(self, content: str) -> Tuple[str, int]:
        count = 0
        for keyword in self.config.ad_keywords:
            if keyword in content:
                content = content.replace(keyword, "")
                count += 1
        return content, count
    
    def _filter_disclaimer(self, content: str) -> Tuple[str, int]:
        count = 0
        for pattern in self.config.disclaimer_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE | re.MULTILINE)
            if matches:
                content = re.sub(pattern, "", content, flags=re.IGNORECASE | re.MULTILINE)
                count += len(matches)
        return content, count
    
    def _clean_content(self, content: str) -> str:
        content = re.sub(r'\s+', ' ', content)
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = content.strip()
        return content
    
    def is_valid_content(self, content: str) -> Tuple[bool, str]:
        if not content:
            return False, "内容为空"
        
        if len(content) < self.config.min_content_length:
            return False, f"内容过短: {len(content)}"
        
        ad_count = sum(1 for kw in self.config.ad_keywords if kw in content)
        if ad_count > 3:
            return False, f"广告内容过多: {ad_count}处"
        
        return True, "有效"


class EnhancedCrawlerSession:
    """增强版爬虫会话"""
    
    def __init__(self):
        self.session = requests.Session()
        self._current_ua_index = 0
        self._rotate_ua()
        
        self.deduplicator = ArticleDeduplicator()
        self.rate_limiter = RateLimiter()
        self.content_filter = ContentFilter()
        
        self._stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'duplicates_skipped': 0,
            'filtered_out': 0
        }
    
    def _rotate_ua(self):
        ua = USER_AGENT_POOL[self._current_ua_index % len(USER_AGENT_POOL)]
        self.session.headers.update({
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
        self._current_ua_index += 1
        logger.debug(f"[Crawler] 切换UA: {ua[:50]}...")
    
    def request(self, url: str, source: str, method: str = 'GET', 
                timeout: int = 15, **kwargs) -> Optional[requests.Response]:
        can_proceed, wait_time = self.rate_limiter.should_retry(url)
        if not can_proceed:
            return None
        
        if wait_time > 0:
            logger.info(f"[Crawler] 等待 {wait_time:.0f} 秒后重试")
            time.sleep(wait_time)
        
        if not self.rate_limiter.wait_if_needed(source):
            return None
        
        self._rotate_ua()
        
        self._stats['total_requests'] += 1
        
        try:
            if method.upper() == 'GET':
                response = self.session.get(url, timeout=timeout, **kwargs)
            else:
                response = self.session.post(url, timeout=timeout, **kwargs)
            
            response.raise_for_status()
            self._stats['successful_requests'] += 1
            return response
            
        except requests.exceptions.RequestException as e:
            self._stats['failed_requests'] += 1
            self.rate_limiter.record_failure(url)
            logger.warning(f"[Crawler] 请求失败: {url}, 错误: {e}")
            return None
    
    def process_article(self, article: CrawlerArticle) -> Tuple[bool, str, Optional[CrawlerArticle]]:
        is_dup, dup_reason = self.deduplicator.is_duplicate(article)
        if is_dup:
            self._stats['duplicates_skipped'] += 1
            return False, dup_reason, None
        
        is_valid, filter_reason, filtered_article = self.content_filter.filter(article)
        if not is_valid:
            self._stats['filtered_out'] += 1
            return False, filter_reason, None
        
        self.deduplicator.mark_seen(article)
        
        return True, filter_reason, filtered_article
    
    def get_stats(self) -> Dict:
        return {
            **self._stats,
            'success_rate': (
                self._stats['successful_requests'] / self._stats['total_requests'] * 100
                if self._stats['total_requests'] > 0 else 0
            )
        }

    def close(self):
        """关闭底层 requests.Session，释放连接资源"""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


enhanced_session: Optional[EnhancedCrawlerSession] = None


def get_enhanced_session() -> EnhancedCrawlerSession:
    global enhanced_session
    if enhanced_session is None:
        enhanced_session = EnhancedCrawlerSession()
    return enhanced_session


def reset_enhanced_session():
    global enhanced_session
    enhanced_session = None
    logger.info("[Crawler] 增强会话已重置")
