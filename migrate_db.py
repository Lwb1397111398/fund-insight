"""
数据库迁移脚本 - 添加所有缺失的字段
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "fund_insight.db"

def migrate_table(cursor, table_name, new_columns):
    """迁移单个表"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cursor.fetchall()}
    
    print(f"\n检查表 {table_name}...")
    print(f"  现有字段: {len(existing_columns)} 个")
    
    added_count = 0
    for column_name, column_type in new_columns:
        if column_name not in existing_columns:
            try:
                sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                cursor.execute(sql)
                print(f"  ✓ 添加字段: {column_name}")
                added_count += 1
            except Exception as e:
                print(f"  ✗ 添加字段失败 {column_name}: {e}")
        else:
            print(f"  - 字段已存在: {column_name}")
    
    return added_count

def migrate_database():
    """迁移数据库 - 添加缺失字段"""
    if not DB_PATH.exists():
        print("数据库文件不存在，无需迁移")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    print("=" * 60)
    print("开始数据库迁移...")
    print("=" * 60)
    
    total_added = 0
    
    # 1. 博主表
    blogger_columns = [
        ("ultra_short_accuracy", "FLOAT DEFAULT 0.0"),
        ("ultra_short_total", "INTEGER DEFAULT 0"),
        ("ultra_short_correct", "INTEGER DEFAULT 0"),
        ("medium_long_accuracy", "FLOAT DEFAULT 0.0"),
        ("medium_long_total", "INTEGER DEFAULT 0"),
        ("medium_long_correct", "INTEGER DEFAULT 0"),
        ("sector_coverage", "INTEGER DEFAULT 0"),
        ("avg_prediction_period", "FLOAT DEFAULT 0.0"),
        ("risk_warning_count", "INTEGER DEFAULT 0"),
        ("last_prediction_date", "DATE"),
        ("prediction_frequency", "FLOAT DEFAULT 0.0"),
    ]
    total_added += migrate_table(cursor, 'bloggers', blogger_columns)
    
    # 2. 预测表
    prediction_columns = [
        ("is_deleted", "BOOLEAN DEFAULT 0"),
        ("deleted_at", "DATETIME"),
        ("deleted_by", "VARCHAR(50)"),
        ("delete_reason", "VARCHAR(200)"),
        ("restore_before", "DATE"),
    ]
    total_added += migrate_table(cursor, 'predictions', prediction_columns)
    
    # 3. 观点表
    viewpoint_columns = [
        ("fund_code", "VARCHAR(20)"),
        ("fund_name", "VARCHAR(100)"),
        ("content", "TEXT"),
        ("author", "VARCHAR(100) DEFAULT '网友'"),
        ("source", "VARCHAR(50) DEFAULT 'manual'"),
        ("article_id", "VARCHAR(100)"),
        ("article_url", "VARCHAR(500)"),
        ("content_hash", "VARCHAR(32)"),
        ("reasoning", "TEXT"),
        ("time_horizon", "VARCHAR(20) DEFAULT 'medium'"),
        ("validity_period", "VARCHAR(20) DEFAULT '1个月'"),
        ("valid_until", "DATE"),
        ("score", "FLOAT DEFAULT 0.0"),
        ("content_depth", "FLOAT DEFAULT 0.0"),
        ("timeliness", "FLOAT DEFAULT 0.0"),
        ("data_support", "FLOAT DEFAULT 0.0"),
        ("reference_value", "FLOAT DEFAULT 0.0"),
        ("viewpoint_type", "VARCHAR(50) DEFAULT '深度分析'"),
        ("credibility_score", "INTEGER DEFAULT 50"),
        ("credibility_factors", "TEXT"),
        ("tags", "TEXT"),
        ("analysis_tags", "TEXT"),
        ("is_expired", "BOOLEAN DEFAULT 0"),
        ("needs_reassessment", "BOOLEAN DEFAULT 0"),
        ("reassessment_reason", "VARCHAR(200)"),
        ("weight", "FLOAT DEFAULT 1.0"),
        ("source_authority", "FLOAT DEFAULT 0.5"),
        ("read_count", "INTEGER DEFAULT 0"),
        ("is_vip", "BOOLEAN DEFAULT 0"),
        ("action_suggestion", "VARCHAR(20)"),
        ("risk_level", "VARCHAR(20) DEFAULT 'medium'"),
        ("analysis_summary", "TEXT"),
        ("is_deleted", "BOOLEAN DEFAULT 0"),
        ("deleted_at", "DATETIME"),
        ("deleted_by", "VARCHAR(50)"),
        ("delete_reason", "VARCHAR(200)"),
        ("restore_before", "DATE"),
    ]
    total_added += migrate_table(cursor, 'viewpoints', viewpoint_columns)
    
    # 4. 基金信息表
    fund_info_columns = [
        ("estimated_nav", "FLOAT"),
        ("estimated_nav_time", "DATETIME"),
        ("actual_nav", "FLOAT"),
        ("actual_nav_time", "DATETIME"),
        ("nav_source", "VARCHAR(20) DEFAULT 'eastmoney'"),
        ("fund_scale", "FLOAT"),
        ("establish_date", "DATE"),
        ("manager_name", "VARCHAR(100)"),
        ("fee_rate", "FLOAT"),
        ("sharpe_ratio", "FLOAT"),
        ("max_drawdown", "FLOAT"),
        ("since_inception_return", "FLOAT"),
        ("data_quality", "VARCHAR(20) DEFAULT 'normal'"),
        ("data_quality_note", "VARCHAR(200)"),
        ("support_level", "FLOAT"),
        ("resistance_level", "FLOAT"),
        ("ma5", "FLOAT"),
        ("ma10", "FLOAT"),
        ("ma20", "FLOAT"),
        ("vs_sector", "FLOAT"),
        ("vs_market", "FLOAT"),
        ("performance_type", "VARCHAR(20)"),
        ("is_core_fund", "BOOLEAN DEFAULT 0"),
    ]
    total_added += migrate_table(cursor, 'fund_info', fund_info_columns)
    
    conn.commit()
    conn.close()
    
    print("\n" + "=" * 60)
    print(f"迁移完成！共添加 {total_added} 个字段")
    print("=" * 60)

if __name__ == "__main__":
    migrate_database()
