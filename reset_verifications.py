#!/usr/bin/env python
"""
清除所有预测的旧验证记录，重置博主准确率
按照新的验证规则重新开始
"""
import sys
sys.path.insert(0, 'E:\\CountBot\\countbot\\workspace\\fund-insight')

from src.models.database import SessionLocal, Prediction, Blogger
from datetime import date

print("=" * 80)
print("清除旧验证记录，重置系统")
print("=" * 80)

db = SessionLocal()

try:
    # 1. 清除所有预测的验证记录
    print("\n1. 清除预测验证记录...")
    predictions = db.query(Prediction).all()
    reset_count = 0
    
    for pred in predictions:
        # 清除验证相关字段
        pred.verify_history = []
        pred.verify_count = 0
        pred.is_correct = None
        pred.actual_change = None
        pred.current_nav = None
        pred.current_nav_date = None
        pred.verified_at = None
        pred.last_verify_date = None
        pred.is_expired = False
        pred.status = 'pending'
        pred.end_nav = None
        pred.end_nav_date = None
        reset_count += 1
    
    db.commit()
    print(f"   已重置 {reset_count} 个预测")
    
    # 2. 重置博主准确率
    print("\n2. 重置博主准确率...")
    bloggers = db.query(Blogger).all()
    blogger_reset_count = 0
    
    for blogger in bloggers:
        blogger.total_predictions = 0
        blogger.correct_predictions = 0
        blogger.accuracy = 0.0
        blogger_reset_count += 1
    
    db.commit()
    print(f"   已重置 {blogger_reset_count} 个博主")
    
    # 3. 统计信息
    print("\n3. 当前状态统计:")
    pending_count = db.query(Prediction).filter(Prediction.status == 'pending').count()
    verified_count = db.query(Prediction).filter(Prediction.verify_count > 0).count()
    expired_count = db.query(Prediction).filter(Prediction.is_expired == True).count()
    
    print(f"   待验证预测: {pending_count}")
    print(f"   已验证预测: {verified_count}")
    print(f"   已过期预测: {expired_count}")
    
    print("\n" + "=" * 80)
    print("系统已重置完成！")
    print("现在可以按照新的验证规则开始验证了。")
    print("=" * 80)
    
except Exception as e:
    print(f"\n错误: {e}")
    import traceback
    traceback.print_exc()
    db.rollback()
finally:
    db.close()
