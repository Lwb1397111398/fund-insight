import sqlite3

conn = sqlite3.connect('E:\\CountBot\\countbot\\workspace\\fund-insight\\data\\fund_insight.db')
cursor = conn.cursor()

# 查询总预测数
cursor.execute("SELECT COUNT(*) FROM predictions WHERE is_deleted=0")
total = cursor.fetchone()[0]
print(f"总预测数（未删除）: {total}")

# 查询按(fund_code, prediction_type, target_date)分组
cursor.execute("""
    SELECT fund_code, prediction_type, target_date, COUNT(*) as cnt
    FROM predictions
    WHERE is_deleted=0
    GROUP BY fund_code, prediction_type, target_date
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
""")

duplicates = cursor.fetchall()
print(f"\n有重复的组数: {len(duplicates)}")
for row in duplicates:
    print(f"  {row[0]}, {row[1]}, {row[2]}: {row[3]}条")

conn.close()
