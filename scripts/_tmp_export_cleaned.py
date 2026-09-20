# -*- coding: utf-8 -*-
"""临时脚本：把本地清洗完成的镜像库导出为线上可导入的 JSON。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(ROOT, "data", "fund_insight.db").replace("\\", "/")
sys.path.insert(0, ROOT)

from src.core import config  # noqa: F401
from src.models.database import SessionLocal  # noqa: E402
from src.services.data_portability_service import DataPortabilityService  # noqa: E402

OUT = os.path.join(ROOT, "data", "fund_insight_cleaned_export_2026-08-06.json")


def main():
    db = SessionLocal()
    try:
        payload = DataPortabilityService(db).export_data()
    finally:
        db.close()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    size_mb = os.path.getsize(OUT) / 1024 / 1024
    print(f"导出完成: {OUT} ({size_mb:.1f} MB)")
    print("summary:", json.dumps(payload.get("summary", {}), ensure_ascii=False))


if __name__ == "__main__":
    main()
