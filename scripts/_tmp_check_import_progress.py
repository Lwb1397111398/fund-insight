"""只读检查线上覆盖导入进度：任务状态 + 各表行数 + 活跃会话。"""
import os
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DATABASE_URL"])

with engine.connect() as conn:
    row = conn.execute(
        text("SELECT config_value FROM system_config WHERE config_key = 'data_import_job'")
    ).fetchone()
    print("=== 导入任务状态 ===")
    if row and row[0]:
        payload = json.loads(row[0])
        print(f"status={payload.get('status')}  started_at={payload.get('started_at')}  finished_at={payload.get('finished_at')}")
        msg = payload.get("message")
        if msg:
            print(f"message={msg}")
        if payload.get("result"):
            result = payload["result"]
            print(f"result.success={result.get('success')}")
            if result.get("data"):
                d = result["data"]
                print(f"total_imported={d.get('total_imported')}  total_skipped={d.get('total_skipped')}")
                if d.get("failed") and any(v for v in d["failed"].values()):
                    print(f"failed={d['failed']}")
            if result.get("message"):
                print(f"result.message={result['message'][:500]}")
    else:
        print("(无任务记录)")

    print("\n=== 各表行数（对比导出量 约 11000） ===")
    for t in ["bloggers", "posts", "predictions", "viewpoints", "fund_info",
              "fund_history", "sector_alias", "sector_fund_mapping",
              "investment_advice", "batch_analysis_tasks", "verification_tasks",
              "prediction_groups", "crawler_article_records", "analysis_logs"]:
        n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()[0]
        print(f"{t}: {n}")

    print("\n=== 活跃查询（最近 3 条） ===")
    rows = conn.execute(text(
        "SELECT pid, state, now() - query_start AS age, left(query, 90) "
        "FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() ORDER BY query_start DESC LIMIT 3"
    )).fetchall()
    for pid, state, age, q in rows:
        print(f"pid={pid} state={state} age={age} query={q}")
