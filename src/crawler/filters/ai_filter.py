"""
AI 筛选器
使用 LLM 进行智能筛选
"""
from typing import List, Dict, Tuple, Optional


class AIFilter:
    """
    AI 筛选器
    
    使用 LLM 对帖子进行智能筛选和分析
    """
    
    def __init__(self, analyzer=None, min_confidence: int = 60):
        """
        初始化 AI 筛选器
        
        Args:
            analyzer: LLM 分析器实例
            min_confidence: 最小置信度阈值
        """
        self.analyzer = analyzer
        self.min_confidence = min_confidence
    
    def filter(self, posts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        AI 筛选帖子
        
        Args:
            posts: 帖子列表
            
        Returns:
            (通过筛选的帖子列表, 未通过的帖子列表)
        """
        if not self.analyzer:
            return posts, []
        
        passed = []
        failed = []
        
        for post in posts:
            analysis = self.analyze(post)
            if analysis and analysis.get('confidence', 0) >= self.min_confidence:
                post['ai_analysis'] = analysis
                passed.append(post)
            else:
                failed.append(post)
        
        return passed, failed
    
    def analyze(self, post: Dict) -> Optional[Dict]:
        """
        分析帖子
        
        Args:
            post: 帖子数据
            
        Returns:
            分析结果
        """
        if not self.analyzer:
            return None
        
        title = post.get('title', '')
        content = post.get('content', '')
        
        try:
            result = self.analyzer.analyze_post(
                title=title,
                content=content
            )
            return result
        except Exception as e:
            print(f"[AIFilter] 分析失败: {e}")
            return None
    
    def batch_analyze(self, posts: List[Dict]) -> List[Dict]:
        """
        批量分析帖子
        
        Args:
            posts: 帖子列表
            
        Returns:
            带分析结果的帖子列表
        """
        results = []
        for post in posts:
            analysis = self.analyze(post)
            if analysis:
                post['ai_analysis'] = analysis
            results.append(post)
        return results
    
    def extract_market_view(self, post: Dict) -> Optional[Dict]:
        """
        提取市场观点
        
        Args:
            post: 帖子数据
            
        Returns:
            市场观点
        """
        analysis = self.analyze(post)
        if not analysis:
            return None
        
        return {
            'direction': analysis.get('viewpoint', {}).get('market_direction', 'neutral'),
            'confidence': analysis.get('viewpoint', {}).get('confidence', 50),
            'sectors_bullish': analysis.get('viewpoint', {}).get('sectors_bullish', []),
            'sectors_bearish': analysis.get('viewpoint', {}).get('sectors_bearish', []),
            'summary': analysis.get('viewpoint', {}).get('summary', '')
        }
