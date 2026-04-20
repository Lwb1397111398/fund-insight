"""
时间解析器测试脚本
测试时间表达式解析功能的准确性
"""
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.time_parser import TimeExpressionParser, parse_time_expression, suggest_prediction_period
from src.utils.prediction_utils import parse_period_to_days, days_to_standard_period, normalize_period


def test_time_parser():
    """测试时间表达式解析器"""
    parser = TimeExpressionParser()
    
    test_cases = [
        ("明天", date(2026, 3, 8), 1, "1天"),
        ("后天", date(2026, 3, 8), 2, "2天"),
        ("大后天", date(2026, 3, 8), 3, "3天"),
        ("下周", date(2026, 3, 8), 7, "1周"),
        ("下周三", date(2026, 3, 8), 9, "9天"),
        ("一周", date(2026, 3, 8), 7, "1周"),
        ("两周", date(2026, 3, 8), 14, "2周"),
        ("一个月", date(2026, 3, 8), 30, "1个月"),
        ("三个月", date(2026, 3, 8), 90, "3个月"),
        ("半年", date(2026, 3, 8), 180, "6个月"),
        ("一年", date(2026, 3, 8), 365, "1年"),
        ("短线", date(2026, 3, 8), 3, "3天"),
        ("短期", date(2026, 3, 8), 3, "3天"),
        ("中线", date(2026, 3, 8), 30, "1个月"),
        ("中期", date(2026, 3, 8), 30, "1个月"),
        ("长线", date(2026, 3, 8), 90, "3个月"),
        ("长期", date(2026, 3, 8), 90, "3个月"),
        ("3天", date(2026, 3, 8), 3, "3天"),
        ("5天", date(2026, 3, 8), 5, "5天"),
        ("两周", date(2026, 3, 8), 14, "2周"),
        ("月底", date(2026, 3, 8), 23, "23天"),
        ("月底", date(2026, 3, 25), 6, "6天"),
        ("下月底", date(2026, 3, 8), 53, "53天"),
        ("季末", date(2026, 3, 8), 23, "23天"),
        ("年底", date(2026, 3, 8), 298, "298天"),
    ]
    
    print("=" * 60)
    print("时间表达式解析测试")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for expression, post_date, expected_days, expected_period in test_cases:
        days, period, reason = parser.parse(expression, post_date)
        
        status = "✓" if days == expected_days else "✗"
        if days == expected_days:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} '{expression}' (发布: {post_date})")
        print(f"   期望: {expected_days}天/{expected_period}")
        print(f"   实际: {days}天/{period}")
        print(f"   说明: {reason}")
        print()
    
    print("=" * 60)
    print(f"测试结果: 通过 {passed}/{len(test_cases)}, 失败 {failed}")
    print("=" * 60)
    
    return passed, failed


def test_period_utils():
    """测试周期工具函数"""
    print("\n" + "=" * 60)
    print("周期工具函数测试")
    print("=" * 60)
    
    test_cases = [
        ("1天", 1, "1天"),
        ("2天", 2, "2天"),
        ("3天", 3, "3天"),
        ("4天", 4, "4天"),
        ("5天", 5, "5天"),
        ("6天", 6, "6天"),
        ("1周", 7, "1周"),
        ("2周", 14, "2周"),
        ("3周", 21, "3周"),
        ("1个月", 30, "1个月"),
        ("明天", 1, "1天"),
        ("后天", 2, "2天"),
        ("下周", 7, "1周"),
        ("一周", 7, "1周"),
        ("一个月", 30, "1个月"),
    ]
    
    passed = 0
    failed = 0
    
    for period_str, expected_days, expected_normalized in test_cases:
        days = parse_period_to_days(period_str)
        normalized = normalize_period(period_str)
        
        status = "✓" if days == expected_days and normalized == expected_normalized else "✗"
        if days == expected_days and normalized == expected_normalized:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} '{period_str}' -> {days}天, 标准化: {normalized}")
        print(f"   期望: {expected_days}天, 标准化: {expected_normalized}")
    
    print("=" * 60)
    print(f"测试结果: 通过 {passed}/{len(test_cases)}, 失败 {failed}")
    print("=" * 60)
    
    return passed, failed


def test_text_suggestion():
    """测试从文本建议周期"""
    print("\n" + "=" * 60)
    print("文本周期建议测试")
    print("=" * 60)
    
    test_cases = [
        ("看好白酒明天的走势", date(2026, 3, 8), 1, "1天"),
        ("新能源后天有机会反弹", date(2026, 3, 8), 2, "2天"),
        ("短线看好医药板块", date(2026, 3, 8), 3, "3天"),
        ("下周看好科技股", date(2026, 3, 8), 7, "1周"),
        ("中线布局消费板块", date(2026, 3, 8), 30, "1个月"),
        ("长期持有黄金ETF", date(2026, 3, 8), 90, "3个月"),
        ("看好白酒板块", date(2026, 3, 8), 30, "1个月"),
        ("月底前看好券商", date(2026, 3, 8), 23, "23天"),
    ]
    
    passed = 0
    failed = 0
    
    for text, post_date, expected_days, expected_period in test_cases:
        days, period, reason = suggest_prediction_period(text, post_date)
        
        status = "✓" if days == expected_days else "✗"
        if days == expected_days:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} 文本: '{text}'")
        print(f"   发布日期: {post_date}")
        print(f"   期望: {expected_days}天/{expected_period}")
        print(f"   实际: {days}天/{period}")
        print(f"   说明: {reason}")
        print()
    
    print("=" * 60)
    print(f"测试结果: 通过 {passed}/{len(test_cases)}, 失败 {failed}")
    print("=" * 60)
    
    return passed, failed


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("时间解析改进功能测试")
    print("=" * 60 + "\n")
    
    total_passed = 0
    total_failed = 0
    
    p, f = test_time_parser()
    total_passed += p
    total_failed += f
    
    p, f = test_period_utils()
    total_passed += p
    total_failed += f
    
    p, f = test_text_suggestion()
    total_passed += p
    total_failed += f
    
    print("\n" + "=" * 60)
    print(f"总测试结果: 通过 {total_passed}, 失败 {total_failed}")
    print("=" * 60)
