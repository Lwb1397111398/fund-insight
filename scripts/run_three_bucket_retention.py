"""三桶保留 dry-run / 受控真删 CLI。

默认 dry-run，写出 docs/RETENTION_THREE_BUCKETS_DRY_RUN.json。
真删示例（需明确确认）：
  PYTHONPATH=. python scripts/run_three_bucket_retention.py --execute --confirm three-buckets-hard-delete
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models.database import SessionLocal
from src.services.retention_three_buckets import (
    CONFIRM_TOKEN,
    ThreeBucketRetentionService,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="三桶保留策略 dry-run / 真删")
    parser.add_argument(
        "--report",
        default="docs/RETENTION_THREE_BUCKETS_DRY_RUN.json",
        help="dry-run 报告路径",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真删（默认关闭；还需 --confirm）",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"真删确认口令，必须等于 {CONFIRM_TOKEN}",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        service = ThreeBucketRetentionService(session)
        plan = service.build_plan()
        report_path = service.write_dry_run_report(Path(args.report), plan)
        print(f"dry-run report: {report_path}")
        print(json.dumps(plan.to_report_dict()["counts"], ensure_ascii=False))
        print(f"total candidates: {plan.total}")

        if not args.execute:
            print("mode=dry-run (no deletes). Pass --execute --confirm ... to hard-delete.")
            return 0

        result = service.execute(
            dry_run=False,
            confirm_token=args.confirm,
            plan=plan,
        )
        print(json.dumps(result.get("deleted_counts"), ensure_ascii=False))
        print(result.get("message"))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
