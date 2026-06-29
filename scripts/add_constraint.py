"""添加唯一约束"""
from src.models.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    # 清理重复数据
    conn.execute(text("""
        DELETE FROM sector_fund_flow a
        USING sector_fund_flow b
        WHERE a.id < b.id
          AND a.flow_date = b.flow_date
          AND a.sector_name = b.sector_name
    """))
    conn.commit()
    print("duplicates cleaned")

    # 添加唯一约束
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uix_sector_flow_date_name
        ON sector_fund_flow (flow_date, sector_name)
    """))
    conn.commit()
    print("unique constraint created")
