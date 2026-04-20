"""
检查预测板块匹配问题
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post
from datetime import date


def check_sector_mismatch():
    db = SessionLocal()
    
    try:
        today = date.today()
        
        # 查询今天的预测
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today
        ).order_by(Prediction.id).all()
        
        print(f'检查今天预测的板块匹配情况')
        print('='*60)
        
        for pred in predictions:
            content = pred.prediction_content or ''
            sector = pred.sector or ''
            
            # 检查预测内容开头是否与板块匹配
            content_start = content[:20] if content else ''
            
            # 如果内容开头包含板块名称，检查是否匹配
            mismatch = False
            mismatch_detail = ''
            
            if '：' in content_start:
                content_sector = content_start.split('：')[0]
                if content_sector and content_sector != sector:
                    mismatch = True
                    mismatch_detail = f'内容提到"{content_sector}"，但板块是"{sector}"'
            
            if mismatch:
                print(f'\n⚠️  预测 {pred.id}: 板块不匹配!')
                print(f'  板块: {sector}')
                print(f'  内容: {content[:80]}...')
                print(f'  问题: {mismatch_detail}')
                print(f'  基金: {pred.fund_name} ({pred.fund_code})')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_sector_mismatch()
