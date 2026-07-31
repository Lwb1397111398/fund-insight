"""
观点深度分析模块
使用主LLM（DeepSeek V3.2）对抓取的观点进行深度分析
"""
import json
import logging
import re
from typing import Dict, Optional
from src.analyzer.llm_analyzer import get_analyzer

logger = logging.getLogger(__name__)


class ViewpointAnalyzer:
    """观点深度分析器"""
    
    def __init__(self):
        self.llm_analyzer = get_analyzer()
    
    def analyze_viewpoint(self, title: str, content: str, author: str = "", source: str = "") -> Dict:
        """
        对观点进行深度LLM分析
        
        Args:
            title: 观点标题
            content: 观点内容
            author: 作者
            source: 来源
            
        Returns:
            分析结果字典
        """
        prompt = f"""你是一个专业的基金投资分析助手。请深度分析以下投资观点，给出专业的评价和判断。

【观点信息】
标题：{title}
作者：{author}
来源：{source}

【观点内容】
{content[:1500]}

请深度分析并返回JSON格式（只返回JSON，不要其他内容）：
{{
    "viewpoint_type": "明确预测/深度分析/行情复盘/情绪表达/新闻转述/广告引流/无关内容",
    "market_direction": "bullish/bearish/neutral",
    "confidence": 0-100,
    "sentiment_score": 0.0-1.0,
    "sectors_bullish": ["板块1", "板块2"],
    "sectors_bearish": ["板块1"],
    "analysis": "深度分析评价（100字以内）",
    "key_points": ["要点1", "要点2", "要点3"],
    "credibility": 0-100,
    "reasoning": "判断理由（80字以内）",
    "summary": "一句话摘要（格式：[时间周期][方向][板块]，[核心逻辑]，[操作建议]，不超过80字）",
    "action_suggestion": "建议操作：买入/卖出/持有/观望",
    "risk_level": "high/medium/low",
    "time_horizon": "short/medium/long"
}}

分析标准：
0. viewpoint_type: 内容分类（决定该观点是否纳入市场观点汇总，务必严格判断）
    - 明确预测：对大盘/板块/基金有明确涨跌判断或目标
    - 深度分析：有分析逻辑、数据或论据支撑
    - 行情复盘：客观回顾当日行情走势（属于理性市场分析，保留）
    - 情绪表达：只有情绪发泄、抱怨、喊口号，没有分析依据
    - 新闻转述：单纯转发新闻/公告/数据发布，没有个人观点
    - 广告引流：推广、导流、荐股收费等营销内容
    - 无关内容：与投资市场无关
1. market_direction: 判断整体市场情绪（看涨/看跌/中性）
2. confidence: 对判断的信心程度（0-100）
3. sentiment_score: 情绪分数（0.0-1.0，0.5为中性）
4. sectors_bullish: 看好的板块列表（最多3个）
5. sectors_bearish: 看空的板块列表（最多2个）
6. analysis: 对观点的深度分析评价
7. key_points: 观点的核心要点（3-5个）
8. credibility: 观点可信度（0-100，基于论据充分性）
9. reasoning: 判断理由简述
10. summary: 一句话摘要，格式为"[时间周期][方向][板块]，[核心逻辑]，[操作建议]"
    - 时间周期：短期/中期/长期
    - 方向：看涨/看跌/震荡
    - 板块：重点关注的板块（最多2个）
    - 核心逻辑：支撑观点的关键理由（一句话）
    - 操作建议：建议的操作
    - 示例："短期看涨新能源，受政策利好驱动，建议逢低布局。"
    - 示例："中期看跌半导体，估值过高叠加需求疲软，建议回避。"
    - 示例："短期震荡，等待方向明确，建议观望。"
11. action_suggestion: 建议的操作行动
12. risk_level: 风险等级（高/中/低）
13. time_horizon: 投资时间 horizon（短期/中期/长期）

时间周期定义：
- short: 短期（1周内），适合短线交易
- medium: 中期（1-4周），适合波段操作
- long: 长期（1个月以上），适合中长期持有

有效期建议：
- 短期观点：1周
- 中期观点：1个月
- 长期观点：3个月

注意：
- 如果观点论据充分、逻辑清晰，credibility给70-90
- 如果观点模糊、缺乏依据，credibility给30-50
- summary必须简洁明了，不超过80字
- 必须返回合法的JSON格式
"""
        
        try:
            result_text = self.llm_analyzer._call_llm(
                prompt, 
                task_type='core', 
                max_tokens=1500, 
                temperature=0.3
            )
            
            json_match = re.search(r'\{[\s\S]+\}', result_text)
            if json_match:
                result = json.loads(json_match.group())
                return self._validate_result(result)
            else:
                return self._empty_result()
                
        except Exception as e:
            print(f"[ViewpointAnalyzer] 分析失败: {e}")
            return self._empty_result()
    
    def _validate_result(self, result: Dict) -> Dict:
        """验证并补充结果字段"""
        default_result = self._empty_result()
        
        for key in default_result.keys():
            if key not in result:
                result[key] = default_result[key]
        
        result['confidence'] = max(0, min(100, int(result.get('confidence', 50))))
        result['credibility'] = max(0, min(100, int(result.get('credibility', 50))))
        result['sentiment_score'] = max(0.0, min(1.0, float(result.get('sentiment_score', 0.5))))
        
        # 规范化分类：只接受枚举值，LLM 跑偏时按"深度分析"保留，宁松勿误杀
        allowed_types = {
            "明确预测", "深度分析", "行情复盘",
            "情绪表达", "新闻转述", "广告引流", "无关内容",
        }
        vtype = str(result.get('viewpoint_type') or "").strip()
        result['viewpoint_type'] = vtype if vtype in allowed_types else "深度分析"

        if not isinstance(result.get('sectors_bullish'), list):
            result['sectors_bullish'] = []
        if not isinstance(result.get('sectors_bearish'), list):
            result['sectors_bearish'] = []
        if not isinstance(result.get('key_points'), list):
            result['key_points'] = []
        
        result['sectors_bullish'] = result['sectors_bullish'][:3]
        result['sectors_bearish'] = result['sectors_bearish'][:2]
        result['key_points'] = result['key_points'][:5]
        
        if result.get('summary') and len(result['summary']) > 80:
            result['summary'] = result['summary'][:80]
        
        return result
    
    def _empty_result(self) -> Dict:
        """返回空结果"""
        return {
            # 分析失败时按"深度分析"保留观点：LLM 抖动不应导致观点被误删
            "viewpoint_type": "深度分析",
            "market_direction": "neutral",
            "confidence": 50,
            "sentiment_score": 0.5,
            "sectors_bullish": [],
            "sectors_bearish": [],
            "analysis": "分析失败",
            "key_points": [],
            "credibility": 50,
            "reasoning": "",
            "summary": "观点分析失败，无法生成摘要",
            "action_suggestion": "观望",
            "risk_level": "medium",
            "time_horizon": "medium"
        }


_viewpoint_analyzer = None


def get_viewpoint_analyzer() -> ViewpointAnalyzer:
    """获取观点分析器实例"""
    global _viewpoint_analyzer
    if _viewpoint_analyzer is None:
        _viewpoint_analyzer = ViewpointAnalyzer()
    return _viewpoint_analyzer


def quick_judge_viewpoint(title: str, content: str) -> Optional[Dict]:
    """辅助AI（轻量模型）快速裁决：这篇拿不准的内容是不是有效市场观点？

    只用于关键词规则无法决定的边界内容（只命中"分析/观点/风险"等泛化词、
    没有任何方向/板块/操作类核心词）。注重效率：极短 prompt、最低输出量、
    轻量模型。返回 {"keep": bool, "reason": str}；任何失败返回 None，
    调用方按"放行"处理——深度分析阶段还有第二道剔除防线。
    """
    try:
        analyzer = get_analyzer()
        prompt = f"""快速判断以下内容是不是“有效市场观点”。
是：对大盘/板块/基金的涨跌预测、操作建议或理性市场分析。
否：情绪发泄、单纯新闻或公告转述、广告导流、与投资无关。

标题：{(title or "")[:80]}
内容：{(content or "")[:400]}

只返回JSON：{{"keep": true或false, "reason": "20字以内理由"}}"""
        result_text = analyzer._call_llm(
            prompt, task_type='simple', max_tokens=100, temperature=0
        )
        json_match = re.search(r'\{[\s\S]+\}', result_text)
        if not json_match:
            return None
        data = json.loads(json_match.group())
        return {
            "keep": bool(data.get("keep")),
            "reason": str(data.get("reason") or "")[:60],
        }
    except Exception as exc:
        logger.warning(f"[ViewpointAnalyzer] 辅助AI裁决失败: {exc}")
        return None
