"""
帖子分析器 - 用于爬虫内容的 AI 筛选和分析

功能：
1. AI 筛选：判断帖子是否值得抓取
2. AI 分析：生成详细的分析报告
3. 多维度评分：内容深度、时效性、数据支撑、参考价值
4. 板块标准化：统一板块名称
5. 可信度分析：支撑因素评估

此模块从 crawler 迁移而来，统一 AI 分析功能入口
"""
import os
import re
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field

from src.analyzer.llm_analyzer import get_analyzer

logger = logging.getLogger(__name__)


# 板块别名映射 - 用于标准化板块名称（含黑话/昵称）
SECTOR_ALIAS_MAP = {
    # ========== 白酒相关 ==========
    "酒ETF": "白酒",
    "酒etf": "白酒",
    "酒类": "白酒",
    "酿酒": "白酒",
    "茅台": "白酒",
    "茅茅": "白酒",
    "茅大哥": "白酒",
    "老茅": "白酒",
    "五粮液": "白酒",
    "五娘": "白酒",
    "老五": "白酒",
    "泸州老窖": "白酒",
    "泸窖": "白酒",
    "汾酒": "白酒",
    "山西汾酒": "白酒",
    "洋河": "白酒",
    
    # ========== 新能源相关 ==========
    "宁王": "新能源",
    "宁德": "新能源",
    "宁德时代": "新能源",
    "迪王": "新能源",
    "比亚迪": "新能源",
    "锂电": "新能源",
    "锂电池": "新能源",
    "电车": "新能源",
    "电动车": "新能源",
    "新能源车": "新能源",
    "新能源汽车": "新能源",
    "光伏": "新能源",
    "隆基": "新能源",
    "隆基绿能": "新能源",
    "风电": "新能源",
    
    # ========== 医药相关 ==========
    "药茅": "医药",
    "药明": "医药",
    "药明康德": "医药",
    "恒瑞": "医药",
    "恒瑞医药": "医药",
    "医疗": "医药",
    "生物医药": "医药",
    "创新药": "医药",
    "中药": "医药",
    "片仔癀": "医药",
    
    # ========== 半导体/芯片相关 ==========
    "芯片": "半导体",
    "半导体": "半导体",
    "集成电路": "半导体",
    "晶圆": "半导体",
    "封测": "半导体",
    "中芯": "半导体",
    "中芯国际": "半导体",
    "北创": "半导体",
    "北方华创": "半导体",
    "韦尔": "半导体",
    "韦尔股份": "半导体",
    
    # ========== 军工相关 ==========
    "军工": "军工",
    "国防": "军工",
    "航天": "军工",
    "航空": "军工",
    
    # ========== 金融相关 ==========
    "券商": "券商",
    "证券": "券商",
    "投行": "券商",
    "中信": "券商",
    "中信证券": "券商",
    "银行": "银行",
    "招行": "银行",
    "招商银行": "银行",
    "平安": "保险",
    "中国平安": "保险",
    
    # ========== 房地产相关 ==========
    "地产": "房地产",
    "房地产": "房地产",
    "楼市": "房地产",
    "房产": "房地产",
    
    # ========== 消费相关 ==========
    "消费": "消费",
    "食品饮料": "消费",
    "家电": "消费",
    "美的": "家电",
    "格力": "家电",
    "格力电器": "家电",
    "伊利": "消费",
    "伊利股份": "消费",
    
    # ========== 科技/互联网相关 ==========
    "互联网": "互联网",
    "中概": "互联网",
    "港股科技": "互联网",
    "恒生科技": "互联网",
    "AI": "人工智能",
    "人工智能": "人工智能",
    "机器人": "人工智能",
    "ChatGPT": "人工智能",
    
    # ========== 资源相关 ==========
    "黄金": "黄金",
    "贵金属": "黄金",
    "有色": "有色金属",
    "有色金属": "有色金属",
    "稀土": "有色金属",
    "煤炭": "煤炭",
    "石油": "石油",
    "油气": "石油",
    "化工": "化工",
    "建材": "建材",
    "水泥": "建材",
}

# 来源权威度评分
SOURCE_AUTHORITY_SCORES = {
    "eastmoney_blog": 0.8,
    "eastmoney_guide": 0.7,
    "sina_finance": 0.6,
    "sina_blog": 0.5,
    "manual": 1.0,
}


@dataclass
class ScoringDimension:
    """评分维度"""
    content_depth: float = 0.0
    timeliness: float = 0.0
    data_support: float = 0.0
    reference_value: float = 0.0
    invest_actionability: float = 0.0  # 投资可操作性
    
    @property
    def total(self) -> float:
        return self.content_depth + self.timeliness + self.data_support + self.reference_value + self.invest_actionability


@dataclass
class CredibilityFactor:
    """可信度因素"""
    has_financial_data: bool = False
    has_report_citation: bool = False
    has_technical_analysis: bool = False
    is_subjective_only: bool = False
    has_policy_analysis: bool = False
    
    def calculate_bonus(self) -> Tuple[float, List[str]]:
        """计算可信度加成"""
        bonus = 0.0
        factors = []
        
        if self.has_financial_data:
            bonus += 20
            factors.append("有财报数据支撑(+20%)")
        if self.has_report_citation:
            bonus += 25
            factors.append("引用行业研报(+25%)")
        if self.has_technical_analysis:
            bonus += 15
            factors.append("有技术面分析(+15%)")
        if self.has_policy_analysis:
            bonus += 15
            factors.append("有政策分析(+15%)")
        if self.is_subjective_only:
            bonus -= 15
            factors.append("仅主观判断(-15%)")
        
        return bonus, factors


@dataclass
class PostAnalysisResult:
    """帖子分析结果"""
    should_capture: bool = False
    score: float = 0.0
    scoring_dimensions: ScoringDimension = field(default_factory=ScoringDimension)
    viewpoint_type: str = "unknown"
    reason: str = ""
    
    market_direction: str = "neutral"
    confidence: int = 50
    credibility_score: int = 50
    credibility_factors: List[str] = field(default_factory=list)
    
    sectors_bullish: List[str] = field(default_factory=list)
    sectors_bearish: List[str] = field(default_factory=list)
    sectors_normalized: List[str] = field(default_factory=list)
    
    key_points: List[str] = field(default_factory=list)
    action_suggestion: str = ""
    risk_level: str = "medium"
    time_horizon: str = "medium"
    
    analysis_summary: str = ""
    sentiment: str = "neutral"
    sentiment_score: float = 0.0
    keywords: List[str] = field(default_factory=list)
    investment_advice: Optional[str] = None


class PostAnalyzer:
    """
    帖子分析器 - 统一 AI 内容筛选和分析
    
    整合了原 crawler/ai_analyzer.py 和 crawler/enhanced_ai_analyzer.py 的功能
    """
    
    VIEWPOINT_TYPES = ["明确预测", "深度分析", "纯情绪表达", "广告", "无关内容"]
    
    def __init__(self):
        self.llm = get_analyzer()
    
    def _call_llm(self, prompt: str, max_tokens: int = 800) -> str:
        """调用 LLM（使用轻量级模型）"""
        return self.llm._call_llm(prompt, task_type='simple', max_tokens=max_tokens, temperature=0.5)
    
    def normalize_sector(self, sector: str) -> str:
        """标准化板块名称"""
        if not sector:
            return sector
        
        sector = sector.strip()
        
        if sector in SECTOR_ALIAS_MAP:
            return SECTOR_ALIAS_MAP[sector]
        
        for alias, standard in SECTOR_ALIAS_MAP.items():
            if alias in sector or sector in alias:
                return standard
        
        return sector
    
    def normalize_sectors(self, sectors: List[str]) -> List[str]:
        """批量标准化板块名称"""
        if not sectors:
            return []
        
        normalized = []
        seen = set()
        
        for sector in sectors:
            std_sector = self.normalize_sector(sector)
            if std_sector and std_sector not in seen:
                normalized.append(std_sector)
                seen.add(std_sector)
        
        return normalized
    
    def should_capture(self, post: Dict, source: str = "manual") -> PostAnalysisResult:
        """
        判断帖子是否值得抓取（增强版）
        
        Args:
            post: 帖子数据，包含 title, content, read_count, reply_count, is_vip 等
            source: 来源标识
        
        Returns:
            PostAnalysisResult: 分析结果
        """
        title = post.get('title', '')
        content = post.get('content', '')
        read_count = post.get('read_count', 0)
        reply_count = post.get('reply_count', 0)
        is_vip = post.get('is_vip', False)
        
        prompt = f"""请分析以下基金投资帖子，进行多维度评分和分类。

【帖子信息】
标题：{title}
内容：{content[:800] if content else title}
阅读数：{read_count}
评论数：{reply_count}
作者等级：{'认证达人' if is_vip else '普通用户'}

请返回JSON格式分析结果：
{{
    "scoring": {{
        "content_depth": 0-2分,
        "timeliness": 0-2分,
        "data_support": 0-2分,
        "reference_value": 0-2分,
        "invest_actionability": 0-2分
    }},
    "viewpoint_type": "明确预测/深度分析/纯情绪表达/广告/无关内容",
    "reason": "判断理由（50字以内）",
    "key_points": ["要点1", "要点2", "要点3"],
    "market_direction": "bullish/bearish/neutral",
    "confidence": 0-100,
    "sectors_bullish": ["看多板块1", "看多板块2"],
    "sectors_bearish": ["看空板块1"],
    "credibility": {{
        "has_financial_data": true/false,
        "has_report_citation": true/false,
        "has_technical_analysis": true/false,
        "is_subjective_only": true/false,
        "has_policy_analysis": true/false
    }},
    "action_suggestion": "买入/卖出/持有/观望",
    "risk_level": "high/medium/low",
    "time_horizon": "short/medium/long"
}}

评分标准：
- content_depth（内容深度）：0分=无实质内容，1分=有基本分析，2分=有深度逻辑
- timeliness（时效性）：0分=过时信息，1分=一般时效，2分=高度时效
- data_support（数据支撑）：0分=无数据，1分=有数据引用，2分=有详细数据+分析
- reference_value（参考价值）：0分=无价值，1分=有限价值，2分=有参考价值
- invest_actionability（投资可操作性）：0分=无可操作建议，1分=有模糊建议，2分=有明确买卖/持仓建议

观点类型说明：
- 明确预测：有明确的涨跌判断和目标
- 深度分析：有深入的分析逻辑
- 纯情绪表达：仅表达情绪无分析
- 广告：包含推广内容
- 无关内容：与投资无关"""

        try:
            result = self._call_llm(prompt, max_tokens=1000)
            return self._parse_capture_result(result, post, source)
        except Exception as e:
            logger.warning(f"[PostAnalyzer] AI 分析失败: {e}")
            return self._fallback_capture_result(post, source)
    
    def _parse_capture_result(self, result: str, post: Dict, source: str) -> PostAnalysisResult:
        """解析 AI 分析结果"""
        json_match = re.search(r'\{[\s\S]+\}', result)
        if not json_match:
            return self._fallback_capture_result(post, source)
        
        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return self._fallback_capture_result(post, source)
        
        scoring_data = data.get('scoring', {})
        scoring = ScoringDimension(
            content_depth=float(scoring_data.get('content_depth', 0)),
            timeliness=float(scoring_data.get('timeliness', 0)),
            data_support=float(scoring_data.get('data_support', 0)),
            reference_value=float(scoring_data.get('reference_value', 0)),
            invest_actionability=float(scoring_data.get('invest_actionability', 0))
        )
        
        credibility_data = data.get('credibility', {})
        credibility = CredibilityFactor(
            has_financial_data=credibility_data.get('has_financial_data', False),
            has_report_citation=credibility_data.get('has_report_citation', False),
            has_technical_analysis=credibility_data.get('has_technical_analysis', False),
            is_subjective_only=credibility_data.get('is_subjective_only', False),
            has_policy_analysis=credibility_data.get('has_policy_analysis', False)
        )
        credibility_bonus, credibility_factors = credibility.calculate_bonus()
        
        base_score = scoring.total
        authority_bonus = SOURCE_AUTHORITY_SCORES.get(source, 0.5) * 2
        final_score = min(10, base_score + authority_bonus)
        
        viewpoint_type = data.get('viewpoint_type', '无关内容')
        should_capture = (
            viewpoint_type in ['明确预测', '深度分析'] and
            final_score >= self._get_threshold(source)
        )
        
        sectors_bullish = self.normalize_sectors(data.get('sectors_bullish', []))
        sectors_bearish = self.normalize_sectors(data.get('sectors_bearish', []))
        
        return PostAnalysisResult(
            should_capture=should_capture,
            score=final_score,
            scoring_dimensions=scoring,
            viewpoint_type=viewpoint_type,
            reason=data.get('reason', ''),
            market_direction=data.get('market_direction', 'neutral'),
            confidence=int(data.get('confidence', 50)),
            credibility_score=min(100, max(0, 50 + int(credibility_bonus))),
            credibility_factors=credibility_factors,
            sectors_bullish=sectors_bullish,
            sectors_bearish=sectors_bearish,
            sectors_normalized=sectors_bullish + sectors_bearish,
            key_points=data.get('key_points', []),
            action_suggestion=data.get('action_suggestion', '观望'),
            risk_level=data.get('risk_level', 'medium'),
            time_horizon=data.get('time_horizon', 'medium'),
            analysis_summary=data.get('reason', '')
        )
    
    def _get_threshold(self, source: str) -> float:
        """获取抓取阈值"""
        return 7.5
    
    def _fallback_capture_result(self, post: Dict, source: str) -> PostAnalysisResult:
        """降级处理 - 基于规则的判断"""
        score = 0.0
        
        if post.get('is_vip'):
            score += 2.0
        if post.get('read_count', 0) > 1000:
            score += 1.5
        if post.get('reply_count', 0) > 20:
            score += 1.5
        if len(post.get('content', '')) > 100:
            score += 1.0
        
        authority_bonus = SOURCE_AUTHORITY_SCORES.get(source, 0.5) * 2
        final_score = min(10, score + authority_bonus)
        
        threshold = self._get_threshold(source)
        should_capture = final_score >= threshold
        
        return PostAnalysisResult(
            should_capture=should_capture,
            score=final_score,
            scoring_dimensions=ScoringDimension(
                content_depth=score * 0.25,
                timeliness=score * 0.25,
                data_support=score * 0.25,
                reference_value=score * 0.25
            ),
            viewpoint_type="深度分析" if should_capture else "无关内容",
            reason="基于规则的降级判断",
            market_direction="neutral",
            confidence=50,
            credibility_score=50,
            credibility_factors=["降级判断"],
            sectors_bullish=[],
            sectors_bearish=[],
            sectors_normalized=[],
            key_points=[],
            action_suggestion="观望",
            risk_level="medium",
            time_horizon="medium",
            analysis_summary="降级分析结果"
        )
    
    def analyze_post_simple(self, post: Dict) -> Dict:
        """
        简单分析帖子（兼容旧版接口）
        
        Args:
            post: 帖子数据
        
        Returns:
            Dict: 分析结果字典
        """
        result = self.should_capture(post)
        
        if not result.should_capture:
            return {
                'should_capture': False,
                'reason': result.reason,
                'score': result.score,
                'category': result.viewpoint_type,
                'sentiment': 'neutral',
                'sentiment_score': 0.0,
                'sectors': [],
                'keywords': [],
                'investment_advice': None,
            }
        
        return {
            'should_capture': result.should_capture,
            'reason': result.reason,
            'score': result.score,
            'category': result.viewpoint_type,
            'sentiment': result.market_direction,
            'sentiment_score': result.confidence / 100.0,
            'sectors': result.sectors_normalized,
            'keywords': result.key_points,
            'investment_advice': result.action_suggestion,
        }
    
    def analyze_viewpoint_deep(self, title: str, content: str, author: str = "", 
                                 source: str = "manual") -> PostAnalysisResult:
        """深度分析观点"""
        prompt = f"""请对以下投资观点进行深度分析。

【观点信息】
标题：{title}
内容：{content[:1000]}
作者：{author}
来源：{source}

请返回JSON格式深度分析：
{{
    "scoring": {{
        "content_depth": 0-2分,
        "timeliness": 0-2分,
        "data_support": 0-3分,
        "reference_value": 0-3分
    }},
    "viewpoint_type": "明确预测/深度分析/纯情绪表达/广告/无关内容",
    "market_direction": "bullish/bearish/neutral",
    "confidence": 0-100,
    "credibility": {{
        "has_financial_data": true/false,
        "has_report_citation": true/false,
        "has_technical_analysis": true/false,
        "is_subjective_only": true/false,
        "has_policy_analysis": true/false
    }},
    "sectors_bullish": ["看多板块1", "看多板块2"],
    "sectors_bearish": ["看空板块1"],
    "key_points": ["核心要点1", "核心要点2", "核心要点3"],
    "analysis": "深度分析（100字以内）",
    "reasoning": "判断理由（80字以内）",
    "action_suggestion": "买入/卖出/持有/观望",
    "risk_level": "high/medium/low",
    "time_horizon": "short/medium/long"
}}"""

        try:
            result = self._call_llm(prompt, max_tokens=1200)
            analysis = self._parse_capture_result(result, {'title': title, 'content': content}, source)
            analysis.should_capture = True
            analysis.analysis_summary = f"【AI深度分析】{self._extract_field(result, 'analysis')}\n\n【判断理由】{self._extract_field(result, 'reasoning')}"
            return analysis
        except Exception as e:
            logger.warning(f"[PostAnalyzer] 深度分析失败: {e}")
            return self._fallback_capture_result({'title': title, 'content': content}, source)
    
    def _extract_field(self, text: str, field: str) -> str:
        """从文本中提取字段"""
        pattern = rf'"{field}"\s*:\s*"([^"]*)"'
        match = re.search(pattern, text)
        return match.group(1) if match else ""
    
    def batch_analyze(self, posts: List[Dict], source: str = "manual") -> List[Dict]:
        """批量分析帖子"""
        results = []
        for post in posts:
            analysis = self.should_capture(post, source)
            results.append({
                **post,
                'ai_analysis': {
                    'should_capture': analysis.should_capture,
                    'score': analysis.score,
                    'viewpoint_type': analysis.viewpoint_type,
                    'market_direction': analysis.market_direction,
                    'confidence': analysis.confidence,
                    'credibility_score': analysis.credibility_score,
                    'credibility_factors': analysis.credibility_factors,
                    'sectors_bullish': analysis.sectors_bullish,
                    'sectors_bearish': analysis.sectors_bearish,
                    'key_points': analysis.key_points,
                    'action_suggestion': analysis.action_suggestion,
                    'risk_level': analysis.risk_level,
                    'time_horizon': analysis.time_horizon
                }
            })
        return results
    
    def get_worthwhile_posts(self, posts: List[Dict], source: str = "manual") -> List[Dict]:
        """只返回值得抓取的帖子"""
        results = []
        for post in posts:
            analysis = self.should_capture(post, source)
            if analysis.should_capture:
                results.append({
                    **post,
                    'ai_analysis': analysis
                })
        return results


# 单例模式
_post_analyzer: Optional[PostAnalyzer] = None


def get_post_analyzer() -> PostAnalyzer:
    """获取帖子分析器单例"""
    global _post_analyzer
    if _post_analyzer is None:
        _post_analyzer = PostAnalyzer()
    return _post_analyzer


# 兼容性接口 - 保持与旧版 ai_analyzer.py 兼容
AIPostAnalyzer = PostAnalyzer
ai_analyzer = get_post_analyzer()

# 兼容性接口 - 保持与旧版 enhanced_ai_analyzer.py 兼容
EnhancedAIAnalyzer = PostAnalyzer
get_enhanced_analyzer = get_post_analyzer
EnhancedAnalysisResult = PostAnalysisResult