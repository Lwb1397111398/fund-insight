"""
添加基金性能索引
默认只打印 SQL，生产环境需人工确认后使用 --execute 执行。
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlalchemy import text

from src.models.database import SessionLocal


INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS ix_fund_info_sector_type ON fund_info (sector_type)",
    "CREATE INDEX IF NOT EXISTS ix_fund_info_active_predictions ON fund_info (active_predictions)",
    "CREATE INDEX IF NOT EXISTS ix_fund_info_last_analyze_date ON fund_info (last_analyze_date)",
]


def add_fund_performance_indexes(dry_run: bool = True):
    """添加基金查询相关索引"""
    print("基金性能索引")
    print("=" * 60)
    for statement in INDEX_STATEMENTS:
        print(statement + ";")

    if dry_run:
        print("\n[试运行模式] 未修改数据库")
        print("如需正式执行，请添加 --execute 参数")
        return

    db = SessionLocal()
    try:
        for statement in INDEX_STATEMENTS:
            db.execute(text(statement))
        db.commit()
        print("\n索引创建完成")
    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="添加基金性能索引")
    parser.add_argument("--execute", action="store_true", help="正式执行（默认试运行）")
    args = parser.parse_args()

    add_fund_performance_indexes(dry_run=not args.execute)
