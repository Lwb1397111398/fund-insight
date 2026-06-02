"""
时间表达式解析模块
用于解析博主帖子中的相对时间表达式，转换为具体天数

支持的表达式类型：
1. 绝对时间：明天、后天、下周、月底、季末等
2. 相对时间：短期、中期、长期、近期等
3. 事件驱动：两会后、财报季、节后等
4. 数字+单位：3天、两周、一个月等
"""
import re
from datetime import date, timedelta, datetime
from calendar import monthrange
from typing import Tuple, Optional, Dict, List
import logging

logger = logging.getLogger(__name__)


class TimeExpressionParser:
    """时间表达式解析器"""
    
    RELATIVE_TIME_MAP = {
        '明天': 1,
        '次日': 1,
        '下个交易日': 1,
        '后天': 2,
        '大后天': 3,
        '下周': 7,
        '下周一': 7,
        '下周二': 8,
        '下周三': 9,
        '下周四': 10,
        '下周五': 11,
        '一周': 7,
        '两周': 14,
        '半个月': 15,
        '一个月': 30,
        '两个月': 60,
        '一季度': 90,
        '一个季度': 90,
        '半年': 180,
        '一年': 365,
    }
    
    FUZZY_TIME_MAP = {
        '超短线': 1,
        '超短期': 1,
        '极短线': 1,
        '短线': 3,
        '短期': 7,
        '近期': 5,
        '近几天': 3,
        '这几天': 3,
        '接下来几天': 3,
        '中线': 30,
        '中期': 30,
        '中短期': 14,
        '中长期': 60,
        '长线': 90,
        '长期': 90,
        '远期': 180,
    }
    
    HIGH_CONFIDENCE_EXPRESSIONS = {
        '明天': 1, '次日': 1, '下个交易日': 1,
        '后天': 2, '大后天': 3,
        '下周': 7, '下周一': 7, '下周二': 8, '下周三': 9, '下周四': 10, '下周五': 11,
        '一周': 7, '两周': 14, '半个月': 15,
        '一个月': 30, '两个月': 60, '三个月': 90,
        '一季度': 90, '半年': 180, '一年': 365,
        '日内': 1, '当天': 1, '今天': 1,
    }
    
    MEDIUM_CONFIDENCE_EXPRESSIONS = {
        '短线': 3, '短期': 7, '近期': 5,
        '中线': 30, '中期': 30,
        '长线': 90, '长期': 90,
        '波段': 30,
        '做T': 1, '高抛低吸': 1,
    }
    
    LOW_CONFIDENCE_EXPRESSIONS = {
        '超短线': 1, '超短期': 1, '极短线': 1,
        '接下来': 5, '未来': 30,
        '中短期': 14, '中长期': 60,
        '远期': 180,
    }
    
    OPERATION_PERIOD_MAP = {
        '做T': 1, '做t': 1, 'T+0': 1, 't+0': 1,
        '日内': 1, '当天': 1, '今天': 1,
        '高抛低吸': 1,
        '抄底': 3, '低吸': 3,
        '波段': 30,
        '持有': 90, '持有待涨': 90,
    }
    
    FUZZY_TIME_PRIORITY = [
        ('超短线', 1), ('超短期', 1), ('极短线', 1),
        ('短线', 3), ('短期', 7), ('近期', 5),
        ('近几天', 3), ('这几天', 3), ('接下来几天', 3),
        ('中线', 30), ('中期', 30), ('中短期', 14), ('中长期', 60),
        ('长线', 90), ('长期', 90), ('远期', 180),
    ]
    
    NUMBER_UNIT_PATTERN = re.compile(
        r'(\d+|[一二三四五六七八九十]+)\s*'
        r'(天|日|周|星期|个月|月|季度|季|年)'
    )
    
    CHINESE_NUM_MAP = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
        '两': 2,
    }
    
    HOLIDAY_DATES = {
        '元旦': [(1, 1), (1, 3)],
        '春节': [(1, 21), (2, 10)],
        '清明': [(4, 4), (4, 6)],
        '五一': [(5, 1), (5, 5)],
        '端午': [(5, 25), (6, 10)],
        '中秋': [(9, 15), (10, 5)],
        '国庆': [(10, 1), (10, 7)],
    }
    
    def __init__(self):
        self._current_year = date.today().year
    
    def parse(
        self, 
        expression: str, 
        post_date: date = None,
        context: str = ""
    ) -> Tuple[Optional[int], str, str]:
        """
        解析时间表达式
        
        Args:
            expression: 时间表达式（如"下周"、"月底"）
            post_date: 帖子发布日期（用于计算相对时间）
            context: 上下文文本（用于辅助判断）
        
        Returns:
            (天数, 标准化周期, 解析说明)
            - 天数: None 表示无法解析
            - 标准化周期: 如 "1天"、"1周"、"自定义" 等
            - 解析说明: 解释如何解析的
        """
        if not expression:
            return None, "", "空表达式"
        
        expression = expression.strip()
        post_date = post_date or date.today()
        
        result = self._try_exact_match(expression, post_date)
        if result[0] is not None:
            return result
        
        result = self._try_fuzzy_match(expression)
        if result[0] is not None:
            return result
        
        result = self._try_number_unit(expression)
        if result[0] is not None:
            return result
        
        result = self._try_relative_date(expression, post_date)
        if result[0] is not None:
            return result
        
        result = self._try_event_driven(expression, post_date)
        if result[0] is not None:
            return result
        
        return None, "", f"无法解析: {expression}"
    
    def _try_exact_match(
        self, 
        expression: str, 
        post_date: date
    ) -> Tuple[Optional[int], str, str]:
        """尝试精确匹配"""
        if expression in self.RELATIVE_TIME_MAP:
            days = self.RELATIVE_TIME_MAP[expression]
            period = self._days_to_period(days)
            return days, period, f"精确匹配: {expression}"
        return None, "", ""
    
    def _try_fuzzy_match(
        self, 
        expression: str
    ) -> Tuple[Optional[int], str, str]:
        """尝试模糊匹配（按优先级顺序）"""
        for key, days in self.FUZZY_TIME_PRIORITY:
            if key == expression:
                period = self._days_to_period(days)
                return days, period, f"模糊匹配: {expression} -> {key}"
        
        for key, days in self.FUZZY_TIME_PRIORITY:
            if key in expression:
                period = self._days_to_period(days)
                return days, period, f"模糊匹配: {expression} -> {key}"
        
        return None, "", ""
    
    def _try_number_unit(
        self, 
        expression: str
    ) -> Tuple[Optional[int], str, str]:
        """尝试解析数字+单位格式"""
        match = self.NUMBER_UNIT_PATTERN.search(expression)
        if not match:
            return None, "", ""
        
        num_str, unit = match.groups()
        
        try:
            if num_str.isdigit():
                num = int(num_str)
            else:
                num = self._parse_chinese_number(num_str)
        except ValueError:
            return None, "", ""
        
        if num <= 0:
            return None, "", ""
        
        days = self._convert_unit_to_days(num, unit)
        if days:
            period = self._days_to_period(days)
            return days, period, f"数字+单位: {num}{unit}"
        
        return None, "", ""
    
    def _parse_chinese_number(self, num_str: str) -> int:
        """解析中文数字"""
        if num_str in self.CHINESE_NUM_MAP:
            return self.CHINESE_NUM_MAP[num_str]
        
        total = 0
        if '十' in num_str:
            parts = num_str.split('十')
            if parts[0]:
                total += self.CHINESE_NUM_MAP.get(parts[0], 0) * 10
            else:
                total += 10
            if len(parts) > 1 and parts[1]:
                total += self.CHINESE_NUM_MAP.get(parts[1], 0)
        else:
            for char in num_str:
                if char in self.CHINESE_NUM_MAP:
                    total += self.CHINESE_NUM_MAP[char]
        
        return total if total > 0 else 0
    
    def _convert_unit_to_days(self, num: int, unit: str) -> Optional[int]:
        """转换单位到天数"""
        unit_map = {
            '天': 1, '日': 1,
            '周': 7, '星期': 7,
            '个月': 30, '月': 30,
            '季度': 90, '季': 90,
            '年': 365,
        }
        
        multiplier = unit_map.get(unit)
        if multiplier:
            return num * multiplier
        return None
    
    def _try_relative_date(
        self, 
        expression: str, 
        post_date: date
    ) -> Tuple[Optional[int], str, str]:
        """尝试解析相对日期（月底、季末等）"""
        
        if '下月底' in expression or '下月末' in expression:
            if post_date.month == 12:
                target_month = 1
                target_year = post_date.year + 1
            else:
                target_month = post_date.month + 1
                target_year = post_date.year
            
            last_day = monthrange(target_year, target_month)[1]
            target_date = date(target_year, target_month, last_day)
            days = (target_date - post_date).days
            return days, self._days_to_period(days), f"下月底计算: {target_date}"
        
        if '月底' in expression or '月末' in expression:
            last_day = monthrange(post_date.year, post_date.month)[1]
            target_date = date(post_date.year, post_date.month, last_day)
            days = (target_date - post_date).days
            if days < 0:
                if post_date.month == 12:
                    target_date = date(post_date.year + 1, 1, 31)
                else:
                    last_day = monthrange(post_date.year, post_date.month + 1)[1]
                    target_date = date(post_date.year, post_date.month + 1, last_day)
                days = (target_date - post_date).days
            return days, self._days_to_period(days), f"月底计算: {target_date}"
        
        if '季末' in expression or '季度末' in expression:
            current_quarter = (post_date.month - 1) // 3 + 1
            quarter_end_month = current_quarter * 3
            last_day = monthrange(post_date.year, quarter_end_month)[1]
            target_date = date(post_date.year, quarter_end_month, last_day)
            days = (target_date - post_date).days
            if days < 0:
                next_quarter = current_quarter + 1
                if next_quarter > 4:
                    next_quarter = 1
                    target_year = post_date.year + 1
                else:
                    target_year = post_date.year
                quarter_end_month = next_quarter * 3
                last_day = monthrange(target_year, quarter_end_month)[1]
                target_date = date(target_year, quarter_end_month, last_day)
                days = (target_date - post_date).days
            return days, self._days_to_period(days), f"季末计算: {target_date}"
        
        if '年底' in expression or '年末' in expression:
            target_date = date(post_date.year, 12, 31)
            days = (target_date - post_date).days
            if days < 0:
                target_date = date(post_date.year + 1, 12, 31)
                days = (target_date - post_date).days
            return days, self._days_to_period(days), f"年底计算: {target_date}"
        
        return None, "", ""
    
    def _try_event_driven(
        self, 
        expression: str, 
        post_date: date
    ) -> Tuple[Optional[int], str, str]:
        """尝试解析事件驱动的时间表达式"""
        
        if '两会' in expression:
            target_date = self._get_lianghui_date(post_date)
            if target_date:
                days = (target_date - post_date).days
                return days, self._days_to_period(days), f"两会时间: {target_date}"
        
        if '财报' in expression or '业绩' in expression:
            target_date = self._get_next_report_date(post_date)
            if target_date:
                days = (target_date - post_date).days
                return days, self._days_to_period(days), f"财报季: {target_date}"
        
        if '节后' in expression or '假期后' in expression:
            target_date = self._get_post_holiday_date(post_date)
            if target_date:
                days = (target_date - post_date).days
                return days, self._days_to_period(days), f"节后: {target_date}"
        
        return None, "", ""
    
    def _get_lianghui_date(self, post_date: date) -> Optional[date]:
        """获取最近的两会日期（3月初）"""
        lianghui = date(post_date.year, 3, 5)
        if post_date < lianghui:
            return lianghui
        else:
            return date(post_date.year + 1, 3, 5)
    
    def _get_next_report_date(self, post_date: date) -> Optional[date]:
        """获取下一个财报披露截止日期"""
        report_dates = [
            (4, 30),
            (8, 31),
            (10, 31),
        ]
        
        for month, day in report_dates:
            target = date(post_date.year, month, day)
            if target > post_date:
                return target
        
        return date(post_date.year + 1, 4, 30)
    
    def _get_post_holiday_date(self, post_date: date) -> Optional[date]:
        """获取最近节假日后的日期"""
        for holiday_name, date_ranges in self.HOLIDAY_DATES.items():
            for start_month, start_day in date_ranges:
                holiday_end = date(post_date.year, start_month, start_day)
                if holiday_end > post_date:
                    return holiday_end + timedelta(days=1)
        
        return None
    
    def _days_to_period(self, days: int) -> str:
        """将天数转换为标准化周期"""
        if days <= 0:
            return "1天"
        
        period_map = {
            1: "1天",
            2: "2天",
            3: "3天",
            4: "4天",
            5: "5天",
            6: "6天",
            7: "1周",
            14: "2周",
            30: "1个月",
            60: "2个月",
            90: "3个月",
            180: "6个月",
            365: "1年",
        }
        
        if days in period_map:
            return period_map[days]
        
        if days < 7:
            return f"{days}天"
        elif days < 30:
            weeks = days // 7
            return f"{weeks}周"
        elif days < 365:
            months = days // 30
            return f"{months}个月"
        else:
            years = days // 365
            return f"{years}年"
    
    def extract_time_expressions(self, text: str) -> List[Dict]:
        """
        从文本中提取所有时间表达式
        
        Args:
            text: 文本内容
        
        Returns:
            时间表达式列表 [{expression, position, days, period}]
        """
        results = []
        
        all_patterns = {}
        all_patterns.update(self.RELATIVE_TIME_MAP)
        all_patterns.update(self.FUZZY_TIME_MAP)
        
        for expr, days in all_patterns.items():
            pattern = re.compile(re.escape(expr))
            for match in pattern.finditer(text):
                results.append({
                    'expression': expr,
                    'position': match.start(),
                    'days': days,
                    'period': self._days_to_period(days)
                })
        
        for match in self.NUMBER_UNIT_PATTERN.finditer(text):
            num_str, unit = match.groups()
            try:
                if num_str.isdigit():
                    num = int(num_str)
                else:
                    num = self._parse_chinese_number(num_str)
                
                days = self._convert_unit_to_days(num, unit)
                if days:
                    results.append({
                        'expression': match.group(),
                        'position': match.start(),
                        'days': days,
                        'period': self._days_to_period(days)
                    })
            except:
                pass
        
        relative_patterns = ['月底', '月末', '下月底', '下月末', '季末', '季度末', '年底', '年末']
        for pattern in relative_patterns:
            for match in re.finditer(pattern, text):
                results.append({
                    'expression': pattern,
                    'position': match.start(),
                    'days': None,
                    'period': '待计算',
                    'needs_post_date': True
                })
        
        results.sort(key=lambda x: x['position'])
        
        return results
    
    def suggest_period_from_context(
        self, 
        text: str, 
        post_date: date = None
    ) -> Tuple[int, str, str]:
        """
        根据上下文建议最可能的预测周期
        
        策略：
        1. 优先使用高置信度的时间表达式（如"中线"、"长线"）
        2. 其次使用操作术语（如"波段"、"持有"）
        3. 避免使用过于短期的表达（如"明天"）作为预测周期
        4. 如果都是短期表达，选择中等周期（1个月）作为默认
        
        Args:
            text: 文本内容
            post_date: 帖子发布日期
        
        Returns:
            (天数, 标准化周期, 推荐理由)
        """
        # 首先尝试使用带置信度的方法
        days, period, reason, confidence, matched_expr = self.suggest_period_with_confidence(text, post_date)
        
        # 如果是高置信度或中等置信度，直接使用
        if confidence in ['high', 'medium']:
            return days, period, reason
        
        # 低置信度或没有匹配时，提取所有表达式进行智能选择
        expressions = self.extract_time_expressions(text)
        
        if not expressions:
            return 30, "1个月", "未找到时间表达式，使用默认值"
        
        valid_exprs = [e for e in expressions if e.get('days') is not None]
        
        if not valid_exprs:
            if post_date:
                for expr in expressions:
                    if expr.get('needs_post_date'):
                        days, period, _ = self._try_relative_date(
                            expr['expression'], post_date
                        )
                        if days:
                            return days, period, f"从相对日期计算: {expr['expression']}"
            return 30, "1个月", "未找到时间表达式，使用默认值"
        
        # 过滤掉过于短期的表达（1-3天），除非是明确的高频交易语境
        non_trivial_exprs = [e for e in valid_exprs if e['days'] >= 7]  # 至少1周
        
        if non_trivial_exprs:
            # 在非短期表达中选择最短的（最保守的）
            selected = min(non_trivial_exprs, key=lambda x: x['days'])
            return selected['days'], selected['period'], f"过滤短期后选择: {selected['expression']}"
        
        # 如果都是短期表达，检查是否有明确的操作术语
        text_lower = text.lower()
        for expr, days in self.OPERATION_PERIOD_MAP.items():
            if expr in text or expr.lower() in text_lower:
                if days >= 7:  # 操作术语对应的中长期
                    return days, self._days_to_period(days), f"操作术语匹配: {expr}"
        
        # 如果都是短期且没有明确的中长期语境，默认使用1个月
        # 但记录最短的表达式作为参考
        shortest = min(valid_exprs, key=lambda x: x['days'])
        if shortest['days'] <= 3:
            return 30, "1个月", f"短期表达过多({shortest['expression']})，默认使用1个月"
        
        return shortest['days'], shortest['period'], f"最短时间表达式: {shortest['expression']}"
    
    def suggest_period_with_confidence(
        self, 
        text: str, 
        post_date: date = None
    ) -> Tuple[int, str, str, str, str]:
        """
        根据上下文建议预测周期（带置信度）
        
        策略：优先选择中长期表达（≥7天），避免过于短期的表达（1-3天）
        
        Args:
            text: 文本内容
            post_date: 帖子发布日期
        
        Returns:
            (天数, 标准化周期, 推荐理由, 置信度, 匹配的表达式)
            置信度: "high" / "medium" / "low" / "none"
        """
        text_lower = text.lower()
        
        # 第一优先级：检查高置信度的中长期表达（≥7天）
        for expr, days in self.HIGH_CONFIDENCE_EXPRESSIONS.items():
            if expr in text and days >= 7:  # 只匹配中长期
                return days, self._days_to_period(days), f"精确匹配(中长期): {expr}", "high", expr
        
        # 第二优先级：检查操作术语中的中长期（持有、波段等）
        for expr, days in self.OPERATION_PERIOD_MAP.items():
            if days >= 7 and (expr in text or expr.lower() in text_lower):
                return days, self._days_to_period(days), f"操作术语匹配(中长期): {expr}", "high", expr
        
        # 第三优先级：检查中等置信度表达式中的中长期
        for expr, days in self.MEDIUM_CONFIDENCE_EXPRESSIONS.items():
            if expr in text and days >= 7:
                return days, self._days_to_period(days), f"关键词匹配(中长期): {expr}", "medium", expr
        
        # 第四优先级：检查低置信度表达式中的中长期
        for expr, days in self.LOW_CONFIDENCE_EXPRESSIONS.items():
            if expr in text and days >= 7:
                return days, self._days_to_period(days), f"模糊匹配(中长期): {expr}", "low", expr
        
        # 如果没有找到中长期表达，再考虑短期表达
        # 第五优先级：高置信度短期表达
        for expr, days in self.HIGH_CONFIDENCE_EXPRESSIONS.items():
            if expr in text:
                return days, self._days_to_period(days), f"精确匹配(短期): {expr}", "high", expr
        
        # 第六优先级：操作术语短期表达
        for expr, days in self.OPERATION_PERIOD_MAP.items():
            if expr in text or expr.lower() in text_lower:
                return days, self._days_to_period(days), f"操作术语匹配(短期): {expr}", "high", expr
        
        # 第七优先级：中等置信度短期表达
        for expr, days in self.MEDIUM_CONFIDENCE_EXPRESSIONS.items():
            if expr in text:
                return days, self._days_to_period(days), f"关键词匹配(短期): {expr}", "medium", expr
        
        # 第八优先级：低置信度短期表达
        for expr, days in self.LOW_CONFIDENCE_EXPRESSIONS.items():
            if expr in text:
                return days, self._days_to_period(days), f"模糊匹配(短期): {expr}", "low", expr
        
        # 解析数字+单位格式
        match = self.NUMBER_UNIT_PATTERN.search(text)
        if match:
            num_str, unit = match.groups()
            try:
                if num_str.isdigit():
                    num = int(num_str)
                else:
                    num = self._parse_chinese_number(num_str)
                
                if num > 0:
                    days = self._convert_unit_to_days(num, unit)
                    if days:
                        expr = f"{num}{unit}"
                        return days, self._days_to_period(days), f"数字+单位: {expr}", "medium", expr
            except (ValueError, KeyError):
                pass
        
        # 检查相对日期表达式
        if post_date:
            relative_patterns = [
                ('月底', '月底'), ('月末', '月末'),
                ('下月底', '下月底'), ('下月末', '下月末'),
                ('季末', '季末'), ('季度末', '季度末'),
                ('年底', '年底'), ('年末', '年末'),
            ]
            for pattern, expr in relative_patterns:
                if pattern in text:
                    days, period, _ = self._try_relative_date(expr, post_date)
                    if days and days > 0:
                        return days, period, f"相对日期计算: {expr}", "medium", expr
        
        return 30, "1个月", "未找到时间表达式，使用默认值", "none", ""


_time_parser: Optional[TimeExpressionParser] = None


def get_time_parser() -> TimeExpressionParser:
    """获取时间解析器单例"""
    global _time_parser
    if _time_parser is None:
        _time_parser = TimeExpressionParser()
    return _time_parser


def parse_time_expression(
    expression: str, 
    post_date: date = None
) -> Tuple[Optional[int], str, str]:
    """
    便捷函数：解析时间表达式
    
    Args:
        expression: 时间表达式
        post_date: 帖子发布日期
    
    Returns:
        (天数, 标准化周期, 解析说明)
    """
    parser = get_time_parser()
    return parser.parse(expression, post_date)


def suggest_prediction_period(
    text: str, 
    post_date: date = None
) -> Tuple[int, str, str]:
    """
    便捷函数：根据文本建议预测周期
    
    Args:
        text: 文本内容
        post_date: 帖子发布日期
    
    Returns:
        (天数, 标准化周期, 推荐理由)
    """
    parser = get_time_parser()
    return parser.suggest_period_from_context(text, post_date)


def suggest_period_with_confidence(
    text: str, 
    post_date: date = None
) -> Tuple[int, str, str, str, str]:
    """
    便捷函数：根据文本建议预测周期（带置信度）
    
    Args:
        text: 文本内容
        post_date: 帖子发布日期
    
    Returns:
        (天数, 标准化周期, 推荐理由, 置信度, 匹配的表达式)
        置信度: "high" / "medium" / "low" / "none"
    """
    parser = get_time_parser()
    return parser.suggest_period_with_confidence(text, post_date)
