"""
检查孤儿预测（没有关联帖子的预测）
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post, Prediction, Blogger
from datetime import datetime


def check_orphan_predictions():
    db = SessionLocal()
    
    try:
        # 查询所有预测
        all_predictions = db.query(Prediction).order_by(Prediction.id.desc()).limit(50).all()
        
        print('检查最近的50条预测:')
        print('='*60)
        
        orphan_preds = []
        
        for pred in all_predictions:
            post = db.query(Post).filter(Post.id == pred.post_id).first()
            
            if not post:
                orphan_preds.append(pred)
                print(f'\n⚠️  孤儿预测 ID: {pred.id}')
                print(f'   Post ID: {pred.post_id} (不存在)')
                print(f'   板块: {pred.sector}')
                print(f'   基金: {pred.fund_name or "未匹配"} ({pred.fund_code or "无代码"})')
                print(f'   预测类型: {pred.prediction_type}')
                print(f'   预测日期: {pred.prediction_date}')
                print(f'   博主ID: {pred.blogger_id}')
                
                # 检查博主是否存在
                blogger = db.query(Blogger).filter(Blogger.id == pred.blogger_id).first()
                if blogger:
                    print(f'   博主名称: {blogger.name} (平台: {blogger.platform})')
                else:
                    print(f'   博主: 不存在!')
        
        print(f'\n{"="*60}')
        print(f'总结: 找到 {len(orphan_preds)} 条孤儿预测')
        
        if orphan_preds:
            print('\n这些预测关联的帖子ID:')
            post_ids = set([p.post_id for p in orphan_preds])
            for pid in sorted(post_ids):
                count = len([p for p in orphan_preds if p.post_id == pid])
                print(f'  Post ID {pid}: {count} 条预测')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_orphan_predictions()
