"""
爬虫模块单元测试
"""
import pytest


class TestQualityFilter:
    """质量筛选器测试"""
    
    def test_filter_high_quality(self):
        """测试筛选高质量帖子"""
        from src.crawler.filters import QualityFilter
        
        filter = QualityFilter(min_read_count=100, min_content_length=50)
        
        posts = [
            {
                "title": "测试帖子1",
                "content": "这是一篇高质量的帖子内容，内容长度超过50个字符，应该通过筛选。" * 2,
                "read_count": 500,
                "comment_count": 10
            },
            {
                "title": "测试帖子2",
                "content": "短内容",
                "read_count": 50,
                "comment_count": 2
            }
        ]
        
        high_quality, low_quality = filter.filter(posts)
        
        assert len(high_quality) == 1
        assert len(low_quality) == 1
    
    def test_spam_detection(self):
        """测试垃圾内容检测"""
        from src.crawler.filters import QualityFilter
        
        filter = QualityFilter()
        
        spam_post = {
            "content": "加群获取牛股，代客理财，内幕消息",
            "read_count": 1000
        }
        
        high_quality, low_quality = filter.filter([spam_post])
        
        assert len(low_quality) == 1
    
    def test_calculate_quality_score(self):
        """测试质量分数计算"""
        from src.crawler.filters import QualityFilter
        
        filter = QualityFilter()
        
        post = {
            "content": "这是一篇较长的优质内容" * 50,
            "read_count": 5000,
            "comment_count": 30
        }
        
        score = filter.calculate_quality_score(post)
        
        assert score >= 60


class TestAIFilter:
    """AI 筛选器测试"""
    
    def test_filter_without_analyzer(self):
        """测试无分析器时的筛选"""
        from src.crawler.filters import AIFilter
        
        filter = AIFilter(analyzer=None)
        
        posts = [{"title": "测试", "content": "内容"}]
        
        passed, failed = filter.filter(posts)
        
        assert len(passed) == 1
        assert len(failed) == 0
    
    def test_analyze_without_analyzer(self):
        """测试无分析器时的分析"""
        from src.crawler.filters import AIFilter
        
        filter = AIFilter(analyzer=None)
        
        result = filter.analyze({"title": "测试", "content": "内容"})
        
        assert result is None


class TestBaseCrawler:
    """爬虫基类测试"""
    
    def test_parse_html(self):
        """测试 HTML 解析"""
        from src.crawler.base import BaseCrawler
        from bs4 import BeautifulSoup
        
        class TestCrawler(BaseCrawler):
            def fetch(self, **kwargs):
                return []
            
            def parse(self, html, **kwargs):
                return []
        
        crawler = TestCrawler()
        html = "<html><body><h1>Test</h1></body></html>"
        
        soup = crawler.parse_html(html)
        
        assert soup.find('h1').text == "Test"
