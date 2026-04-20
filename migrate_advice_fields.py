"""
数据库迁移脚本 - 添加投资建议新字段
运行方式: python migrate_advice_fields.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from src.models.database import engine


def migrate():
    """添加投资建议表新字段"""
    print("开始迁移投资建议表...")
    
    new_columns = [
        ("reasoning", "TEXT"),
        ("risk_warning", "TEXT"),
        ("suggested_sectors", "JSON"),
        ("avoid_sectors", "JSON"),
    ]
    
    with engine.connect() as conn:
        for col_name, col_type in new_columns:
            try:
                conn.execute(text(f"ALTER TABLE investment_advice ADD COLUMN {col_name} {col_type}"))
                conn.commit()
                print(f"✅ 添加字段: {col_name}")
            except Exception as e:
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    print(f"⏭️ 字段已存在: {col_name}")
                else:
                    print(f"❌ 添加字段失败 {col_name}: {e}")
    
    print("\n迁移完成！")


if __name__ == "__main__":
    migrate()
