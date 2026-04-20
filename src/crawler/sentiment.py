"""
爬虫情绪分析工具
提供基于关键词的情绪分析和板块识别功能
"""
from typing import Dict, List, Tuple
import re


class SentimentAnalyzer:
    """情绪分析器"""
    
    BULLISH_WORDS = [
        '上涨', '涨停', '大涨', '看好', '买入', '增持', '反弹', '突破',
        '走强', '拉升', '暴涨', '新高', '机会', '利好', '看多', '推荐',
        '强势', '上涨趋势', '牛市', '底部', '支撑', '企稳', '回暖'
    ]
    
    BEARISH_WORDS = [
        '下跌', '跌停', '大跌', '看空', '卖出', '减持', '破位', '暴跌',
        '新低', '风险', '利空', '看淡', '弱势', '下跌趋势', '熊市', '顶部',
        '压力', '回调', '调整', '下跌中继', '止损'
    ]
    
    SECTOR_KEYWORDS = {
        '科技': ['科技', '芯片', '半导体', '人工智能', 'AI', '5G', '云计算', '大数据', '软件', '硬件'],
        '医药': ['医药', '医疗', '药品', '疫苗', '生物', '中药', '西药', '医疗器械', '创新药'],
        '消费': ['消费', '白酒', '食品', '饮料', '家电', '零售', '电商', '品牌'],
        '新能源': ['新能源', '光伏', '风电', '锂电', '储能', '电动车', '充电桩', '清洁能源'],
        '金融': ['银行', '保险', '证券', '券商', '金融', '信托', '基金'],
        '地产': ['地产', '房地产', '房产', '物业', '商业地产', '住宅'],
        '军工': ['军工', '国防', '航天', '航空', '兵器'],
        '农业': ['农业', '种植', '养殖', '农产品', '种子', '化肥']
    }
    
    @classmethod
    def analyze_sentiment(cls, text: str) -> Tuple[str, int]:
        """
        分析文本情绪
        
        Args:
            text: 待分析文本
        
        Returns:
            (情绪方向, 置信度)
            情绪方向: 'bullish', 'bearish', 'neutral'
            置信度: 0-100
        """
        if not text:
            return 'neutral', 50
        
        text_lower = text.lower()
        
        bullish_count = sum(1 for word in cls.BULLISH_WORDS if word in text_lower)
        bearish_count = sum(1 for word in cls.BEARISH_WORDS if word in text_lower)
        
        total_signals = bullish_count + bearish_count
        
        if total_signals == 0:
            return 'neutral', 50
        
        if bullish_count > bearish_count:
            confidence = min(90, 50 + bullish_count * 5)
            return 'bullish', confidence
        elif bearish_count > bullish_count:
            confidence = min(90, 50 + bearish_count * 5)
            return 'bearish', confidence
        else:
            return 'neutral', 50
    
    @classmethod
    def detect_sectors(cls, text: str) -> List[str]:
        """
        检测文本涉及的板块
        
        Args:
            text: 待分析文本
        
        Returns:
            板块列表
        """
        if not text:
            return []
        
        text_lower = text.lower()
        detected_sectors = []
        
        for sector, keywords in cls.SECTOR_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                detected_sectors.append(sector)
        
        return detected_sectors
    
    @classmethod
    def analyze_article(cls, title: str, content: str) -> Dict:
        """
        分析文章的情绪和板块
        
        Args:
            title: 文章标题
            content: 文章内容
        
        Returns:
            分析结果字典
        """
        full_text = f"{title} {content}"
        
        sentiment, confidence = cls.analyze_sentiment(full_text)
        sectors = cls.detect_sectors(full_text)
        
        return {
            'sentiment': sentiment,
            'confidence': confidence,
            'sectors': sectors,
            'is_adopted': confidence >= 60 and len(sectors) > 0
        }
