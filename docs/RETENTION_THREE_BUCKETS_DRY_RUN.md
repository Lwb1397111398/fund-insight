# 三桶保留 dry-run 将删清单（v2）

- 生成时间：2026-07-29T21:35:07
- as_of：2026-07-29
- 模式：**dry-run（未删除）**
- 策略：three-buckets-v2
- 全局上限截断：True

## 496 护栏结论（执行前必读）

| 项 | 数量 |
| --- | ---: |
| 软删且 deleted_at 满 30 天 | 496 |
| 其中 **is_correct 非空（台账，永不删）** | **53**（对 9 / 错 44） |
| 护栏后可删 | **443** |

## 本轮 dry-run 计数（受 max_total_per_run=500 截断）

| 桶 | 将删 | 说明 |
| --- | ---: | --- |
| deleted_predictions | 443 | 已排除 verified 台账 |
| cleanup_item_logs | 0 | 未满 90 天 |
| unverifiable_predictions | 0 | 未满 90 天 |
| deleted_viewpoints | 57 | 软删观点 deleted_at+30d |
| summary_viewpoints | 0 | viewpoint_date 默认 90d；当前 0 |
| **合计** | **500** | 触达全局上限则截断 |

protected_counts：{'verified_ledger_excluded': 53}

完整 id：`RETENTION_THREE_BUCKETS_DRY_RUN.json`

## 读路径（汇总）

前端 `web/index.html` / `viewpoint-manager.js` 支持按「每日汇总」筛选并列表展示 `is_summary` 历史行；API `GET /api/viewpoints?viewpoint_type=summary`。故汇总窗口默认 **90 天**（参数 `summary_viewpoint_days`），非 30。

## 旧执行器

`RetentionCleanupService.execute` / API POST cleanup / 定时 `run_cleanup_task`：**硬删已下线**；`build_plan` preview 仍只读可用。

## 真删（需你明确确认）

```bash
PYTHONPATH=. python scripts/run_three_bucket_retention.py --execute --confirm three-buckets-hard-delete
```

**当前未执行真删。**
