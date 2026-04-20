"""
重新验证所有预测并更新博主准确率
使用新的验证规则（过程验证 + 分数机制 + 加权准确率）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from src.models.database import SessionLocal, Prediction, Blogger
from src.services.prediction_verify_service import PredictionVerifyService


def reset_blogger_accuracy():
    """重置所有博主的准确率为0"""
    print("=" * 50)
    print("重置博主准确率...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        bloggers = db.query(Blogger).all()
        count = 0
        for blogger in bloggers:
            blogger.accuracy_rate = 0
            blogger.total_predictions = 0
            blogger.correct_predictions = 0
            blogger.ultra_short_accuracy = 0
            blogger.ultra_short_total = 0
            blogger.ultra_short_correct = 0
            blogger.grade = 'C'
            count += 1
        
        db.commit()
        print(f"✅ 已重置 {count} 个博主的准确率和等级")
    except Exception as e:
        print(f"❌ 重置失败: {e}")
        db.rollback()
    finally:
        db.close()


def reset_predictions_verify_status():
    """重置预测的验证状态"""
    print("\n" + "=" * 50)
    print("重置预测验证状态...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        predictions = db.query(Prediction).filter(
            Prediction.status.in_(['success', 'failed'])
        ).all()
        
        count = 0
        for pred in predictions:
            pred.is_correct = None
            pred.actual_change = None
            pred.verify_count = 0
            pred.verify_history = []
            pred.last_verify_date = None
            pred.status = 'pending'
            pred.is_expired = False
            pred.verify_score = 0
            count += 1
        
        db.commit()
        print(f"✅ 已重置 {count} 个预测的验证状态")
    except Exception as e:
        print(f"❌ 重置失败: {e}")
        db.rollback()
    finally:
        db.close()


def reverify_all_predictions():
    """使用新规则重新验证所有预测"""
    print("\n" + "=" * 50)
    print("重新验证所有预测（使用新规则）...")
    print("=" * 50)
    
    db = SessionLocal()
    service = PredictionVerifyService(db)
    
    try:
        today = date.today()
        
        predictions = db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.target_date <= today + timedelta(days=7)
        ).all()
        
        total = len(predictions)
        success_count = 0
        failed_count = 0
        total_score = 0
        
        print(f"\n找到 {total} 个待验证预测")
        print("-" * 50)
        
        for i, pred in enumerate(predictions):
            try:
                result = service.verify_prediction(pred.id)
                
                if result.get("success"):
                    success_count += 1
                    score = result.get("data", {}).get("score", 0)
                    total_score += score
                    
                    if (i + 1) % 10 == 0:
                        avg_score = total_score / success_count if success_count > 0 else 0
                        print(f"进度: {i+1}/{total} | 成功: {success_count} | 平均分: {avg_score:.1f}")
                else:
                    failed_count += 1
                    if "验证通道" not in result.get("message", ""):
                        print(f"  预测 {pred.id} 验证失败: {result.get('message')}")
                    
            except Exception as e:
                failed_count += 1
                print(f"  预测 {pred.id} 验证异常: {e}")
        
        print("\n" + "-" * 50)
        print(f"✅ 验证完成!")
        print(f"   总数: {total}")
        print(f"   成功: {success_count}")
        print(f"   失败: {failed_count}")
        if success_count > 0:
            avg_score = total_score / success_count
            print(f"   平均分: {avg_score:.1f}")
        
    except Exception as e:
        print(f"❌ 验证失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


def update_all_blogger_accuracy():
    """更新所有博主的准确率（使用加权准确率）"""
    print("\n" + "=" * 50)
    print("更新博主准确率（加权准确率）...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        bloggers = db.query(Blogger).all()
        
        for blogger in bloggers:
            predictions = db.query(Prediction).filter(
                Prediction.blogger_id == blogger.id,
                Prediction.status.in_(['success', 'failed'])
            ).all()
            
            total = len(predictions)
            
            if total > 0:
                total_score = sum(
                    p.verify_score if p.verify_score else (100 if p.is_correct else 0)
                    for p in predictions
                )
                weighted_accuracy = round(total_score / (total * 100) * 100, 2)
                
                correct = sum(1 for p in predictions if p.is_correct)
                
                blogger.accuracy_rate = weighted_accuracy
                blogger.total_predictions = total
                blogger.correct_predictions = correct
                
                if total < 3:
                    blogger.grade = 'C'
                elif weighted_accuracy >= 80 and total >= 10:
                    blogger.grade = 'S'
                elif weighted_accuracy >= 70 and total >= 5:
                    blogger.grade = 'A'
                elif weighted_accuracy >= 60 and total >= 3:
                    blogger.grade = 'B'
                elif weighted_accuracy >= 50:
                    blogger.grade = 'C'
                else:
                    blogger.grade = 'D'
            
            ultra_short_periods = ['1天', '2天', '3天', '1周']
            ultra_short_preds = [p for p in predictions if p.prediction_period in ultra_short_periods]
            if ultra_short_preds:
                us_total = len(ultra_short_preds)
                us_total_score = sum(
                    p.verify_score if p.verify_score else (100 if p.is_correct else 0)
                    for p in ultra_short_preds
                )
                us_correct = sum(1 for p in ultra_short_preds if p.is_correct)
                blogger.ultra_short_total = us_total
                blogger.ultra_short_correct = us_correct
                blogger.ultra_short_accuracy = round(us_total_score / (us_total * 100) * 100, 2) if us_total > 0 else 0
        
        db.commit()
        
        print("\n博主排名（按加权准确率排序）：")
        print("-" * 70)
        print(f"{'排名':<4} {'等级':<4} {'博主名称':<20} {'准确率':<8} {'预测数':<6} {'正确数':<6} {'平均分':<6}")
        print("-" * 70)
        
        bloggers_sorted = sorted(
            [b for b in bloggers if b.total_predictions and b.total_predictions > 0],
            key=lambda b: b.accuracy_rate or 0,
            reverse=True
        )
        
        for i, b in enumerate(bloggers_sorted, 1):
            avg_score = (b.accuracy_rate or 0) * (b.total_predictions or 1) / 100
            print(f"{i:<4} {b.grade:<4} {b.name:<20} {b.accuracy_rate:>6.1f}% {b.total_predictions:<6} {b.correct_predictions:<6} {avg_score:>5.1f}")
        
        print("\n✅ 博主准确率更新完成!")
        
    except Exception as e:
        print(f"❌ 更新失败: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


def main():
    print("\n" + "=" * 70)
    print("  重新验证预测并更新博主准确率")
    print("  新规则：过程验证 + 分数机制 + 加权准确率")
    print("=" * 70)
    
    print("\n新规则说明：")
    print("  1. 加权准确率 = 所有预测分数之和 / (预测总数 * 100) * 100")
    print("  2. 等级规则：")
    print("     - S级：准确率 ≥ 80% 且 预测数 ≥ 10")
    print("     - A级：准确率 ≥ 70% 且 预测数 ≥ 5")
    print("     - B级：准确率 ≥ 60% 且 预测数 ≥ 3")
    print("     - C级：准确率 ≥ 50% 或 预测数 < 3")
    print("     - D级：准确率 < 50%")
    
    confirm = input("\n⚠️  这将重置所有博主的准确率并重新验证预测，确定继续吗？(y/n): ")
    if confirm.lower() != 'y':
        print("已取消操作")
        return
    
    reset_blogger_accuracy()
    reset_predictions_verify_status()
    reverify_all_predictions()
    update_all_blogger_accuracy()
    
    print("\n" + "=" * 70)
    print("  全部完成！")
    print("=" * 70)


if __name__ == "__main__":
    main()
