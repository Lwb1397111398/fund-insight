#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库迁移脚本 - 添加缺失的列
修复: sqlite3.OperationalError: no such column: bloggers.updated_at
"""
import sqlite3
import os

def migrate_database():
    db_path = os.path.join(os.path.dirname(__file__), 'data', 'fund_insight.db')
    
    if not os.path.exists(db_path):
        print(f"[Migration] 数据库文件不存在: {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    migrations = []
    
    cursor.execute("PRAGMA table_info(bloggers)")
    blogger_columns = [row[1] for row in cursor.fetchall()]
    
    if 'updated_at' not in blogger_columns:
        migrations.append(("bloggers", "updated_at", "DATETIME"))
    
    if 'ultra_short_accuracy' not in blogger_columns:
        migrations.append(("bloggers", "ultra_short_accuracy", "FLOAT DEFAULT 0.0"))
    
    if 'ultra_short_total' not in blogger_columns:
        migrations.append(("bloggers", "ultra_short_total", "INTEGER DEFAULT 0"))
    
    if 'ultra_short_correct' not in blogger_columns:
        migrations.append(("bloggers", "ultra_short_correct", "INTEGER DEFAULT 0"))
    
    cursor.execute("PRAGMA table_info(predictions)")
    prediction_columns = [row[1] for row in cursor.fetchall()]
    
    if 'verify_score' not in prediction_columns:
        migrations.append(("predictions", "verify_score", "INTEGER DEFAULT 0"))
    
    if 'verify_count' not in prediction_columns:
        migrations.append(("predictions", "verify_count", "INTEGER DEFAULT 0"))
    
    if 'verify_history' not in prediction_columns:
        migrations.append(("predictions", "verify_history", "JSON"))
    
    if 'is_expired' not in prediction_columns:
        migrations.append(("predictions", "is_expired", "BOOLEAN DEFAULT 0"))
    
    if 'has_active_prediction' not in prediction_columns:
        migrations.append(("predictions", "has_active_prediction", "BOOLEAN DEFAULT 1"))
    
    if not migrations:
        print("[Migration] 数据库结构已是最新，无需迁移")
        conn.close()
        return
    
    print(f"[Migration] 发现 {len(migrations)} 个需要添加的列")
    
    for table, column, column_type in migrations:
        try:
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            cursor.execute(sql)
            print(f"[Migration] 已添加列: {table}.{column}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                print(f"[Migration] 列已存在，跳过: {table}.{column}")
            else:
                print(f"[Migration] 添加列失败: {table}.{column} - {e}")
    
    conn.commit()
    conn.close()
    print("[Migration] 数据库迁移完成")

if __name__ == "__main__":
    migrate_database()
