"""
检查最近一次分析的文章
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post, Prediction, Blogger
from datetime import datetime, timedelta


def check_latest_analysis():
    db = SessionLocal()
    
    try:
        # 查询最新的帖子（所有平台）
        print('最新的10个帖子:')
        latest_posts = db.query(Post).order_by(Post.created_at.desc()).limit(10).all()
        for post in latest_posts:
            blogger = db.query(Blogger).filter(Blogger.id == post.blogger_id).first()
            platform = blogger.platform if blogger else '未知'
            blogger_name = blogger.name if blogger else '未知'
            title = post.title[:50] if post.title else "无标题"
            
            # 统计预测数量
            pred_count = db.query(Prediction).filter(Prediction.post_id == post.id).count()
            
            print(f'\n  Post ID: {post.id}')
            print(f'  平台: {platform}')
            print(f'  博主: {blogger_name}')
            print(f'  标题: {title}...')
            print(f'  分析状态: {post.analyzed}')
            print(f'  预测数量: {pred_count}')
            print(f'  创建时间: {post.created_at}')
            print(f'  来源URL: {post.source_url[:60] if post.source_url else "无"}...')
        
        # 查询今天创建的所有预测
        print('\n' + '='*60)
        print('今天创建的所有预测（按post_id分组）:')
        
        today = datetime.now().date()
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today
        ).order_by(Prediction.id.desc()).all()
        
        post_predictions = {}
        for pred in predictions:
            if pred.post_id not in post_predictions:
                post_predictions[pred.post_id] = []
            post_predictions[pred.post_id].append(pred)
        
        for post_id, preds in sorted(post_predictions.items(), key=lambda x: -x[0]):
            post = db.query(Post).filter(Post.id == post_id).first()
            if post:
                blogger = db.query(Blogger).filter(Blogger.id == post.blogger_id).first()
                platform = blogger.platform if blogger else '未知'
                print(f'\n  Post ID: {post_id} (平台: {platform})')
                print(f'  预测数量: {len(preds)}')
                for pred in preds[:3]:  # 只显示前3个
                    print(f'    - {pred.sector}: {pred.prediction_type} ({pred.fund_name or "未匹配基金"})')
                if len(preds) > 3:
                    print(f'    ... 还有 {len(preds)-3} 个预测')
            else:
                print(f'\n  Post ID: {post_id} (帖子不存在!)')
                print(f'  预测数量: {len(preds)}')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_latest_analysis()
