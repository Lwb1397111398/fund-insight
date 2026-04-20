"""
检查预测周期分布
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post, Blogger
from datetime import datetime, date
from collections import Counter


def check_prediction_periods():
    db = SessionLocal()
    
    try:
        today = date.today()
        
        # 查询今天的所有预测
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today
        ).all()
        
        print(f'今天 ({today}) 的预测统计')
        print('='*60)
        print(f'总预测数量: {len(predictions)}')
        
        if not predictions:
            print('今天没有预测')
            return
        
        # 按预测周期统计
        period_stats = Counter([p.prediction_period for p in predictions])
        
        print('\n按预测周期分布:')
        for period, count in period_stats.most_common():
            percentage = count / len(predictions) * 100
            print(f'  {period}: {count}条 ({percentage:.1f}%)')
        
        # 检查是否有异常的1天预测
        one_day_preds = [p for p in predictions if p.prediction_period == '1天']
        if len(one_day_preds) > len(predictions) * 0.5:
            print(f'\n⚠️  警告: 超过50%的预测是"1天"周期，这可能不正常!')
        
        # 按帖子分组查看
        print('\n按帖子查看预测周期分布:')
        post_predictions = {}
        for pred in predictions:
            if pred.post_id not in post_predictions:
                post_predictions[pred.post_id] = []
            post_predictions[pred.post_id].append(pred)
        
        for post_id, preds in sorted(post_predictions.items(), key=lambda x: -len(x[1])):
            post = db.query(Post).filter(Post.id == post_id).first()
            blogger = db.query(Blogger).filter(Blogger.id == post.blogger_id).first() if post else None
            
            blogger_name = blogger.name if blogger else '未知'
            post_title = post.title[:30] if post and post.title else f'Post {post_id}'
            
            periods = [p.prediction_period for p in preds]
            period_counts = Counter(periods)
            
            print(f'\n  帖子: {post_title}...')
            print(f'  博主: {blogger_name}')
            print(f'  预测数量: {len(preds)}')
            print(f'  周期分布: {dict(period_counts)}')
            
            # 如果所有预测都是1天，标记为异常
            if all(p == '1天' for p in periods):
                print(f'  ⚠️  该帖子所有预测都是"1天"周期!')
        
        # 检查预测内容中的时间关键词
        print('\n\n检查预测内容中的时间关键词:')
        keywords = ['明天', '今天', '日内', '短线', '中线', '长线', '1天', '1周', '1个月']
        
        for pred in predictions[:10]:  # 只检查前10条
            content = pred.prediction_content or ''
            found_keywords = [kw for kw in keywords if kw in content]
            if found_keywords:
                print(f'  预测 {pred.id}: {found_keywords}')
                print(f'    内容: {content[:50]}...')
                print(f'    周期: {pred.prediction_period}')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_prediction_periods()
