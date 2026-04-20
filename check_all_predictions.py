import sqlite3

conn = sqlite3.connect('E:\\CountBot\\countbot\\workspace\\fund-insight\\data\\fund_insight.db')
cursor = conn.cursor()

# 查询所有512290基金的预测
cursor.execute("""
    SELECT id, fund_code, prediction_type, target_date, confidence, is_deleted, blogger_id 
    FROM predictions 
    WHERE fund_code='512290' AND is_deleted=0
    ORDER BY target_date, prediction_type
""")

rows = cursor.fetchall()
print(f"找到 {len(rows)} 条512290基金的预测:\n")
for row in rows:
    print(f"ID: {row[0]}, 类型: {row[2]}, 目标日期: {row[3]}, 置信度: {row[4]}, 博主ID: {row[6]}")

print("\n\n按(target_date, prediction_type)分组:")
from collections import defaultdict
groups = defaultdict(list)
for row in rows:
    key = (row[3], row[2])  # (target_date, prediction_type)
    groups[key].append(row)

for key, group in groups.items():
    if len(group) > 1:
        print(f"\n组 {key}: {len(group)}条预测")
        for row in group:
            print(f"  - ID: {row[0]}, 置信度: {row[4]}, 博主ID: {row[6]}")

conn.close()
