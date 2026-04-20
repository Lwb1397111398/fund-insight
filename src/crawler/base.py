"""
爬虫基类
提供通用的爬虫功能
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
import time

from src.core.config import config


class BaseCrawler(ABC):
    """
    爬虫基类
    
    所有爬虫继承此类，实现 fetch 和 parse 方法
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
        self.timeout = config.CRAWLER_TIMEOUT
        self.delay = config.CRAWLER_REQUEST_DELAY
    
    @abstractmethod
    def fetch(self, **kwargs) -> List[Dict]:
        """
        抓取数据
        
        Args:
            **kwargs: 抓取参数
            
        Returns:
            抓取结果列表
        """
        pass
    
    @abstractmethod
    def parse(self, html: str, **kwargs) -> List[Dict]:
        """
        解析页面
        
        Args:
            html: HTML 内容
            **kwargs: 解析参数
            
        Returns:
            解析结果列表
        """
        pass
    
    def request(self, url: str, method: str = 'GET', **kwargs) -> Optional[str]:
        """
        发送 HTTP 请求
        
        Args:
            url: 请求 URL
            method: 请求方法
            **kwargs: 其他请求参数
            
        Returns:
            响应内容或 None
        """
        try:
            if method.upper() == 'GET':
                response = self.session.get(
                    url, 
                    timeout=kwargs.pop('timeout', self.timeout),
                    **kwargs
                )
            else:
                response = self.session.post(
                    url,
                    timeout=kwargs.pop('timeout', self.timeout),
                    **kwargs
                )
            
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            return response.text
            
        except requests.exceptions.RequestException as e:
            print(f"[Crawler] 请求失败: {url}, 错误: {e}")
            return None
    
    def request_json(self, url: str, method: str = 'GET', **kwargs) -> Optional[Dict]:
        """
        发送请求并返回 JSON
        
        Args:
            url: 请求 URL
            method: 请求方法
            **kwargs: 其他请求参数
            
        Returns:
            JSON 数据或 None
        """
        try:
            if method.upper() == 'GET':
                response = self.session.get(
                    url,
                    timeout=kwargs.pop('timeout', self.timeout),
                    **kwargs
                )
            else:
                response = self.session.post(
                    url,
                    timeout=kwargs.pop('timeout', self.timeout),
                    **kwargs
                )
            
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            print(f"[Crawler] JSON 请求失败: {url}, 错误: {e}")
            return None
    
    def sleep(self):
        """请求间隔"""
        if self.delay > 0:
            time.sleep(self.delay)
    
    def parse_html(self, html: str) -> BeautifulSoup:
        """
        解析 HTML
        
        Args:
            html: HTML 内容
            
        Returns:
            BeautifulSoup 对象
        """
        return BeautifulSoup(html, 'html.parser')
    
    def close(self):
        """关闭会话"""
        self.session.close()
