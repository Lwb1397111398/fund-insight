"""
检查微信文章相关的帖子和预测
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post, Prediction, Blogger
from datetime import datetime, timedelta


def check_wechat_posts():
    db = SessionLocal()
    
    try:
        # 查询微信平台的博主
        wechat_bloggers = db.query(Blogger).filter(
            Blogger.platform == 'wechat'
        ).all()
        
        print(f'微信平台博主数量: {len(wechat_bloggers)}')
        for blogger in wechat_bloggers:
            print(f'  ID: {blogger.id}, 名称: {blogger.name}')
            
            # 查询该博主的帖子
            posts = db.query(Post).filter(
                Post.blogger_id == blogger.id
            ).order_by(Post.created_at.desc()).all()
            
            print(f'    帖子数量: {len(posts)}')
            for post in posts:
                title = post.title[:40] if post.title else "无标题"
                print(f'      Post ID: {post.id}, 标题: {title}...')
                print(f'      分析状态: {post.analyzed}, 创建时间: {post.created_at}')
                
                # 查询该帖子的预测
                predictions = db.query(Prediction).filter(
                    Prediction.post_id == post.id
                ).all()
                
                print(f'      预测数量: {len(predictions)}')
                for pred in predictions:
                    fund_status = '✓' if pred.fund_code else '✗'
                    fund_name = pred.fund_name or "未匹配"
                    print(f'        {fund_status} 板块: {pred.sector}, 基金: {fund_name}')
                print()
        
        # 查询最新的帖子（不管平台）
        print('\n' + '='*60)
        print('最新的5个帖子（所有平台）:')
        latest_posts = db.query(Post).order_by(Post.created_at.desc()).limit(5).all()
        for post in latest_posts:
            blogger = db.query(Blogger).filter(Blogger.id == post.blogger_id).first()
            title = post.title[:40] if post.title else "无标题"
            platform = blogger.platform if blogger else '未知'
            print(f'  ID: {post.id}, 平台: {platform}, 标题: {title}...')
            print(f'     分析状态: {post.analyzed}, 创建时间: {post.created_at}')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_wechat_posts()
