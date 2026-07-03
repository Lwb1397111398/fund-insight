"""
板块资金流向抓取脚本

GitHub Actions、手动命令和补抓任务统一通过服务层执行，避免脚本与 API 口径分裂。
"""
import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.models.database import SessionLocal, init_db
from src.services.sector_flow_service import SectorFlowService


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取板块资金流向数据")
    parser.add_argument("--trigger", default="github_actions", help="触发来源标识")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        service = SectorFlowService(db)
        result = service.run_fetch(trigger=args.trigger)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0 if result.get("success") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
