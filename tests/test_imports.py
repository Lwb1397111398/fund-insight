"""测试导入是否正常"""
from src.utils.prediction_utils import normalize_period, parse_period_to_days, days_to_standard_period, PERIOD_MAP, ULTRA_SHORT_PERIODS
from src.utils.time_parser import suggest_prediction_period, TimeExpressionParser
from datetime import date

print("All imports OK")
print(f"normalize_period('明天'): {normalize_period('明天')}")
print(f"days_to_standard_period(23): {days_to_standard_period(23)}")
print(f"parse_period_to_days('1周'): {parse_period_to_days('1周')}")

parser = TimeExpressionParser()
days, period, reason = parser.parse("下周", date(2026, 3, 8))
print(f"parse('下周', 2026-03-08): {days}天, {period}")

days, period, reason = suggest_prediction_period("看好白酒下周的走势", date(2026, 3, 8))
print(f"suggest_period: {days}天, {period}, {reason}")

print("\nAll tests passed!")
