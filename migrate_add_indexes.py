"""
数据库索引迁移脚本
为现有表添加缺失的索引以提升查询性能

使用方法:
    python migrate_add_indexes.py

注意:
    - 此脚本是幂等的，可以重复运行
    - 如果索引已存在，CREATE INDEX IF NOT EXISTS 会跳过
    - 不会影响现有数据
"""
import sqlite3
import sys
from pathlib import Path


def get_db_path():
    """获取数据库路径"""
    db_path = Path("data/fund_insight.db")
    if not db_path.exists():
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    return db_path


def add_indexes(db_path):
    """添加索引"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    indexes = [
        # Post 表索引
        ("ix_posts_blogger_id", "posts", ["blogger_id"]),
        ("ix_posts_post_date", "posts", ["post_date"]),
        ("ix_posts_blogger_date", "posts", ["blogger_id", "post_date"]),

        # Prediction 表索引
        ("ix_predictions_blogger_id", "predictions", ["blogger_id"]),
        ("ix_predictions_status", "predictions", ["status"]),
        ("ix_predictions_fund_code", "predictions", ["fund_code"]),
        ("ix_predictions_is_deleted", "predictions", ["is_deleted"]),
        ("ix_predictions_blogger_status", "predictions", ["blogger_id", "status", "is_deleted"]),
        ("ix_predictions_target_date", "predictions", ["target_date"]),

        # Viewpoint 表索引
        ("ix_viewpoints_is_deleted", "viewpoints", ["is_deleted"]),
        ("ix_viewpoints_viewpoint_date", "viewpoints", ["viewpoint_date"]),
        ("ix_viewpoints_blogger_id", "viewpoints", ["blogger_id"]),
        ("ix_viewpoints_source", "viewpoints", ["source"]),

        # Blogger 表索引
        ("ix_bloggers_platform", "bloggers", ["platform"]),
        ("ix_bloggers_is_active", "bloggers", ["is_active"]),
    ]

    print("开始添加索引...")

    for idx_name, table, columns in indexes:
        try:
            # 检查表是否存在
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if not cursor.fetchone():
                print(f"  [SKIP] 表 {table} 不存在，跳过索引 {idx_name}")
                continue

            # 创建索引（如果不存在）
            cols = ", ".join(columns)
            sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})"
            cursor.execute(sql)
            print(f"  [OK] 索引 {idx_name} 已添加到表 {table}")
        except Exception as e:
            print(f"  [FAIL] 创建索引 {idx_name} 失败: {e}")

    conn.commit()

    # 验证索引
    print("\n验证索引:")
    for idx_name, table, columns in indexes:
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='index' AND name='{idx_name}'")
        if cursor.fetchone():
            print(f"  [OK] {idx_name} 存在")
        else:
            print(f"  [FAIL] {idx_name} 不存在")

    conn.close()
    print("\n索引迁移完成!")


if __name__ == "__main__":
    db_path = get_db_path()
    print(f"数据库路径: {db_path}")
    add_indexes(db_path)
