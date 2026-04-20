"""
数据库迁移脚本 - 添加观点汇总字段
"""
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlalchemy import text
from src.models.database import engine, SessionLocal


def migrate():
    """添加观点汇总相关字段"""
    db = SessionLocal()
    
    try:
        print("[迁移] 开始添加观点汇总字段...")
        
        result = db.execute(text("PRAGMA table_info(viewpoints)"))
        columns = [row[1] for row in result.fetchall()]
        
        if 'is_summary' not in columns:
            db.execute(text("ALTER TABLE viewpoints ADD COLUMN is_summary BOOLEAN DEFAULT 0"))
            print("[迁移] 添加字段: is_summary")
        
        if 'original_count' not in columns:
            db.execute(text("ALTER TABLE viewpoints ADD COLUMN original_count INTEGER DEFAULT 0"))
            print("[迁移] 添加字段: original_count")
        
        if 'original_ids' not in columns:
            db.execute(text("ALTER TABLE viewpoints ADD COLUMN original_ids JSON"))
            print("[迁移] 添加字段: original_ids")
        
        if 'topics' not in columns:
            db.execute(text("ALTER TABLE viewpoints ADD COLUMN topics JSON"))
            print("[迁移] 添加字段: topics")
        
        db.commit()
        print("[迁移] 观点汇总字段添加完成！")
        
    except Exception as e:
        db.rollback()
        print(f"[迁移] 失败: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
