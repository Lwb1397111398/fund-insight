"""
修复孤儿预测 - 为没有关联帖子的预测创建帖子
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Post, Prediction, Blogger
from datetime import datetime


def fix_orphan_predictions(dry_run: bool = True):
    """
    修复孤儿预测
    
    策略：
    1. 找到所有孤儿预测
    2. 按博主和日期分组
    3. 为每组创建一个新的帖子
    4. 更新预测的 post_id
    """
    db = SessionLocal()
    
    try:
        # 查询所有预测
        all_predictions = db.query(Prediction).order_by(Prediction.id.desc()).limit(100).all()
        
        orphan_preds = []
        for pred in all_predictions:
            post = db.query(Post).filter(Post.id == pred.post_id).first()
            if not post:
                orphan_preds.append(pred)
        
        if not orphan_preds:
            print("✓ 没有发现孤儿预测")
            return
        
        print(f"发现 {len(orphan_preds)} 条孤儿预测")
        print("="*60)
        
        # 按博主ID分组
        by_blogger = {}
        for pred in orphan_preds:
            if pred.blogger_id not in by_blogger:
                by_blogger[pred.blogger_id] = []
            by_blogger[pred.blogger_id].append(pred)
        
        for blogger_id, preds in by_blogger.items():
            blogger = db.query(Blogger).filter(Blogger.id == blogger_id).first()
            if not blogger:
                print(f"\n⚠️  博主 {blogger_id} 不存在，跳过 {len(preds)} 条预测")
                continue
            
            print(f"\n博主: {blogger.name} (ID: {blogger_id})")
            print(f"  孤儿预测数量: {len(preds)}")
            
            # 按日期分组
            by_date = {}
            for pred in preds:
                date_key = pred.prediction_date.strftime('%Y-%m-%d') if pred.prediction_date else 'unknown'
                if date_key not in by_date:
                    by_date[date_key] = []
                by_date[date_key].append(pred)
            
            for date_key, date_preds in by_date.items():
                print(f"\n  日期: {date_key}")
                print(f"    预测数量: {len(date_preds)}")
                
                # 检查是否已存在该日期的帖子
                existing_post = db.query(Post).filter(
                    Post.blogger_id == blogger_id,
                    Post.post_date == date_key
                ).first()
                
                if existing_post:
                    print(f"    ✓ 已存在帖子 ID: {existing_post.id}")
                    target_post_id = existing_post.id
                else:
                    # 创建新帖子
                    if not dry_run:
                        new_post = Post(
                            blogger_id=blogger_id,
                            title=f"{date_key} {blogger.name} (自动修复)",
                            content="该帖子由系统自动创建，用于关联孤儿预测。",
                            post_date=datetime.strptime(date_key, '%Y-%m-%d').date() if date_key != 'unknown' else datetime.now().date(),
                            source_url=None,
                            analyzed=True,
                            auto_titled=True
                        )
                        db.add(new_post)
                        db.flush()
                        target_post_id = new_post.id
                        print(f"    ✓ 创建新帖子 ID: {target_post_id}")
                    else:
                        target_post_id = "NEW_POST_ID"
                        print(f"    [试运行] 将创建新帖子")
                
                # 更新预测的 post_id
                for pred in date_preds:
                    if not dry_run:
                        old_post_id = pred.post_id
                        pred.post_id = target_post_id
                        print(f"      更新预测 {pred.id}: post_id {old_post_id} -> {target_post_id}")
                    else:
                        print(f"      [试运行] 预测 {pred.id}: post_id {pred.post_id} -> {target_post_id}")
        
        if not dry_run:
            db.commit()
            print("\n✓ 修复完成，已提交数据库")
        else:
            print("\n[试运行模式] 未实际修改数据库")
            print("如需正式执行，请添加 --execute 参数")
        
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="修复孤儿预测")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="正式执行（默认试运行）"
    )
    
    args = parser.parse_args()
    fix_orphan_predictions(dry_run=not args.execute)
