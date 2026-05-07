"""
修复博主准确率超过100%的bug

问题原因：
1. LLM验证可能返回超过100的分数
2. 准确率计算没有做上限检查

修复内容：
1. 修正所有超过100的verify_score
2. 重新计算所有博主的准确率
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, case
from src.api.deps import get_db
from src.models.database import Blogger, Prediction


def fix_blogger_accuracy():
    """修复博主准确率"""
    db = next(get_db())
    
    print("=" * 50)
    print("开始修复博主准确率...")
    print("=" * 50)
    
    # 1. 修复超过100的verify_score
    print("\n[步骤1] 检查并修复超过100的verify_score...")
    over_score_predictions = db.query(Prediction).filter(
        Prediction.verify_score > 100
    ).all()
    
    if over_score_predictions:
        print(f"发现 {len(over_score_predictions)} 条预测分数超过100")
        for p in over_score_predictions:
            old_score = p.verify_score
            p.verify_score = min(100, max(0, p.verify_score))
            print(f"  预测ID {p.id}: {old_score} -> {p.verify_score}")
        db.commit()
        print("✅ 分数修复完成")
    else:
        print("✅ 没有发现超过100的分数")
    
    # 2. 重新计算所有博主的准确率
    print("\n[步骤2] 重新计算所有博主的准确率...")
    bloggers = db.query(Blogger).all()
    
    fixed_count = 0
    for blogger in bloggers:
        # 从预测记录重新统计
        stats = db.query(
            func.count(Prediction.id).label('verified'),
            func.sum(case((Prediction.is_correct == True, 1), else_=0)).label('correct'),
            func.sum(Prediction.verify_score).label('total_score')
        ).filter(
            Prediction.blogger_id == blogger.id,
            Prediction.is_deleted == False,
            Prediction.verify_count > 0
        ).first()
        
        verified_count = stats.verified or 0
        correct_predictions = stats.correct or 0
        total_verify_score = float(stats.total_score or 0)
        
        if verified_count > 0:
            accuracy = round(total_verify_score / (verified_count * 100) * 100, 2)
            accuracy = min(100.0, max(0.0, accuracy))
        else:
            accuracy = 0.0
        
        # 计算评级
        if verified_count < 3:
            grade = 'C'
        elif accuracy >= 80 and verified_count >= 20:
            grade = 'S'
        elif accuracy >= 70 and verified_count >= 15:
            grade = 'A'
        elif accuracy >= 60 and verified_count >= 10:
            grade = 'B'
        elif accuracy >= 50 and verified_count >= 5:
            grade = 'C'
        else:
            grade = 'D'
        
        # 检查是否需要修复
        if (blogger.accuracy_rate != accuracy or 
            blogger.total_predictions != verified_count or
            blogger.accuracy_rate > 100):
            
            print(f"\n博主: {blogger.name}")
            print(f"  预测数: {blogger.total_predictions} -> {verified_count}")
            print(f"  正确数: {blogger.correct_predictions} -> {correct_predictions}")
            print(f"  累计分数: {blogger.total_verify_score} -> {total_verify_score}")
            print(f"  准确率: {blogger.accuracy_rate}% -> {accuracy}%")
            print(f"  评级: {blogger.grade} -> {grade}")
            
            blogger.total_predictions = verified_count
            blogger.correct_predictions = correct_predictions
            blogger.total_verify_score = total_verify_score
            blogger.accuracy_rate = accuracy
            blogger.grade = grade
            
            fixed_count += 1
    
    db.commit()
    
    print("\n" + "=" * 50)
    print(f"✅ 修复完成！共修复 {fixed_count} 个博主的数据")
    print("=" * 50)
    
    return fixed_count


if __name__ == "__main__":
    fix_blogger_accuracy()
