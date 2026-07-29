# 三桶保留 dry-run 将删清单

- 生成时间：2026-07-29T21:09:24
- as_of：2026-07-29
- 模式：**dry-run（未删除）**
- 策略：three-buckets-v1 {'deleted_hard_delete_days': 30, 'cleanup_item_log_days': 90, 'unverifiable_days': 90, 'batch_size': 200, 'max_per_bucket': 500}

## 计数

| 桶 | 将删条数 | 说明 |
| --- | ---: | --- |
| deleted_predictions | 496 | 软删且 deleted_at 满 30 天；本批 cap=500，实际 496 |
| cleanup_item_logs | 0 | 满 90 天；当前日志都是今日写入 → 0 |
| unverifiable_predictions | 0 | 满 90 天；当前最大约 55 天 → 0 |
| **合计** | **496** | |

完整 id 列表见同目录 `RETENTION_THREE_BUCKETS_DRY_RUN.json`。

## 真删（需你明确确认后）

```bash
PYTHONPATH=. python scripts/run_three_bucket_retention.py --execute --confirm three-buckets-hard-delete
```

**当前未执行真删。**
