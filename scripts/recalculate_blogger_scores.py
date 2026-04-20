"""
重新计算所有博主的累计分数
使用累计分数制：total_verify_score
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import SessionLocal, Prediction, Blogger


def recalculate_all_bloggers():
    """重新计算所有博主的累计分数"""
    print("=" * 60)
    print("重新计算博主累计分数")
    print("=" * 60)
    
    db = SessionLocal()
    
    try:
        bloggers = db.query(Blogger).all()
        
        print(f"\n找到 {len(bloggers)} 个博主")
        print("-" * 60)
        
        for blogger in bloggers:
            predictions = db.query(Prediction).filter(
                Prediction.blogger_id == blogger.id,
                Prediction.status.in_(['success', 'failed'])
            ).all()
            
            total = len(predictions)
            total_score = 0
            correct = 0
            
            for p in predictions:
                score = p.verify_score if p.verify_score is not None else (100 if p.is_correct else 0)
                total_score += score
                if p.is_correct:
                    correct += 1
            
            blogger.total_predictions = total
            blogger.correct_predictions = correct
            blogger.total_verify_score = total_score
            
            if total > 0:
                blogger.accuracy_rate = round(total_score / (total * 100) * 100, 2)
                
                if total < 3:
                    blogger.grade = 'C'
                elif blogger.accuracy_rate >= 80 and total >= 10:
                    blogger.grade = 'S'
                elif blogger.accuracy_rate >= 70 and total >= 5:
                    blogger.grade = 'A'
                elif blogger.accuracy_rate >= 60 and total >= 3:
                    blogger.grade = 'B'
                elif blogger.accuracy_rate >= 50:
                    blogger.grade = 'C'
                else:
                    blogger.grade = 'D'
            else:
                blogger.accuracy_rate = 0
                blogger.grade = 'C'
        
        db.commit()
        
        print("\n博主排名（按加权准确率排序）：")
        print("-" * 70)
        print(f"{'排名':<4} {'等级':<4} {'博主名称':<20} {'加权准确率':<10} {'预测数':<6} {'正确数':<6} {'累计分数':<8}")
        print("-" * 70)
        
        bloggers_sorted = sorted(
            [b for b in bloggers if b.total_predictions > 0],
            key=lambda b: b.accuracy_rate or 0,
            reverse=True
        )
        
        for i, b in enumerate(bloggers_sorted, 1):
            print(f"{i:<4} {b.grade:<4} {b.name:<20} {b.accuracy_rate:>6.1f}%    {b.total_predictions:<6} {b.correct_predictions:<6} {b.total_verify_score:<8}")
        
        print("\n无预测数据的博主：")
        for b in bloggers:
            if b.total_predictions == 0:
                print(f"  - {b.name}")
        
        print("\n✅ 累计分数计算完成!")
        
    except Exception as e:
        print(f"❌ 计算失败: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    recalculate_all_bloggers()
