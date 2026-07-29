# 保留策略 Step 0 只读盘点

- 盘点日：2026-07-29
- 范围：只读查询 + 代码引用清单
- 本文件只列数字与事实，不含建议

## 0. 数据源说明

| 项 | 值 |
| --- | --- |
| 应用当前 `DATABASE_URL` 方言 | postgresql |
| 主机 | aws-1-ap-south-1.pooler.supabase.com（Supabase pooler） |
| 库名 | postgres |
| 服务端版本 | PostgreSQL 17.6 |
| `pg_database_size` | 23,833,747 bytes（22.73 MB） |
| `autovacuum` | on |
| 本地 `data/fund_insight.db` | 存在，但是**另一份** SQLite 快照，**不是**应用当前读写库 |

本地 SQLite 对照（非生产主库，仅作旁证）：

| 项 | 值 |
| --- | --- |
| 路径 | `data/fund_insight.db` |
| 文件大小 | 3,567,616 bytes |
| mtime | 2026-07-28 13:41 |
| page_count | 871 |
| page_size | 4096 |
| freelist_count | 489 |
| auto_vacuum | 0（NONE） |
| journal_mode | wal |
| predictions 行数 | 473（与生产 2137 不一致） |

下文 **§1–§5 全部以生产 PostgreSQL 为准**。

---

## 1. 全表行数与体积

### 1.1 精确行数（`COUNT(*)`，按行数降序，非零表）

| 表 | 行数 |
| --- | ---: |
| fund_history | 3851 |
| cleanup_item_logs | 2601 |
| predictions | 2137 |
| viewpoints | 583 |
| posts | 458 |
| crawler_article_records | 407 |
| prediction_change_logs | 109 |
| fund_info | 106 |
| sector_fund_mapping | 77 |
| batch_analysis_tasks | 64 |
| analysis_logs | 43 |
| bloggers | 24 |
| investment_advice | 19 |
| cleanup_logs | 1 |
| cleanup_tasks | 1 |
| 其余公开表 | 0 |

零行表（生产）：`advice_feedback`、`advice_performance`、`advice_reasoning`、`cleanup_rules`、`cleanup_schedules`、`fund_holdings`、`fund_sync_retry`、`market_data`、`market_events`、`policy_data`、`prediction_groups`、`sector_alias`、`sector_rotation_reviews`、`sector_trade_logs`、`sentiment_data`、`sync_logs`、`system_config`、`user_fund_bindings`、`user_profiles`、`verification_tasks`。

非零表合计行数：10,481。

### 1.2 关系体积（`pg_total_relation_size`，含索引；字节）

| 表 | est_live_rows | total_bytes | table_bytes | index_bytes |
| --- | ---: | ---: | ---: | ---: |
| viewpoints | 583 | 3,481,600 | 1,056,768 | 106,496 |
| posts | 458 | 2,408,448 | 794,624 | 106,496 |
| fund_history | 3851 | 1,687,552 | 663,552 | 991,232 |
| predictions | 2137 | 1,482,752 | 1,015,808 | 425,984 |
| cleanup_item_logs | 2601 | 524,288 | 409,600 | 73,728 |
| crawler_article_records | 407 | 360,448 | 229,376 | 98,304 |
| prediction_change_logs | 109 | 327,680 | 229,376 | 65,536 |
| batch_analysis_tasks | 64 | 278,528 | 106,496 | 16,384 |
| investment_advice | 19 | 262,144 | 81,920 | 16,384 |
| analysis_logs | 43 | 139,264 | 73,728 | 16,384 |
| fund_info | 106 | 114,688 | 40,960 | 32,768 |
| bloggers | 24 | 106,496 | 16,384 | 49,152 |
| sector_fund_mapping | 77 | 65,536 | 8,192 | 32,768 |

体积排序与行数排序不一致：`viewpoints` / `posts` 行数少于 `fund_history`，但 total_bytes 更大（大文本/JSON 列）。

### 1.3 时间极值（有数据的主表）

| 表 | 时间列 | min | max |
| --- | --- | --- | --- |
| predictions | created_at | 2026-06-07 | 2026-07-29 |
| predictions | prediction_date | 2026-06-02 | 2026-07-29 |
| predictions | target_date | 2026-06-04 | 2027-01-25 |
| predictions | verified_at | null | null |
| fund_history | nav_date | 2026-05-08 | 2026-07-28 |
| fund_history | created_at | 2026-06-07 | 2026-07-29 |
| posts | created_at | 2026-06-07 | 2026-07-29 |
| viewpoints | created_at | 2026-06-11 | 2026-07-29 |
| bloggers | created_at | 2026-06-07 | 2026-07-23 |
| investment_advice | created_at / advice_date | 2026-06-29 | 2026-07-29 |
| crawler_article_records | created_at | 2026-07-22 | 2026-07-29 |
| prediction_change_logs | created_at | 2026-07-29（同日批量） | 2026-07-29 |
| cleanup_item_logs | created_at | 2026-07-29 07:39 | 2026-07-29 07:49 |
| cleanup_logs | created_at | 2026-07-29 07:39 | 2026-07-29 07:39 |
| analysis_logs | created_at | 2026-07-22 | 2026-07-29 |
| batch_analysis_tasks | created_at | 2026-06-29 | 2026-07-29 |

库龄观察窗口（生产主业务表）：约 **2026-06-07 → 2026-07-29**（~7.5 周）。`predictions.verified_at` 全为 null（结论存在于 `is_correct`，未写 verified_at）。

---

## 2. 逐月增长率

| 序列 | 2026-05 | 2026-06 | 2026-07 | 形态（描述性） |
| --- | ---: | ---: | ---: | --- |
| predictions.created_at | — | 1021 | 1116 | 近线性（两整月） |
| predictions.prediction_date | — | 1021 | 1116 | 同上 |
| fund_history.nav_date | 297 | 1402 | 2152 | 加速（随基金覆盖/天数） |
| fund_history.created_at | — | 1439 | 2412 | 加速 |
| posts.created_at | — | 244 | 214 | 近线性略降 |
| viewpoints.created_at | — | 217 | 366 | 上升 |
| bloggers.created_at | — | 21 | 3 | 早期一次导入后趋平 |
| investment_advice.created_at | — | 2 | 17 | 小基数上升 |
| crawler_article_records.created_at | — | — | 407 | 7 月起量 |
| prediction_change_logs.created_at | — | — | 109 | 7-29 单日 |
| analysis_logs.created_at | — | — | 43 | 7 月起量 |
| cleanup_item_logs | — | — | 2601 | 7-29 单次清理日志爆发 |

`predictions.target_date` 分布（含未来目标，非增长率）：

| 月 | 行数 |
| --- | ---: |
| 2026-06 | 458 |
| 2026-07 | 994 |
| 2026-08 | 233 |
| 2026-09 | 120 |
| 2026-10 | 56 |
| 2026-11 | 6 |
| 2026-12 | 160 |
| 2027-01 | 110 |

---

## 3. predictions 生命周期分布

### 3.1 七桶（`prediction_lifecycle.classify`，as_of=2026-07-29）

| 生命周期 | 计数 | 占 2137 |
| --- | ---: | ---: |
| deleted | 926 | 43.3% |
| incomplete | 0 | 0% |
| active | 558 | 26.1% |
| due_unverified | 213 | 10.0% |
| unverifiable | 220 | 10.3% |
| verified_correct | 134 | 6.3% |
| verified_incorrect | 86 | 4.0% |
| **合计** | **2137** | **100%** |

已有验证结论（correct+incorrect）：220。  
软删：926。  
仍可能参与方向/验证路径的未删未结论集合：active + due_unverified = 771。  
可清理死亡数据体量上限相关：deleted 926 + unverifiable 220 = 1146（是否可删由策略决定，此处只计量）。

### 3.2 原始列交叉（事实对照，非 lifecycle）

| 维度 | 分布 |
| --- | --- |
| is_deleted | false 1211 / true 926 |
| is_expired | false 1885 / true 252 |
| is_correct | true 167 / false 138 / null 1832 |
| status | pending 1885 / success 158 / failed 94 |

`is_correct` 非空 × `is_expired`：

| has_conclusion | is_expired | n |
| --- | --- | ---: |
| false | false | 1832 |
| true | false | 53 |
| true | true | 252 |

`status` × has_conclusion：

| status | has_conclusion | n |
| --- | --- | ---: |
| failed | true | 94 |
| pending | false | 1832 |
| pending | true | 53 |
| success | true | 158 |

`is_deleted` × has_conclusion：

| is_deleted | has_conclusion | n |
| --- | --- | ---: |
| false | false | 991 |
| false | true | 220 |
| true | false | 841 |
| true | true | 85 |

列不一致样本：`status=pending` 且 `is_correct` 非空 = 53；`is_correct` 非空且 `is_expired=false` = 53。

### 3.3 unverifiable 年龄直方图（`as_of - target_date`，天）

| 桶 | 计数 |
| --- | ---: |
| 0–29 | 126 |
| 30–89 | 94 |
| 90+ | 0 |

| 统计 | 值 |
| --- | ---: |
| count | 220 |
| min | 11 |
| max | 55 |
| median | 27 |
| p90 | 49 |

### 3.4 verified_* 目标日年龄（对照）

| 桶 | 计数 |
| --- | ---: |
| 0–29 | 202 |
| 30–89 | 18 |

| 统计 | 值 |
| --- | ---: |
| count | 220 |
| min | 6 |
| max | 30 |
| median | 20 |
| p90 | 29 |

---

## 4. 旧数据读路径依赖（删了谁会发现）

### 4.1 `predictions` 行

| 消费者 | 路径/符号 | 读什么 |
| --- | --- | --- |
| 列表/筛选 API | `src/api/routes/predictions.py`、`prediction_query_service` | 全量/分页/状态筛选；含历史 verified |
| 验证批处理 | `prediction_verify_service`、`prediction_verify_task`、`scheduler._run_prediction_verify` | due 队列；lifecycle `filter_due_for_verify` |
| 建议证据 | `advice_evidence.AdviceEvidenceBuilder`、`advice_service.get_data_for_advice` | 当前方向信号 + 博主权重输入 |
| 三阶段 LLM | `llm_analyzer.generate_investment_advice_three_stage` | 入参 predictions 列表 |
| 统计 | `stats_service`、`prediction_service.get_stats` / `get_verify_progress` | 准确率分母/分子 |
| 博主重算 | `utils/blogger_stats.recalculate_blogger_stats` | `verify_count>0` 且未删预测 |
| 维护/合并 | `prediction_maintenance_service`、`prediction_groups` | 重置/合并历史 |
| 变更日志 | `prediction_change_log_service` | 写读变更 |
| 清理预览/执行 | `retention_cleanup_service` | 候选与保护集（pending/长期目标等） |
| 导出 | `data_portability_service` | JSON 备份含 predictions |
| 前端 | `web/*prediction*`、`index.html` | 列表/验证/统计展示 |

### 4.2 超龄 / 已删 predictions

| 事实 | 说明 |
| --- | --- |
| 软删 926 行仍占表 | 多数读路径带 `is_deleted == False`；lifecycle `deleted` 桶直接分类 |
| `blogger_stats` | 文档写明累计分在删除后靠 `archived_*`；重算只扫未删且 `verify_count>0` |
| 清理服务 | 已有 `RetentionCleanupService` 候选逻辑与保护计数（pending / long_term 等） |
| `verified_at` 全 null | 任何按 `verified_at` 时间窗的读目前为空集 |

### 4.3 `fund_history`

| 消费者 | 用途 |
| --- | --- |
| `prediction_verify_service` | 起止净值、过程指标、as-of 边界 |
| `fund_api` / `fund_sync_manager` / `fund_service` | 同步与展示 |
| `retention_cleanup_service` | fund_history 候选与 long_term 保护 |
| API `routes/funds.py` | 历史序列 |

### 4.4 `viewpoints` / `posts` / `crawler_article_records`

| 表 | 主要读方 |
| --- | --- |
| viewpoints | advice 证据、观点 API、workflow 汇总、清理候选 |
| posts | 预测外键、帖子分析、清理（依赖预测候选） |
| crawler_article_records | 爬虫去重、清理（adopted 窗口策略代码已存在） |

### 4.5 日志类

| 表 | 行数 | 读方 |
| --- | ---: | --- |
| cleanup_item_logs | 2601 | 清理审计 API/服务 |
| prediction_change_logs | 109 | 变更审计、portability 导出 |
| analysis_logs | 43 | 分析日志查询 |
| cleanup_logs / cleanup_tasks | 1 / 1 | 清理任务状态 |

### 4.6 `investment_advice`

| 事实 | 值 |
| --- | --- |
| 行数 | 19 |
| 读方 | `advice` GET latest/history；缓存键比对 `data_hash` |
| advice_reasoning | 0 行 |

---

## 5. 统计口径依赖（准确率分母相关）

| 位置 | 分母/口径（代码事实） | 是否扫 predictions 行 |
| --- | --- | --- |
| `stats_service.get_overall_stats` | `is_correct IS NOT NULL` | 是 |
| `stats_service.get_prediction_stats` | `is_correct IS NOT NULL`（`expired` 兼容字段同义） | 是 |
| `prediction_service.get_stats` | `is_correct IS NOT NULL` | 是 |
| `prediction_service.get_verify_progress` | `is_correct IS NOT NULL`；`expired` 兼容 = verified | 是 |
| `prediction_service` 失败/异常/历史回溯列表 | 过滤改为 `is_correct` 结论，不再用 `is_expired` 列当结论 | 是 |
| `utils/blogger_stats.recalculate_blogger_stats` | **`verify_count > 0`** + `is_deleted==False` + 类型≠flat；叠加 `Blogger.archived_*` | 是 |
| `Blogger.accuracy_rate` / `total_predictions` / `correct_predictions` | 物化列，由重算/增量更新维护 | 间接 |
| `advice_evidence` 选博主 | 读 **`Blogger.accuracy_rate`**（不直接重算 predictions） | 否（用物化） |
| `prediction_query_service` 聚合 | verified 计数仍含 **`status in (success,failed,verified)`** 分支；correct/wrong 用 `is_correct` | 是 |
| lifecycle `verified_*` | **仅** `is_correct` | 分类时 |

盘点时点补充：生产 `is_correct` 非空 305 行（167+138），其中未删 220、已删 85；与 lifecycle verified 两桶合计 220（未删结论）一致。

---

## 6. 运维面：体积、回收、备份

### 6.1 生产 PostgreSQL

| 项 | 事实 |
| --- | --- |
| 库体积 | 22.73 MB |
| autovacuum | on |
| 版本 | 17.6 |
| 托管 | Supabase（pooler 主机名） |

### 6.2 本地 SQLite 旁路库

| 项 | 事实 |
| --- | --- |
| 体积 | ~3.4 MB |
| freelist_count | 489 / 871 pages（约 56% 空闲页） |
| auto_vacuum | NONE |
| 与生产行数 | 不一致（见 §0） |

### 6.3 备份机制现状（代码与仓库内文件）

| 项 | 事实 |
| --- | --- |
| `scripts/backup_database.py` | 仅支持**指定路径的 SQLite** `sqlite3.backup` + manifest；**不读 `DATABASE_URL`** |
| `RetentionCleanupService._create_backup` | `dialect != sqlite` 时 **直接返回 `None`**（生产 Postgres 清理前备份函数为空操作） |
| `DEPLOYMENT.md` | 文档要求生产变更前人工 `pg_dump`；给出示例命令 |
| `render.yaml` Cron | `30 10 * * *` → `run_scheduled_tasks.py daily` |
| daily 任务内容 | `fund_update`、`prediction_verify`；观点汇总默认关；**无 backup 步骤** |
| 本地调度 `TaskScheduler._run_cleanup` | 受 `destructive_cleanup_enabled()` 门闩；非 backup |
| 仓库内旧 SQLite 文件 | `data/fund_insight_backup_20260307_*.db`（2026-03，与当前生产无关） |
| `data/backups/` 目录 | 存在 |
| 自动化 Postgres 定时备份（本仓库配置） | **未发现** cron/脚本/Render job 调用 `pg_dump` 或等价物 |
| 平台侧 Supabase 自动备份 | 本仓库**无法从代码证实或证伪**；代码路径无集成 |

### 6.4 已存在的清理相关代码（状态事实，非建议）

| 项 | 事实 |
| --- | --- |
| `retention_cleanup_service.POLICY_VERSION` | `retention-v2` |
| 默认策略字段 | retention_days=30，weekly_history_until_days=90，adopted_crawler_days=180，cleanup_audit_days=365，batch_size=200 |
| 生产 cleanup_item_logs | 2601 行，时间戳集中在 2026-07-29 07:39–07:49 |
| 生产 cleanup_logs / cleanup_tasks | 各 1 行，同日 |

---

## 7. 盘点方法与可复现性

- 生产计数/体积/月度：只读 SQL via 应用 engine（`SELECT` / `pg_*`）。
- 生命周期：ORM 加载全部 `Prediction` + `classify(as_of=current_as_of())`。
- 读路径/统计口径：仓库内 `src/**/*.py` 静态检索。
- 备份/调度：`scripts/`、`render.yaml`、`src/tasks/`、`DEPLOYMENT.md`、`retention_cleanup_service._create_backup` 静态检索。
- 未修改业务代码；未执行 DELETE/VACUUM/迁移。

---

## 8. 关键数字摘要（便于 Step 1 输入）

| 指标 | 值 |
| --- | ---: |
| 生产库体积 | 22.73 MB |
| 行数第一 | fund_history 3851 |
| 行数第二 | cleanup_item_logs 2601 |
| 行数第三 | predictions 2137 |
| 体积第一 | viewpoints 3.48 MB |
| predictions 软删 | 926 |
| predictions unverifiable | 220（年龄 11–55 天） |
| predictions 已验证结论（未删） | 220 |
| predictions 仍 active | 558 |
| predictions due_unverified | 213 |
| 本仓库自动化 pg 备份作业 | 0 |
| 清理执行器在 Postgres 上的代码内 backup | 返回 None |
