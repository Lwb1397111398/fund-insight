"""
检查短期预测的帖子内容
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post, Blogger
from datetime import date


def check_short_period_posts():
    db = SessionLocal()
    
    try:
        # 查询今天的短期预测（1天）
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == date.today(),
            Prediction.prediction_period == '1天'
        ).all()
        
        print(f'今天短期预测（1天）的帖子内容分析')
        print('='*60)
        
        # 按帖子分组
        post_map = {}
        for pred in predictions:
            if pred.post_id not in post_map:
                post_map[pred.post_id] = []
            post_map[pred.post_id].append(pred)
        
        for post_id, preds in post_map.items():
            post = db.query(Post).filter(Post.id == post_id).first()
            blogger = db.query(Blogger).filter(Blogger.id == post.blogger_id).first() if post else None
            
            blogger_name = blogger.name if blogger else '未知'
            post_title = post.title[:50] if post and post.title else f'Post {post_id}'
            post_content = post.content[:500] if post and post.content else '无内容'
            
            print(f'\n帖子: {post_title}...')
            print(f'博主: {blogger_name}')
            print(f'预测数量: {len(preds)}')
            print(f'预测板块: {[p.sector for p in preds]}')
            print(f'\n帖子内容（前500字）:')
            print('-'*40)
            print(post_content)
            print('-'*40)
            
            # 检查是否有中长期关键词
            keywords = ['中线', '长线', '中期', '长期', '波段', '持有', '一个月', '三个月', '半年', '一年']
            found = [kw for kw in keywords if kw in post_content]
            if found:
                print(f'\n⚠️  发现中长期关键词: {found}')
            else:
                print(f'\n未发现中长期关键词')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_short_period_posts()
