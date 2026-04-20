"""
质量筛选器
筛选高质量的帖子内容
"""
from typing import List, Dict, Tuple
import re


class QualityFilter:
    """
    质量筛选器
    
    根据阅读量、评论数、内容长度等指标筛选高质量帖子
    """
    
    def __init__(
        self,
        min_read_count: int = 100,
        min_comment_count: int = 5,
        min_content_length: int = 50,
        max_content_length: int = 5000
    ):
        """
        初始化质量筛选器
        
        Args:
            min_read_count: 最小阅读量
            min_comment_count: 最小评论数
            min_content_length: 最小内容长度
            max_content_length: 最大内容长度
        """
        self.min_read_count = min_read_count
        self.min_comment_count = min_comment_count
        self.min_content_length = min_content_length
        self.max_content_length = max_content_length
        
        self.spam_patterns = [
            r'加群',
            r'加微信',
            r'加QQ',
            r'私聊',
            r'代客理财',
            r'荐股',
            r'牛股',
            r'黑马',
            r'涨停',
            r'内幕',
        ]
    
    def filter(self, posts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        筛选帖子
        
        Args:
            posts: 帖子列表
            
        Returns:
            (高质量帖子列表, 低质量帖子列表)
        """
        high_quality = []
        low_quality = []
        
        for post in posts:
            if self._is_quality(post):
                high_quality.append(post)
            else:
                low_quality.append(post)
        
        return high_quality, low_quality
    
    def _is_quality(self, post: Dict) -> bool:
        """
        判断帖子是否高质量
        
        Args:
            post: 帖子数据
            
        Returns:
            是否高质量
        """
        content = post.get('content', '') or post.get('title', '')
        
        if len(content) < self.min_content_length:
            return False
        
        if len(content) > self.max_content_length:
            return False
        
        read_count = post.get('read_count', 0) or 0
        if read_count < self.min_read_count:
            return False
        
        if self._is_spam(content):
            return False
        
        return True
    
    def _is_spam(self, content: str) -> bool:
        """
        判断是否为垃圾内容
        
        Args:
            content: 内容
            
        Returns:
            是否为垃圾内容
        """
        for pattern in self.spam_patterns:
            if re.search(pattern, content):
                return True
        return False
    
    def calculate_quality_score(self, post: Dict) -> int:
        """
        计算质量分数
        
        Args:
            post: 帖子数据
            
        Returns:
            质量分数 (0-100)
        """
        score = 50
        
        content = post.get('content', '') or ''
        content_length = len(content)
        
        if content_length >= 200:
            score += 10
        if content_length >= 500:
            score += 10
        
        read_count = post.get('read_count', 0) or 0
        if read_count >= 1000:
            score += 10
        if read_count >= 5000:
            score += 10
        
        comment_count = post.get('comment_count', 0) or 0
        if comment_count >= 10:
            score += 5
        if comment_count >= 50:
            score += 5
        
        if self._is_spam(content):
            score -= 30
        
        return max(0, min(100, score))
