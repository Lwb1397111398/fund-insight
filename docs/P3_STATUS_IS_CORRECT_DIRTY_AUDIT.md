# P3：status / is_correct 脏状态（只读盘点 + 回填 SQL，未执行）

盘点日：2026-07-29（三桶真删归零之后）

## 1. 现象

| 条件 | 生产计数 |
| --- | ---: |
| `is_correct IS NOT NULL AND status='pending'` | **53** |
| 其中 is_deleted=true | 53 |
| 其中 is_deleted=false | **0**（存活行已干净） |
| pending+correct | 9 |
| pending+incorrect | 44 |
| 全库有结论 | 305 |
| 有结论且 status∈{success,failed,verified} | 252 |

→ 脏行 **全部是软删台账行**（正是护栏保住的 53 条）。存活待验证队列已无此裂缝。

## 2. 写入侧根因

`prediction_verify_service` 旧逻辑：

- 每次验证都写 `is_correct`
- **仅当** `today >= target_date` 才把 `status` 改为 success/failed

因此目标日之前的验证会留下 `pending + is_correct≠null`。

## 3. 已做修复（代码，防新脏数据）

- `prediction_verify_service`：写 `is_correct` 的同一处同步 `status=success|failed`，并补 `verified_at`
- `prediction_service.verify`：手工验证改为 `success|failed`（不再写模糊的 `verified` 字符串）

## 4. 回填 SQL（**先统计，不执行**）

### 4.1 影响面（只读）

```sql
SELECT status, is_correct, is_deleted, COUNT(*) AS n
FROM predictions
WHERE is_correct IS NOT NULL
  AND status = 'pending'
GROUP BY 1, 2, 3
ORDER BY n DESC;

-- 预期：约 53 行，全 is_deleted=true
```

### 4.2 回填（需人工确认后再跑）

```sql
BEGIN;

UPDATE predictions
SET
  status = CASE
    WHEN is_correct IS TRUE THEN 'success'
    WHEN is_correct IS FALSE THEN 'failed'
    ELSE status
  END,
  verified_at = COALESCE(
    verified_at,
    CASE
      WHEN last_verify_date IS NOT NULL THEN last_verify_date::timestamp
      ELSE NOW()
    END
  )
WHERE is_correct IS NOT NULL
  AND status = 'pending';

-- 复核
SELECT status, is_correct, is_deleted, COUNT(*)
FROM predictions
WHERE is_correct IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 4 DESC;

-- 确认后 COMMIT; 有问题 ROLLBACK;
```

软删行也回填 status：不恢复删除，只修枚举裂缝，避免未来 `status='pending'` 扫描误伤台账。

## 5. 与清理线关系

- 三桶 dry-run 已归零；护栏继续排除这 53 条物理删除
- 回填只改 status/verified_at，**不删行、不改 is_correct**
- 回填确认后清理线可收口
