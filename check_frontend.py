import sys
sys.path.insert(0, 'e:\\CountBot\\countbot\\workspace\\fund-insight')

from src.models.database import SessionLocal, Prediction, Post
from datetime import date

db = SessionLocal()

# 检查3月11日的帖子
today = date(2026, 3, 11)
posts = db.query(Post).filter(
    Post.post_date == today,
    Post.analyzed == True
).order_by(Post.id.desc()).all()

print(f"=== 3月11日帖子预测时间检查 ===\n")

for post in posts[:3]:  # 只检查前3个
    print(f"帖子ID {post.id}: {post.title[:30]}...")
    
    predictions = db.query(Prediction).filter(
        Prediction.post_id == post.id,
        Prediction.is_deleted == False
    ).all()
    
    periods = [p.prediction_period for p in predictions]
    print(f"  预测周期: {periods}")
    
    if len(set(periods)) == 1:
        print(f"  ⚠️  所有周期相同: {periods[0]}")
    else:
        print(f"  ✓ 周期不同")
    print()

# 特别检查帖子ID 71（乐橙姐姐）
print("=== 特别检查：帖子ID 71（乐橙姐姐）===")
post71 = db.query(Post).filter(Post.id == 71).first()
if post71:
    preds = db.query(Prediction).filter(
        Prediction.post_id == 71,
        Prediction.is_deleted == False
    ).all()
    for p in preds:
        print(f"  预测ID {p.id}: {p.sector} | 周期={p.prediction_period} | 目标日期={p.target_date}")

db.close()
