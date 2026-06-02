"""
预测工具模块
包含预测周期相关的常量和工具函数

增强功能：
1. 支持更多周期粒度（4天、5天、6天等）
2. 支持自定义天数
3. 集成时间表达式解析器
"""
from datetime import date
from typing import Tuple, Optional, Dict, List


PERIOD_MAP = {
    '1天': 1,
    '2天': 2,
    '3天': 3,
    '4天': 4,
    '5天': 5,
    '6天': 6,
    '1周': 7,
    '2周': 14,
    '3周': 21,
    '1个月': 30,
    '2个月': 60,
    '3个月': 90,
    '6个月': 180,
    '1年': 365
}

PERIOD_ALIASES = {
    '一周': '1周',
    '两周': '2周',
    '三周': '3周',
    '一个月': '1个月',
    '两个月': '2个月',
    '三个月': '3个月',
    '半年': '6个月',
    '一年': '1年',
    '明天': '1天',
    '后天': '2天',
    '大后天': '3天',
    '下周': '1周',
    '下个月': '1个月',
}

ULTRA_SHORT_PERIODS = ['1天', '2天', '3天', '4天', '5天', '6天']

SHORT_PERIODS = ['1周', '2周', '3周']

MID_PERIODS = ['1个月', '2个月', '3个月']

LONG_PERIODS = ['6个月', '1年']

ALL_STANDARD_PERIODS = list(PERIOD_MAP.keys())


def parse_period_to_days(period_str: str) -> int:
    """
    解析预测周期字符串为天数
    
    Args:
        period_str: 预测周期字符串
        
    Returns:
        天数
    """
    if not period_str:
        return 30
    
    if period_str in PERIOD_MAP:
        return PERIOD_MAP[period_str]
    
    if period_str in PERIOD_ALIASES:
        return PERIOD_MAP[PERIOD_ALIASES[period_str]]
    
    import re
    numbers = re.findall(r'\d+', str(period_str))
    if numbers:
        num = int(numbers[0])
        if '周' in period_str or '星期' in period_str:
            return num * 7
        elif '月' in period_str:
            return num * 30
        elif '年' in period_str:
            return num * 365
        elif '天' in period_str or '日' in period_str:
            return num
        return num
    
    return 30


def days_to_standard_period(days: int) -> str:
    """
    将天数转换为标准周期字符串
    
    Args:
        days: 天数
        
    Returns:
        标准周期字符串
    """
    if days <= 0:
        return "1天"
    
    if days in PERIOD_MAP.values():
        for period, d in PERIOD_MAP.items():
            if d == days:
                return period
    
    if days < 7:
        return f"{days}天"
    elif days < 30:
        weeks = round(days / 7)
        if weeks == 1:
            return "1周"
        elif weeks == 2:
            return "2周"
        elif weeks == 3:
            return "3周"
        return f"{weeks}周"
    elif days < 365:
        months = round(days / 30)
        if months == 1:
            return "1个月"
        elif months == 2:
            return "2个月"
        elif months == 3:
            return "3个月"
        elif months == 6:
            return "6个月"
        return f"{months}个月"
    else:
        years = round(days / 365)
        if years == 1:
            return "1年"
        return f"{years}年"


def get_period_threshold(period_days: int) -> float:
    """
    根据预测周期获取震荡阈值
    
    Args:
        period_days: 预测周期天数
        
    Returns:
        震荡阈值（百分比）
    """
    if period_days <= 1:
        return 0.5
    elif period_days <= 3:
        return 0.8
    elif period_days <= 7:
        return 1.0
    elif period_days <= 14:
        return 1.2
    elif period_days <= 30:
        return 1.5
    elif period_days <= 90:
        return 2.0
    else:
        return 3.0


def get_period_category(period_days: int) -> str:
    """
    获取周期类别
    
    Args:
        period_days: 预测周期天数
        
    Returns:
        类别：ultra_short / short / mid / long
    """
    if period_days <= 3:
        return 'ultra_short'
    elif period_days <= 14:
        return 'short'
    elif period_days <= 90:
        return 'mid'
    else:
        return 'long'


def is_valid_period(period_str: str) -> bool:
    """
    检查是否为有效的周期字符串
    
    Args:
        period_str: 周期字符串
        
    Returns:
        是否有效
    """
    if not period_str:
        return False
    
    if period_str in PERIOD_MAP:
        return True
    
    if period_str in PERIOD_ALIASES:
        return True
    
    import re
    match = re.match(r'^(\d+)(天|周|个月|年)$', period_str)
    if match:
        return True
    
    return False


def normalize_period(period_str: str) -> str:
    """
    标准化周期字符串
    
    Args:
        period_str: 原始周期字符串
        
    Returns:
        标准化后的周期字符串
    """
    if not period_str:
        return "1个月"
    
    if period_str in PERIOD_MAP:
        return period_str
    
    if period_str in PERIOD_ALIASES:
        return PERIOD_ALIASES[period_str]
    
    days = parse_period_to_days(period_str)
    return days_to_standard_period(days)


def suggest_period_from_text(
    text: str, 
    post_date: date = None
) -> Tuple[int, str, str]:
    """
    从文本中建议预测周期（集成时间解析器）
    
    Args:
        text: 文本内容
        post_date: 帖子发布日期
        
    Returns:
        (天数, 标准化周期, 推荐理由)
    """
    try:
        from src.utils.time_parser import suggest_prediction_period
        return suggest_prediction_period(text, post_date)
    except ImportError:
        pass
    
    import re
    
    ultra_short_patterns = ['明天', '后天', '大后天', '次日', '下个交易日']
    for pattern in ultra_short_patterns:
        if pattern in text:
            days_map = {'明天': 1, '后天': 2, '大后天': 3, '次日': 1, '下个交易日': 1}
            days = days_map.get(pattern, 1)
            return days, days_to_standard_period(days), f"匹配到: {pattern}"
    
    short_patterns = ['短线', '近期', '这几天', '接下来几天']
    for pattern in short_patterns:
        if pattern in text:
            return 3, "3天", f"匹配到: {pattern}"

    # '短期' 映射为 7 天（1周），更符合 PERIOD_MAP 中短期的定义（14天以内）
    if '短期' in text:
        return 7, "1周", "匹配到: 短期"
    
    week_patterns = ['下周', '一周', '这周']
    for pattern in week_patterns:
        if pattern in text:
            return 7, "1周", f"匹配到: {pattern}"
    
    mid_patterns = ['中线', '中期', '一个月', '下个月']
    for pattern in mid_patterns:
        if pattern in text:
            return 30, "1个月", f"匹配到: {pattern}"
    
    long_patterns = ['长线', '长期', '季度', '半年']
    for pattern in long_patterns:
        if pattern in text:
            return 90, "3个月", f"匹配到: {pattern}"
    
    number_pattern = re.search(r'(\d+)\s*(天|周|个月|年)', text)
    if number_pattern:
        num = int(number_pattern.group(1))
        unit = number_pattern.group(2)
        unit_map = {'天': 1, '周': 7, '个月': 30, '年': 365}
        days = num * unit_map.get(unit, 1)
        return days, days_to_standard_period(days), f"数字+单位匹配: {number_pattern.group()}"
    
    return 30, "1个月", "未找到时间表达式，使用默认值"


def get_period_display_info(period_str: str) -> Dict:
    """
    获取周期的显示信息
    
    Args:
        period_str: 周期字符串
        
    Returns:
        显示信息字典
    """
    days = parse_period_to_days(period_str)
    category = get_period_category(days)
    threshold = get_period_threshold(days)
    
    category_names = {
        'ultra_short': '超短期',
        'short': '短期',
        'mid': '中期',
        'long': '长期'
    }
    
    return {
        'period': period_str,
        'days': days,
        'category': category,
        'category_name': category_names.get(category, '未知'),
        'threshold': threshold,
        'is_ultra_short': category == 'ultra_short',
        'display_text': f"{period_str}（{days}天）"
    }


def calculate_target_date(prediction_date: date, period_str: str) -> date:
    """
    计算目标验证日期
    
    Args:
        prediction_date: 预测发布日期
        period_str: 预测周期
        
    Returns:
        目标日期
    """
    from datetime import timedelta
    days = parse_period_to_days(period_str)
    return prediction_date + timedelta(days=days)


def get_verify_schedule(period_days: int) -> List[int]:
    """
    获取验证时间表（建议在哪些天进行验证）
    
    Args:
        period_days: 预测周期天数
        
    Returns:
        验证天数列表（相对于预测日期）
    """
    if period_days <= 3:
        return [1, 2, 3]
    elif period_days <= 7:
        return [1, 3, 5, 7]
    elif period_days <= 14:
        return [3, 7, 10, 14]
    elif period_days <= 30:
        return [5, 10, 15, 20, 25, 30]
    elif period_days <= 90:
        return [7, 15, 30, 45, 60, 75, 90]
    else:
        interval = period_days // 6
        return [interval * i for i in range(1, 7)]
