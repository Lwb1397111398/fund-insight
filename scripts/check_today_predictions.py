"""
检查今天预测的最终状态
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post
from datetime import date
from collections import Counter


def check_today_predictions():
    db = SessionLocal()
    
    try:
        today = date.today()
        
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today
        ).order_by(Prediction.id).all()
        
        print(f'今天预测的最终状态检查')
        print('='*60)
        print(f'总预测数量: {len(predictions)}')
        
        # 按板块统计
        sector_stats = Counter([p.sector for p in predictions])
        print(f'\n按板块分布:')
        for sector, count in sector_stats.most_common():
            print(f'  {sector}: {count}条')
        
        # 按周期统计
        period_stats = Counter([p.prediction_period for p in predictions])
        print(f'\n按周期分布:')
        for period, count in period_stats.most_common():
            print(f'  {period}: {count}条')
        
        # 检查基金匹配情况
        no_fund = [p for p in predictions if not p.fund_code]
        if no_fund:
            print(f'\n⚠️  有 {len(no_fund)} 条预测没有匹配基金:')
            for p in no_fund[:5]:
                print(f'  ID {p.id}: {p.sector}')
        else:
            print(f'\n✓ 所有预测都已匹配基金')
        
        # 检查板块和内容是否明显不匹配
        print(f'\n检查板块和内容的匹配情况:')
        mismatch_count = 0
        
        for pred in predictions[:10]:  # 只检查前10条
            content = pred.prediction_content or ''
            sector = pred.sector or ''
            
            # 检查内容开头是否包含板块关键词
            if '：' in content[:30]:
                prefix = content.split('：')[0]
                # 简单检查：如果内容开头的板块和sector完全无关
                if sector and prefix and sector not in prefix and prefix not in sector:
                    # 进一步检查是否有关联
                    related = False
                    if sector == '半导体' and any(kw in prefix for kw in ['芯片', '半导体', '集成电路']):
                        related = True
                    elif sector == '港股' and any(kw in prefix for kw in ['港科技', '恒生', '港股']):
                        related = True
                    elif sector == '医药' and any(kw in prefix for kw in ['创新药', '医药', '医疗']):
                        related = True
                    elif sector == '新能源' and any(kw in prefix for kw in ['光伏', '储能', '电池']):
                        related = True
                    
                    if not related:
                        print(f'  预测 {pred.id}: 板块={sector}, 内容开头={prefix}')
                        mismatch_count += 1
        
        if mismatch_count == 0:
            print('  ✓ 前10条预测的板块和内容匹配正常')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_today_predictions()
