"""
检查最近的帖子和预测
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post, Prediction
from datetime import datetime, timedelta


def check_recent_posts():
    db = SessionLocal()
    
    try:
        # 查询最近1小时的帖子
        recent_posts = db.query(Post).filter(
            Post.created_at >= datetime.now() - timedelta(hours=2)
        ).order_by(Post.created_at.desc()).all()
        
        print(f'最近2小时帖子数量: {len(recent_posts)}')
        for post in recent_posts:
            title = post.title[:30] if post.title else "无标题"
            print(f'  ID: {post.id}, 标题: {title}..., 博主ID: {post.blogger_id}, 分析状态: {post.analyzed}')
        
        # 查询今天的预测
        today = datetime.now().date()
        recent_predictions = db.query(Prediction).filter(
            Prediction.prediction_date == today
        ).order_by(Prediction.id.desc()).all()
        
        print(f'\n今天预测数量: {len(recent_predictions)}')
        
        # 按板块统计
        sector_stats = {}
        for pred in recent_predictions:
            sector = pred.sector or "未知"
            if sector not in sector_stats:
                sector_stats[sector] = 0
            sector_stats[sector] += 1
        
        print('\n按板块统计:')
        for sector, count in sorted(sector_stats.items(), key=lambda x: -x[1]):
            print(f'  {sector}: {count}条')
        
        # 显示最新的5条预测详情
        print('\n最新5条预测:')
        for pred in recent_predictions[:5]:
            fund_info = f'{pred.fund_name}({pred.fund_code})' if pred.fund_code else '未匹配基金'
            print(f'  ID: {pred.id}, 板块: {pred.sector}, 基金: {fund_info}, 类型: {pred.prediction_type}')
        
        # 检查是否有未匹配的预测
        unmatched = [p for p in recent_predictions if not p.fund_code]
        if unmatched:
            print(f'\n⚠️  有 {len(unmatched)} 条预测未匹配基金:')
            for pred in unmatched:
                print(f'  ID: {pred.id}, 板块: {pred.sector}')
        else:
            print('\n✓ 所有预测都已匹配基金')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_recent_posts()
