# 抢筹板块抓取模块完整优化设计

日期：2026-07-03

## 1. 背景

当前抢筹板块数据管线存在多入口、多口径、低可观测的问题：

- GitHub Actions 使用 `scripts/fetch_sector_flow.py` 独立抓取、计算并写库。
- Web 手动接口通过 `SectorFlowService.fetch_and_save()` 抓取，但当前参数名不匹配，手动触发可能失败。
- Render Cron 的日常任务不包含抢筹抓取。
- GitHub Actions 定时任务不保证准点，2026-07-02 的定时运行已触发但 `Run crawler` 与 `Retry on failure` 均失败。
- 失败时缺少结构化运行日志，只能依赖 Actions 日志；日志权限不足时无法定位具体错误。

本设计目标是把抢筹板块抓取整理成统一、稳定、可追踪、可清理的数据管线。

## 2. 目标

1. GitHub Actions、Render Cron、手动按钮都调用同一套服务逻辑。
2. Render Cron 与 GitHub Actions 采用“双主幂等”策略：两边都可以定时抓取，谁成功都能更新数据，重复执行不会产生重复记录。
3. 每次抓取都记录运行日志，包括触发来源、状态、开始/结束时间、抓取数量、保存数量、错误原因。
4. 抓取失败时不删除已有数据；前端继续展示最近可用数据，并明确标注数据日期和失败原因。
5. 历史抢筹数据与抓取运行日志纳入现有清理机制。
6. 行业板块与概念板块使用统一抓取、计算、保存口径。
7. 增加测试覆盖，包含计算规则、幂等保存、失败保护、调度调用、状态 API。
8. 实施后进行真实自测：设置一次短延迟定时抓取（例如 30 秒后执行），等待后检查运行日志和数据写入结果。

## 3. 非目标

1. 不重新设计整体前端风格。
2. 不更换数据源，仍以东方财富 push2 API 为主。
3. 不删除已有抢筹历史数据，除非通过清理机制按保留周期清理。
4. 不依赖 Render Web 进程内后台线程作为生产主调度。

## 4. 推荐架构

统一数据流：

```text
东方财富 API
   ↓
SectorFlowCrawler
   ↓
SectorFlowService.run_fetch(trigger=...)
   ↓
幂等保存 sector_fund_flow
   ↓
记录 sector_flow_fetch_runs
   ↓
API 查询与前端展示
```

模块职责：

| 模块 | 职责 |
|---|---|
| `src/crawler/sector_flow_crawler.py` | 只负责请求东方财富 API，返回板块资金流向原始/半结构化数据。 |
| `src/services/sector_flow_service.py` | 负责统一抓取流程、去重、单位转换、暗盘计算、主力强度计算、行为标签、幂等保存、运行日志。 |
| `scripts/fetch_sector_flow.py` | 作为 GitHub Actions/手动命令入口，只调用服务层，不再自写计算和 SQL。 |
| `scripts/run_scheduled_tasks.py` | Render Cron 入口，加入抢筹抓取调用。 |
| `src/api/routes/sector_flow.py` | 提供手动抓取、排行榜、历史、统计和抓取状态 API。 |
| `src/tasks/cleanup_tasks.py` | 清理过期抢筹数据与抓取运行日志。 |
| `web/index.html` | 展示最新数据日期、抓取状态、错误原因与手动刷新结果。 |

## 5. 统一抓取入口

新增服务方法：

```python
SectorFlowService.run_fetch(
    trigger: str,
    categories: list[str] | None = None,
) -> dict
```

默认抓取：

```text
categories = ["industry", "concept"]
```

返回结构示例：

```json
{
  "success": true,
  "status": "success",
  "trigger": "github_actions",
  "flow_date": "2026-07-03",
  "fetched_count": 200,
  "saved_count": 200,
  "run_id": 123,
  "error_message": null
}
```

失败时返回：

```json
{
  "success": false,
  "status": "failed",
  "trigger": "render_cron",
  "flow_date": "2026-07-03",
  "fetched_count": 0,
  "saved_count": 0,
  "run_id": 124,
  "error_message": "东方财富 API 连接中断"
}
```

## 6. 双主幂等调度

### 6.1 GitHub Actions

保留 `.github/workflows/sector_flow_crawler.yml`。

优化后仍按交易日定时触发，并支持 `workflow_dispatch` 手动补抓。工作流执行：

```bash
python scripts/fetch_sector_flow.py --trigger github_actions
```

脚本必须输出结构化日志：

- 触发来源
- 抓取分类
- 上游返回数量
- 保存数量
- run id
- 错误摘要

### 6.2 Render Cron

`render.yaml` 中的独立 Cron Job 继续执行：

```bash
python scripts/run_scheduled_tasks.py daily
```

`run_daily_tasks()` 增加抢筹抓取调用。Render Web 休眠不应影响独立 Cron Job；同时 GitHub Actions 仍作为并行兜底。

### 6.3 Web 内部 scheduler

FastAPI 进程里的后台 scheduler 不作为生产抢筹主调度。可以保留给本地开发或临时任务，但生产稳定性依赖 GitHub Actions + Render Cron 双主幂等。

## 7. 幂等保存策略

推荐唯一键：

```text
flow_date + sector_code + data_category
```

如果已有旧数据缺少 `sector_code`，服务层查询时兼容：

```text
flow_date + sector_name + data_category
```

保存规则：

1. 抓取成功且有数据：按唯一键 upsert。
2. 同一天重复抓取：更新已有记录，不重复插入。
3. 抓取失败：不删除已有数据，不覆盖已有数据，只写失败运行日志。
4. 部分分类失败：成功分类可保存，运行状态记录为 `partial`，错误信息记录失败分类。

## 8. 运行日志表

新增模型：`SectorFlowFetchRun`。

建议表名：

```text
sector_flow_fetch_runs
```

字段：

| 字段 | 说明 |
|---|---|
| `id` | 主键。 |
| `trigger` | `manual` / `github_actions` / `render_cron` / `test_timer`。 |
| `status` | `running` / `success` / `failed` / `partial`。 |
| `flow_date` | 数据日期。 |
| `categories` | 抓取分类，如 `industry,concept`。 |
| `started_at` | 开始时间。 |
| `finished_at` | 结束时间。 |
| `fetched_count` | 抓取到的记录数。 |
| `saved_count` | 保存成功的记录数。 |
| `error_message` | 错误摘要，成功时为空。 |
| `data_source` | 数据源，如 `eastmoney`。 |

运行日志用于：

- 前端展示最近一次抓取状态。
- 调试失败原因。
- 清理和审计。
- 区分 GitHub、Render、手动触发结果。

## 9. 数据清理机制

现有 `cleanup_old_sector_flow(keep_days=90)` 继续清理过期抢筹历史数据。

新增：

```python
cleanup_old_sector_flow_runs(keep_days: int = 180) -> dict
```

默认保留策略：

| 数据类型 | 默认保留 |
|---|---|
| `sector_fund_flow` 抢筹历史数据 | 90 天 |
| `sector_flow_fetch_runs` 抓取运行日志 | 180 天 |

统一纳入现有清理总任务：

```text
run_cleanup_task()
```

清理返回摘要增加：

```json
{
  "sector_flow": {"deleted": 10},
  "sector_flow_runs": {"deleted": 3}
}
```

前端待清理统计也应包含过期抢筹数据和过期抓取日志。

## 10. API 设计

保留并修正：

```text
POST /api/sector-flow/fetch
```

修正当前参数名不匹配问题，改为调用统一入口：

```python
service.run_fetch(trigger="manual")
```

新增：

```text
GET /api/sector-flow/fetch-status
```

返回示例：

```json
{
  "success": true,
  "data": {
    "latest_run": {
      "status": "failed",
      "trigger": "github_actions",
      "started_at": "2026-07-02T16:27:51+08:00",
      "finished_at": "2026-07-02T16:29:19+08:00",
      "fetched_count": 0,
      "saved_count": 0,
      "error_message": "东方财富 API 连接中断"
    },
    "latest_data_date": "2026-07-01",
    "today_data_count": 0,
    "displaying_stale_data": true
  }
}
```

排行榜接口可以在今天无数据时支持最近可用数据查询，并返回实际数据日期。

## 11. 前端展示

抢筹区域增加以下信息：

- 最新数据日期。
- 最近抓取状态。
- 最近触发来源。
- 失败时的简短错误原因。
- 如果展示非今日数据，显示明确提示。
- 手动刷新按钮展示 loading、成功保存数量或失败原因。

示例文案：

```text
当前展示：2026-07-01 数据
最近抓取：失败（GitHub Actions，2026-07-02 16:29）
原因：东方财富 API 连接中断
```

## 12. 错误处理

错误处理原则：

1. 上游 API 失败：重试后仍失败，写失败运行日志，不删除旧数据。
2. 单条数据解析失败：跳过该条，记录警告；如果其他记录成功，状态为 `partial`。
3. 数据库提交失败：回滚本次写入，运行日志记录失败。
4. GitHub/Render 重复触发：依赖幂等 upsert，不产生重复数据。
5. 手动触发失败：API 返回结构化错误，前端展示错误原因。

## 13. 测试计划

### 13.1 单元测试

覆盖：

- `calculate_dark_pool()`。
- `calculate_intensity()`。
- `judge_behavior()` 阈值：`grab` / `build` / `wash` / `sell`。
- 原始数据 enrich 后字段完整。

### 13.2 服务层测试

覆盖：

- 成功抓取后保存记录。
- 同一天重复抓取不会重复插入。
- 抓取失败不删除已有数据。
- 抓取失败写入失败运行日志。
- 部分分类失败时状态为 `partial`。

### 13.3 API 测试

覆盖：

- `POST /api/sector-flow/fetch` 不再因参数名错误失败。
- `GET /api/sector-flow/fetch-status` 返回最近运行状态。
- 今天无数据时，状态接口能返回最近可用数据日期。

### 13.4 调度测试

覆盖：

- `scripts/fetch_sector_flow.py --trigger github_actions` 调用统一服务入口。
- `run_daily_tasks()` 包含抢筹抓取。
- 抢筹抓取失败不应阻断其他日常任务的结果记录。

### 13.5 真实定时自测

实施完成后执行一次短延迟验证：

1. 创建或调用一个测试入口，安排 30 秒后执行抢筹抓取，触发来源标记为 `test_timer`。
2. 等待 30 秒以上。
3. 查询 `sector_flow_fetch_runs`，确认出现 `test_timer` 运行记录。
4. 如果上游 API 可用，确认 `saved_count > 0` 且 `sector_fund_flow` 有对应日期数据。
5. 如果上游 API 不可用，确认状态为 `failed`，错误原因被记录，并且旧数据未被删除。

该自测不作为生产长期调度，只用于验证“定时触发 → 抓取 → 记录日志 → 写库/失败保护”链路。

## 14. 实施阶段

### 阶段 1：止血修复

- 修正手动接口参数名错误。
- 保证抓取失败不删除已有数据。
- 增强 GitHub 脚本日志输出。

### 阶段 2：统一服务入口

- 新增 `run_fetch()`。
- GitHub 脚本改为调用服务层。
- Render daily 任务加入抢筹抓取。
- 保留旧方法作为兼容包装，避免一次性破坏调用方。

### 阶段 3：运行日志与状态 API

- 新增 `SectorFlowFetchRun` 模型。
- 每次抓取写运行日志。
- 新增 `/api/sector-flow/fetch-status`。

### 阶段 4：清理机制与前端展示

- 新增运行日志清理。
- 接入现有清理汇总。
- 前端展示数据日期、抓取状态和失败原因。

### 阶段 5：测试与真实自测

- 补齐单元测试、服务测试、API 测试、调度测试。
- 执行 30 秒短延迟真实自测。

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 东方财富 API 临时不可用 | 重试、失败日志、保留旧数据、双主调度。 |
| Render Web 休眠 | 不依赖 Web 内部后台线程，使用独立 Render Cron + GitHub Actions。 |
| GitHub Actions 不准点 | 与 Render Cron 双主幂等，任一成功即可。 |
| 重复抓取造成重复数据 | 使用唯一键或服务层 upsert。 |
| 运行日志过多 | 增加 180 天日志清理。 |
| 数据口径再次分裂 | GitHub、Render、手动统一调用 `SectorFlowService.run_fetch()`。 |

## 16. 验收标准

1. 手动抓取接口可正常返回结构化结果。
2. GitHub Actions 与 Render Cron 调用同一套服务逻辑。
3. 重复抓取同一天数据不会产生重复记录。
4. 抓取失败时旧数据不被删除。
5. 每次抓取都有 `sector_flow_fetch_runs` 记录。
6. 前端能显示最新数据日期、最近抓取状态和失败原因。
7. 过期抢筹数据和运行日志可被现有清理任务清理。
8. 测试通过。
9. 30 秒短延迟真实自测能证明定时触发链路可用，或在上游不可用时证明失败保护和日志记录可用。
