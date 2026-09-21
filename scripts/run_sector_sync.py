# -*- coding: utf-8 -*-
"""用审查过的板块映射纠正预测的基金标的（S4 第 4 步）。

复用 `PredictionMaintenanceService.sync_sector_mappings`，本脚本只负责：
  1) 先 dry-run 出对照表（CSV + JSON 报告），人工看过再决定是否 apply；
  2) apply 时固定一个 run_id（写进 change log，供 restore_prediction_batch.py 整批回滚）；
  3) 跑完把"预期改动数"与"实际改动数"对账，不一致就非 0 退出。

用法：
    DATABASE_URL="sqlite:///<本地镜像库>" python scripts/run_sector_sync.py --min-confidence 0.85
    ... python scripts/run_sector_sync.py --apply --run-id rematch-20260921
    ... python scripts/run_sector_sync.py --apply --run-id rematch-20260921 --expect 250
"""
import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, "docs", "迭代计划", "run-2026-09-20")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正写库（默认只 dry-run）')
    ap.add_argument('--min-confidence', type=float, default=0.85)
    ap.add_argument('--run-id', default=None)
    ap.add_argument('--expect', type=int, default=None,
                    help='预期改动条数；不匹配则非 0 退出（防止无声跑偏）')
    ap.add_argument('--tag', default='sync')
    args = ap.parse_args()

    _db_guard.pin_local_sqlite()
    from src.models.database import SessionLocal
    from src.services.prediction_maintenance_service import PredictionMaintenanceService

    os.makedirs(OUT_DIR, exist_ok=True)
    db = SessionLocal()
    try:
        service = PredictionMaintenanceService(db)
        preview = service.sync_sector_mappings(
            dry_run=True, min_confidence=args.min_confidence)
        print('[dry-run] 可改映射 %d 条（低于置信度门槛跳过 %d 条）'
              % (preview['total_mappings'], preview['mappings_skipped_low_confidence']))
        print('[dry-run] 预计改动预测 %d 条，其中需重置验证结论 %d 条；无映射 %d 条；已一致 %d 条'
              % (preview['would_update'],
                 sum(1 for d in preview['details'] if d['reset_verified']),
                 preview['predictions_no_mapping'], preview['predictions_unchanged']))

        csv_path = os.path.join(OUT_DIR, 'remap-%s-preview.csv' % args.tag)
        with io.open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(preview['details'][0].keys())
                                    if preview['details'] else ['prediction_id'])
            writer.writeheader()
            writer.writerows(preview['details'])
        print('[ok] 对照表：%s（%d 行）' % (csv_path, len(preview['details'])))

        if not args.apply:
            print('\n未写库。确认无误后加 --apply --run-id <id> 执行。')
            return 0

        run_id = args.run_id or 'rematch-%s-%s' % (
            args.tag, datetime.now().strftime('%Y%m%d-%H%M%S'))
        # run_id 是**唯一**的回滚钥匙：同一个 id 挂两批改动 = 一条回滚命令把两批
        # 一起退掉。第 8 轮实测 `rematch-20260921b` 上挂着 645 条 change log，
        # 其中 530 条是上一批（commit 48feaf6）的，而 `--expect` 只看条数看不出串批。
        from src.models.database import PredictionChangeLog
        used = db.query(PredictionChangeLog).filter(
            PredictionChangeLog.run_id == run_id).count()
        if used:
            print('[abort] run_id=%s 已经用过（%d 条改动记录），回滚会连旧账一起退；'
                  '换一个 id（不带 --run-id 会自动生成带时间戳的）' % (run_id, used))
            return 4
        result = service.sync_sector_mappings(
            dry_run=False, min_confidence=args.min_confidence, run_id=run_id)
        report = os.path.join(OUT_DIR, 'remap-%s-applied.json' % args.tag)
        with io.open(report, 'w', encoding='utf-8') as f:
            json.dump({'run_id': run_id, 'min_confidence': args.min_confidence,
                       'updated': result['predictions_updated'],
                       'verified_reset': result['verified_reset'],
                       'details': result['details']}, f, ensure_ascii=False, indent=1)
        print('[applied] 改动预测 %d 条，重置验证 %d 条，run_id=%s，报告：%s'
              % (result['predictions_updated'], result['verified_reset'], run_id, report))
        print('[hint] 回滚：python scripts/restore_prediction_batch.py --run-id %s' % run_id)

        if args.expect is not None and result['predictions_updated'] != args.expect:
            print('[FAIL] 实际改动 %d ≠ 预期 %d，请核对上面的对照表'
                  % (result['predictions_updated'], args.expect))
            return 3
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
