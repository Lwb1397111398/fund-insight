# 三桶真删执行摘要（2026-07-29）

## 轮次

| 轮 | 模式 | deleted_predictions | deleted_viewpoints | logs | unverifiable | summary | 合计 | guard 排除 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | dry-run 基线 | 443 | 57 | 0 | 0 | 0 | 500（cap） | 53 |
| 1 | **execute** | **443** | **57** | 0 | 0 | 0 | **500** | 53 未删 |
| 1b | dry-run | 0 | 82 | 0 | 0 | 0 | 82 | — |
| 2 | **execute** | 0 | **82** | 0 | 0 | 0 | **82** | — |
| 2b | dry-run | **0** | **0** | **0** | **0** | **0** | **0** | — |

## 合计实删

| 类别 | 条数 |
| --- | ---: |
| predictions（无结论软删） | 443 |
| viewpoints（软删满 30 天） | 139（57+82） |
| **总硬删** | **582** |
| verified 台账护栏保留 | **53**（9 对 / 44 错，仍 is_deleted） |

## 归零确认

最终 dry-run 全桶 **0**。未再执行。

## 库快照（删后）

| 表 | 指标 | 值 |
| --- | --- | ---: |
| predictions | total | 1694 |
| predictions | soft_deleted | 483 |
| predictions | soft_deleted + is_correct 非空 | 85 |
| viewpoints | total | 444 |
| viewpoints | soft_deleted | 400 |
| viewpoints | is_summary | 34 |

## 脏 status（P3，未回填）

`is_correct` 非空且 `status=pending`：**53**（全为软删台账行，存活 0）。  
写入侧已修；回填 SQL 见 `docs/P3_STATUS_IS_CORRECT_DIRTY_AUDIT.md`（**未执行**）。
