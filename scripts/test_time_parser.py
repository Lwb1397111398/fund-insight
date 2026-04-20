"""
测试时间解析器修复
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.utils.time_parser import suggest_prediction_period

# 测试用例
test_cases = [
    # 原来会导致问题的用例（包含"明天"和"做T"）
    ('博主说明天会涨，建议做T', '短期表达'),
    ('明天有行情，可以做T日内短线', '短期表达'),
    
    # 应该正确识别的用例
    ('博主认为中线有机会，建议持有', '中线表达'),
    ('长线看好，准备持有半年', '长线表达'),
    ('波段操作，预计一个月后收益', '波段表达'),
    
    # 混合用例（同时包含短期和中长期）
    ('明天可能会涨，但中线看一个月后的机会', '混合表达'),
    ('短线做T，中线持有', '混合表达'),
    
    # 其他常见表达
    ('下周一看涨', '下周表达'),
    ('月底有行情', '月底表达'),
    ('一季度看好', '季度表达'),
]

print('测试时间解析器修复')
print('='*60)

for text, category in test_cases:
    days, period, reason = suggest_prediction_period(text)
    print(f'\n【{category}】')
    print(f'  文本: {text}')
    print(f'  结果: {period} ({days}天)')
    print(f'  理由: {reason}')

print('\n' + '='*60)
print('修复目标：避免将"明天"、"做T"等短期表达作为预测周期')
print('期望结果：优先使用"中线"、"长线"、"波段"等中长期表达')
