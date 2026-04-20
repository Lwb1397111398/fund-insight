import sys
sys.path.insert(0, 'e:\\CountBot\\countbot\\workspace\\fund-insight')

from src.models.database import SessionLocal, Prediction, Post

db = SessionLocal()

# 查询帖子65的预测
post = db.query(Post).filter(Post.id == 65).first()
if post:
    print(f"帖子ID: {post.id}")
    print(f"标题: {post.title}")
    print(f"发布日期: {post.post_date}")
    print("\n预测列表:")
    
    predictions = db.query(Prediction).filter(
        Prediction.post_id == 65,
        Prediction.is_deleted == False
    ).all()
    
    for p in predictions:
        print(f"  预测ID {p.id}: 板块={p.sector}, 周期={p.prediction_period}, 目标日期={p.target_date}")
else:
    print("帖子不存在")

db.close()
