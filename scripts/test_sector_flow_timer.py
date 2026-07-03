"""短延迟抢筹抓取验证脚本"""
import argparse
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.models.database import SessionLocal
from src.services.sector_flow_service import SectorFlowService


def main() -> int:
    parser = argparse.ArgumentParser(description="等待指定秒数后执行抢筹抓取验证")
    parser.add_argument("--delay", type=int, default=30)
    args = parser.parse_args()

    print(f"将在 {args.delay} 秒后执行抢筹抓取验证...")
    time.sleep(args.delay)

    db = SessionLocal()
    try:
        service = SectorFlowService(db)
        result = service.run_fetch(trigger="test_timer")
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0 if result.get("success") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
