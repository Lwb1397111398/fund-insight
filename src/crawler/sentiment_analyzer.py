"""
情绪分析器 - 基于关键词匹配的规则引擎

功能：
- 分析帖子内容的情绪倾向（看多/看空/中性）
- 提取提到的板块/行业
- 计算情绪评分（-1.0 ~ +1.0）

特点：
- 不依赖外部 NLP API
- 完全本地运行
- 快速、可配置
"""
import re
from typing import Dict, List, Optional, Tuple


class SentimentAnalyzer:
    """情绪分析器"""
    
    def __init__(self):
        # 看多关键词（正向）
        self.bullish_keywords = [
            # 强烈看多
            '暴涨', '涨停', '抄底', '买入', '加仓', '重仓', '看好', '强烈推荐',
            '机会来了', '底部', '反弹', '反转', '突破', '新高', '主升浪',
            '牛市', '做多', '进攻', '满仓', '梭哈', 'all in',
            
            # 温和看多
            '上涨', '拉升', '红盘', '阳线', '盈利', '赚钱', '吃肉',
            '价值投资', '长期持有', '定投', '布局', '建仓', '低吸',
            '潜力', '低估', '便宜', '划算', '值得', '机会', '希望',
            
            # 情绪词
            '兴奋', '激动', '期待', '开心', '爽', '给力', '牛',
            '相信', '信心', '坚定', '乐观',
        ]
        
        # 看空关键词（负向）
        self.bearish_keywords = [
            # 强烈看空
            '暴跌', '跌停', '崩盘', '割肉', '卖出', '减仓', '清仓', '跑路',
            '快跑', '要崩', '危险', '风险', '警惕', '陷阱', '泡沫',
            '熊市', '做空', '跳水', '崩了', '废了', '凉凉', '完蛋',
            
            # 温和看空
            '下跌', '回调', '调整', '绿盘', '阴线', '亏损', '亏钱', '吃面',
            '高估', '太贵', '风险大', '不看好', '回避', '谨慎', '观望',
            '压力位', '阻力', '顶部', '见顶', '下行', '走弱',
            
            # 情绪词
            '害怕', '恐惧', '焦虑', '慌了', '崩溃', '绝望', '心累',
            '后悔', '拍大腿', '踏空', '被套', '套牢',
        ]
        
        # 板块/行业关键词
        self.sector_keywords = {
            '半导体': ['半导体', '芯片', '集成电路', '中芯国际', '韦尔股份', '兆易创新'],
            '新能源': ['新能源', '光伏', '风电', '储能', '宁德时代', '比亚迪', '隆基'],
            '医药': ['医药', '医疗', '生物', '创新药', '恒瑞', '药明康德'],
            '消费': ['消费', '白酒', '食品', '饮料', '茅台', '五粮液', '伊利'],
            '科技': ['科技', '5G', '通信', '计算机', '软件', '人工智能', 'AI'],
            '金融': ['金融', '银行', '保险', '券商', '证券', '信托'],
            '地产': ['地产', '房地产', '万科', '保利', '碧桂园'],
            '有色': ['有色', '金属', '黄金', '铜', '铝', '锌', '钴', '锂'],
            '化工': ['化工', '石化', '石油', '天然气', '油气'],
            '军工': ['军工', '国防', '航天', '航空', '船舶', '兵器'],
            '电力': ['电力', '电网', '发电', '水电', '火电', '核电'],
            '汽车': ['汽车', '整车', '零部件', '特斯拉', '蔚来', '小鹏', '理想'],
            '互联网': ['互联网', '电商', '游戏', '传媒', '腾讯', '阿里', '美团'],
            '农业': ['农业', '养殖', '种植', '猪', '鸡', '豆粕', '玉米'],
            '基建': ['基建', '建筑', '建材', '水泥', '钢铁', '工程机械'],
            '卫星': ['卫星', '导航', '北斗', '航天'],
            '存储': ['存储', '内存', '闪存', '硬盘'],
            '电力设备': ['电力设备', '变压器', '开关', '电缆'],
        }
        
        # 否定词（会反转情绪）
        self.negation_keywords = ['不', '没', '无', '非', '未', '别', '莫', '勿']
        
        # 程度副词（加强情绪）
        self.degree_keywords = {
            '非常': 2.0,
            '特别': 2.0,
            '极其': 2.5,
            '太': 1.8,
            '很': 1.5,
            '挺': 1.3,
            '比较': 1.2,
            '较为': 1.2,
            '稍微': 0.8,
            '略微': 0.8,
        }
    
    def analyze(self, text: str) -> Dict:
        """
        分析文本情绪
        
        Args:
            text: 要分析的文本
        
        Returns:
            {
                'sentiment': 'bullish' | 'bearish' | 'neutral',
                'score': -1.0 ~ 1.0,  # 负数看空，正数看多
                'confidence': 0 ~ 100,  # 置信度
                'bullish_score': 0 ~ 100,  # 看多分数
                'bearish_score': 0 ~ 100,  # 看空分数
                'sectors': [],  # 提到的板块
                'keywords': [],  # 命中的关键词
            }
        """
        if not text:
            return self._neutral_result()
        
        # 转小写
        text_lower = text.lower()
        
        # 计算看多分数
        bullish_matches = self._match_keywords(text_lower, self.bullish_keywords)
        bullish_score = self._calculate_score(bullish_matches, text_lower)
        
        # 计算看空分数
        bearish_matches = self._match_keywords(text_lower, self.bearish_keywords)
        bearish_score = self._calculate_score(bearish_matches, text_lower)
        
        # 归一化
        total_score = bullish_score + bearish_score
        if total_score == 0:
            return self._neutral_result()
        
        # 计算情绪评分（-1 ~ 1）
        sentiment_score = (bullish_score - bearish_score) / total_score
        
        # 确定情绪倾向
        if sentiment_score > 0.15:
            sentiment = 'bullish'
        elif sentiment_score < -0.15:
            sentiment = 'bearish'
        else:
            sentiment = 'neutral'
        
        # 置信度（基于命中关键词数量）
        total_matches = len(bullish_matches) + len(bearish_matches)
        confidence = min(100, total_matches * 15)  # 每个关键词 15% 置信度
        
        # 提取板块
        sectors = self._extract_sectors(text)
        
        # 所有命中的关键词
        all_keywords = [m['keyword'] for m in bullish_matches + bearish_matches]
        
        return {
            'sentiment': sentiment,
            'score': round(sentiment_score, 3),
            'confidence': confidence,
            'bullish_score': round(bullish_score, 2),
            'bearish_score': round(bearish_score, 2),
            'sectors': sectors,
            'keywords': list(set(all_keywords)),
        }
    
    def _match_keywords(self, text: str, keywords: List[str]) -> List[Dict]:
        """匹配关键词并记录位置"""
        matches = []
        
        for keyword in keywords:
            # 简单匹配
            if keyword in text:
                # 检查是否有否定词
                is_negated = self._check_negation(text, keyword)
                
                # 检查程度副词
                degree = self._check_degree(text, keyword)
                
                matches.append({
                    'keyword': keyword,
                    'negated': is_negated,
                    'degree': degree,
                })
        
        return matches
    
    def _check_negation(self, text: str, keyword: str) -> bool:
        """检查关键词前是否有否定词"""
        pos = text.find(keyword)
        if pos == -1:
            return False
        
        # 检查前 5 个字符
        start = max(0, pos - 5)
        preceding_text = text[start:pos]
        
        for neg in self.negation_keywords:
            if neg in preceding_text:
                return True
        
        return False
    
    def _check_degree(self, text: str, keyword: str) -> float:
        """检查程度副词"""
        pos = text.find(keyword)
        if pos == -1:
            return 1.0
        
        # 检查前 10 个字符
        start = max(0, pos - 10)
        preceding_text = text[start:pos]
        
        for degree_word, multiplier in self.degree_keywords.items():
            if degree_word in preceding_text:
                return multiplier
        
        return 1.0
    
    def _calculate_score(self, matches: List[Dict], text: str) -> float:
        """计算分数"""
        if not matches:
            return 0.0
        
        score = 0.0
        for match in matches:
            base_score = 1.0
            
            # 否定词反转
            if match['negated']:
                base_score = -base_score
            
            # 程度副词加强
            base_score *= match['degree']
            
            score += base_score
        
        return score
    
    def _extract_sectors(self, text: str) -> List[str]:
        """提取提到的板块"""
        sectors = []
        
        for sector, keywords in self.sector_keywords.items():
            for keyword in keywords:
                if keyword in text:
                    sectors.append(sector)
                    break  # 每个板块只添加一次
        
        return list(set(sectors))
    
    def _neutral_result(self) -> Dict:
        """返回中性结果"""
        return {
            'sentiment': 'neutral',
            'score': 0.0,
            'confidence': 0,
            'bullish_score': 0.0,
            'bearish_score': 0.0,
            'sectors': [],
            'keywords': [],
        }
    
    def analyze_batch(self, texts: List[str]) -> Dict:
        """
        批量分析多个文本，返回整体情绪
        
        Args:
            texts: 文本列表
        
        Returns:
            整体情绪统计
        """
        if not texts:
            return self._neutral_result()
        
        results = [self.analyze(text) for text in texts]
        
        # 统计
        bullish_count = sum(1 for r in results if r['sentiment'] == 'bullish')
        bearish_count = sum(1 for r in results if r['sentiment'] == 'bearish')
        neutral_count = sum(1 for r in results if r['sentiment'] == 'neutral')
        
        avg_score = sum(r['score'] for r in results) / len(results)
        avg_confidence = sum(r['confidence'] for r in results) / len(results)
        
        # 所有提到的板块
        all_sectors = []
        for r in results:
            all_sectors.extend(r['sectors'])
        all_sectors = list(set(all_sectors))
        
        # 确定整体情绪
        if bullish_count > bearish_count * 1.5:
            overall_sentiment = 'bullish'
        elif bearish_count > bullish_count * 1.5:
            overall_sentiment = 'bearish'
        else:
            overall_sentiment = 'neutral'
        
        return {
            'overall_sentiment': overall_sentiment,
            'avg_score': round(avg_score, 3),
            'avg_confidence': round(avg_confidence, 1),
            'total_posts': len(results),
            'bullish_count': bullish_count,
            'bearish_count': bearish_count,
            'neutral_count': neutral_count,
            'hot_sectors': all_sectors[:10],  # 热门板块 TOP10
            'results': results,  # 详细结果
        }


# 单例
analyzer = SentimentAnalyzer()


if __name__ == '__main__':
    # 测试
    test_texts = [
        "今天半导体暴涨，我重仓吃到大肉，太爽了！继续看好芯片板块！",
        "医药又跌停了，快跑啊，要崩盘了，已经割肉离场",
        "市场震荡调整，观望为主，等待方向明确",
        "新能源板块估值合理，值得长期持有，开始定投布局",
        "白酒太高了，风险很大，建议回避，等回调再说",
    ]
    
    print("单条分析测试:\n")
    for text in test_texts:
        result = analyzer.analyze(text)
        print(f"文本：{text}")
        print(f"情绪：{result['sentiment']}, 评分：{result['score']}, 置信度：{result['confidence']}%")
        print(f"板块：{result['sectors']}")
        print(f"关键词：{result['keywords']}")
        print()
    
    print("\n批量分析测试:\n")
    batch_result = analyzer.analyze_batch(test_texts)
    print(f"整体情绪：{batch_result['overall_sentiment']}")
    print(f"平均评分：{batch_result['avg_score']}")
    print(f"看多：{batch_result['bullish_count']}, 看空：{batch_result['bearish_count']}, 中性：{batch_result['neutral_count']}")
    print(f"热门板块：{batch_result['hot_sectors']}")
