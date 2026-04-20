"""
检查短期预测的类型分布
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction
from datetime import date
from collections import Counter


def check_short_period_types():
    db = SessionLocal()
    
    try:
        today = date.today()
        
        # 查询今天的短期预测
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today,
            Prediction.prediction_period == '1天'
        ).all()
        
        print(f'今天短期预测（1天）的类型分析')
        print('='*60)
        print(f'总数量: {len(predictions)}')
        
        # 按预测类型统计
        type_stats = Counter([p.prediction_type for p in predictions])
        print(f'\n按预测类型分布:')
        for ptype, count in type_stats.most_common():
            print(f'  {ptype}: {count}条')
        
        # 显示每个预测的详情
        print(f'\n预测详情:')
        for pred in predictions[:10]:  # 只显示前10条
            print(f'  ID {pred.id}: {pred.sector} - {pred.prediction_type}')
            if pred.prediction_content:
                content = pred.prediction_content[:80]
                print(f'    内容: {content}...')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_short_period_types()
