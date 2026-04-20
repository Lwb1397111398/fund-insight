import sys
sys.path.insert(0, 'e:\\CountBot\\countbot\\workspace\\fund-insight')

from src.models.database import SessionLocal, Prediction, Post
from datetime import date

db = SessionLocal()

# 查询最近分析的帖子（今天）
today = date(2026, 3, 11)
posts = db.query(Post).filter(
    Post.post_date == today,
    Post.analyzed == True
).order_by(Post.id.desc()).limit(5).all()

print(f"日期: {today}")
print(f"最近分析的帖子数: {len(posts)}\n")

for post in posts:
    print(f"帖子ID {post.id}: {post.title[:40]}...")
    
    predictions = db.query(Prediction).filter(
        Prediction.post_id == post.id,
        Prediction.is_deleted == False
    ).all()
    
    if predictions:
        periods = [p.prediction_period for p in predictions]
        print(f"  预测数: {len(predictions)}")
        print(f"  周期列表: {periods}")
        if len(set(periods)) == 1:
            print(f"  ⚠️  所有预测周期相同: {periods[0]}")
        else:
            print(f"  ✓ 预测周期不同")
    print()

db.close()
