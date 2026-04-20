"""
检查原始LLM分析结果
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post
from datetime import date
import json


def check_llm_analysis_result():
    db = SessionLocal()
    
    try:
        today = date.today()
        
        # 查询今天的帖子
        posts = db.query(Post).filter(
            Post.post_date == today,
            Post.analyzed == True
        ).all()
        
        print(f'检查今天帖子的LLM分析结果')
        print('='*60)
        
        for post in posts[:3]:  # 只检查前3个
            print(f'\n帖子: {post.title[:50] if post.title else "无标题"}...')
            print(f'分析状态: {post.analyzed}')
            
            if post.analysis_result:
                try:
                    result = json.loads(post.analysis_result) if isinstance(post.analysis_result, str) else post.analysis_result
                    
                    predictions = result.get('predictions', [])
                    print(f'\n预测数量: {len(predictions)}')
                    
                    for i, pred in enumerate(predictions[:5]):  # 只显示前5个
                        sector = pred.get('sector', '未知')
                        content = pred.get('prediction_content', '')[:60]
                        fund_code = pred.get('fund_code', '')
                        fund_name = pred.get('fund_name', '')
                        
                        print(f'\n  预测 {i+1}:')
                        print(f'    板块: {sector}')
                        print(f'    基金: {fund_name} ({fund_code})')
                        print(f'    内容: {content}...')
                        
                except Exception as e:
                    print(f'解析分析结果失败: {e}')
            else:
                print('无分析结果')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_llm_analysis_result()
