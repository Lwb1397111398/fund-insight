import sys
sys.path.insert(0, 'e:\\CountBot\\countbot\\workspace\\fund-insight')

from src.models.database import SessionLocal, Prediction, Post

db = SessionLocal()

# 检查帖子ID 69
post = db.query(Post).filter(Post.id == 69).first()
if post:
    print(f"帖子ID 69: {post.title}")
    print(f"内容:\n{post.content[:500]}...\n")
    
    predictions = db.query(Prediction).filter(
        Prediction.post_id == 69,
        Prediction.is_deleted == False
    ).all()
    
    print("预测列表:")
    for p in predictions:
        print(f"  - {p.sector}: {p.prediction_content[:50]}... | 周期={p.prediction_period}")

db.close()
