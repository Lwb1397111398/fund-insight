"""
数据库初始化脚本
"""
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "fund_insight.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def init_database():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    tables = {
        'bloggers': '''
            CREATE TABLE IF NOT EXISTS bloggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                platform VARCHAR(50) DEFAULT 'xiaohongshu',
                description TEXT,
                accuracy_rate FLOAT DEFAULT 0.0,
                total_predictions INTEGER DEFAULT 0,
                correct_predictions INTEGER DEFAULT 0,
                grade VARCHAR(5) DEFAULT 'C',
                recent_accuracy FLOAT DEFAULT 0.0,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'posts': '''
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blogger_id INTEGER NOT NULL,
                title VARCHAR(200),
                content TEXT NOT NULL,
                post_date DATE NOT NULL,
                source_url VARCHAR(500),
                analyzed BOOLEAN DEFAULT 0,
                analysis_result TEXT,
                auto_titled BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'predictions': '''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                blogger_id INTEGER NOT NULL,
                fund_code VARCHAR(20),
                fund_name VARCHAR(100),
                sector VARCHAR(100),
                sector_type VARCHAR(50),
                prediction_type VARCHAR(20) NOT NULL,
                prediction_content TEXT,
                confidence INTEGER DEFAULT 50,
                prediction_date DATE NOT NULL,
                prediction_period VARCHAR(20) DEFAULT '1个月',
                target_date DATE,
                status VARCHAR(20) DEFAULT 'pending',
                start_nav FLOAT,
                start_nav_date DATE,
                current_nav FLOAT,
                current_nav_date DATE,
                end_nav FLOAT,
                end_nav_date DATE,
                actual_change FLOAT,
                is_correct BOOLEAN,
                ai_judgment TEXT,
                verified_at DATETIME,
                verify_count INTEGER DEFAULT 0,
                verify_history TEXT,
                last_verify_date DATE,
                next_verify_date DATE,
                is_expired BOOLEAN DEFAULT 0,
                has_active_prediction BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'viewpoints': '''
            CREATE TABLE IF NOT EXISTS viewpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blogger_id INTEGER NOT NULL,
                post_id INTEGER,
                market_direction VARCHAR(20),
                confidence INTEGER DEFAULT 50,
                sectors_bullish TEXT,
                sectors_bearish TEXT,
                reasoning TEXT,
                viewpoint_date DATE NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'fund_history': '''
            CREATE TABLE IF NOT EXISTS fund_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code VARCHAR(20) NOT NULL,
                fund_name VARCHAR(100),
                nav_date DATE NOT NULL,
                nav FLOAT NOT NULL,
                day_growth FLOAT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'fund_info': '''
            CREATE TABLE IF NOT EXISTS fund_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code VARCHAR(20) UNIQUE NOT NULL,
                fund_name VARCHAR(100),
                fund_type VARCHAR(50),
                sector_type VARCHAR(50),
                latest_nav FLOAT,
                nav_date DATE,
                day_growth FLOAT,
                week_growth FLOAT,
                month_growth FLOAT,
                last_analyze_date DATE,
                active_predictions INTEGER DEFAULT 0,
                can_delete BOOLEAN DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'sector_fund_mapping': '''
            CREATE TABLE IF NOT EXISTS sector_fund_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_name VARCHAR(50) UNIQUE NOT NULL,
                fund_code VARCHAR(20) NOT NULL,
                fund_name VARCHAR(100),
                keywords TEXT,
                is_active BOOLEAN DEFAULT 1
            )''',
        'investment_advice': '''
            CREATE TABLE IF NOT EXISTS investment_advice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                advice_date DATE NOT NULL,
                advice_type VARCHAR(20),
                advice_content TEXT,
                referenced_bloggers TEXT,
                referenced_predictions TEXT,
                market_sentiment VARCHAR(20),
                confidence INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''',
        'verification_tasks': '''
            CREATE TABLE IF NOT EXISTS verification_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                task_date DATE NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                result TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )'''
    }
    
    for table_name, create_sql in tables.items():
        try:
            cursor.execute(create_sql)
            print(f"✓ {table_name}")
        except Exception as e:
            print(f"✗ {table_name}: {e}")
    
    conn.commit()
    conn.close()
    print("\n数据库初始化完成!")

if __name__ == "__main__":
    init_database()
