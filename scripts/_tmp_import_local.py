# -*- coding: utf-8 -*-
"""临时脚本：把线上导出的 JSON 导入本地 SQLite 镜像库（不影响生产 Supabase）。

用法：DATABASE_URL 由脚本内部强制指向本地 SQLite。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(ROOT, "data", "fund_insight.db").replace("\\", "/")
sys.path.insert(0, ROOT)

from src.core import config  # noqa: F401  先触发 load_dotenv（不会覆盖已设置的 env）
from src.models.database import SessionLocal, init_db  # noqa: E402
from src.services.data_portability_service import DataPortabilityService  # noqa: E402

EXPORT_FILE = r"C:\Users\李文彬\Downloads\fund_insight_export_2026-08-05.json"


def main():
    init_db()
    with open(EXPORT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    db = SessionLocal()
    try:
        result = DataPortabilityService(db).import_data(data)
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
