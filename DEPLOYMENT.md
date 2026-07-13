# Fund Insight 部署与运维指南

最后更新：2026-07-04

## 当前部署形态

Fund Insight 当前按三部分运行：

```text
Render Web Service
  -> FastAPI 应用
  -> Supabase PostgreSQL

Render Cron
  -> 每日任务：板块资金流、基金更新、预测验证
  -> Supabase PostgreSQL

GitHub Actions
  -> 交易日板块资金流抓取
  -> Supabase PostgreSQL
```

本地开发默认使用 SQLite，不需要 Supabase。

## 本地运行

```bash
pip install -r requirements.txt
copy .env.example .env
python -m src --init-db
python -m src --port 8002
```

访问：

```text
http://localhost:8002
```

## Render Web Service

配置文件：`render.yaml`

启动命令：

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port $PORT
```

关键环境变量：

| 变量 | 说明 |
| --- | --- |
| `PYTHON_VERSION` | Render 当前为 `3.10.12` |
| `APP_ENV` | `production` |
| `DATABASE_URL` | Supabase/PostgreSQL 连接串，secret |
| `ACCESS_PASSWORD` | API 访问密码，secret |
| `CORS_ORIGINS` | 生产域名，例如 `https://fund-insight.onrender.com` |
| `LLM_PROVIDER` | 当前生产使用 `volcengine` |
| `VOLCENGINE_API_KEY` | 火山引擎密钥，secret |
| `VOLCENGINE_BASE_URL` | 火山引擎 API 地址 |
| `VOLCENGINE_MODEL` | 主模型 |
| `VOLCENGINE_LIGHT_MODEL` | 轻量模型 |
| `DB_POOL_SIZE` | PostgreSQL 连接池大小 |
| `DB_MAX_OVERFLOW` | PostgreSQL 最大溢出连接 |
| `DB_POOL_RECYCLE` | 连接回收秒数 |
| `DB_POOL_TIMEOUT` | 连接池等待秒数 |

注意：

- `ENABLE_DATABASE_IMPORT` 默认必须保持 `false`。
- `ENABLE_TEST_DATA_CLEANUP` 默认必须保持 `false`；它按关键词硬删除数据，仅能在隔离维护环境短时开启。
- `ENABLE_DATA_CLEANUP` 默认必须保持 `false`；它控制过期数据、孤儿基金和定时批量清理。
- `ENABLE_STARTUP_MIGRATIONS` 默认必须保持 `false`，除非明确要补列/补索引。
- `CRAWLER_ENABLED` 在 Render Web 当前为 `true`，但爬虫仍应由用户或任务触发。

## Render Cron

`render.yaml` 中定义了 `fund-insight-scheduler`。

计划：

```text
30 10 * * *
```

命令：

```bash
python scripts/run_scheduled_tasks.py daily
```

执行内容：

1. `init_db()`。
2. `_run_sector_flow(trigger="render_cron")`。
3. `_run_fund_update()`。
4. `_run_prediction_verify()`。
5. `_run_expired_verify()`。

失败时命令返回非 0，Render 会显示 Cron 失败。

## GitHub Actions

主要工作流：

| 文件 | 用途 |
| --- | --- |
| `.github/workflows/sector_flow_crawler.yml` | 交易日 13:30 北京时间抓取板块资金流 |
| `.github/workflows/discover_akshare.yml` | 探测 akshare 接口 |
| `.github/workflows/test_akshare.yml` | 测试 akshare 接口 |
| `.github/workflows/test_direct_api.yml` | 测试直接 API |
| `.github/workflows/test_sector_types.yml` | 测试板块类型 |

`sector_flow_crawler.yml` 使用：

```bash
python scripts/fetch_sector_flow.py
```

需要 GitHub Secret：

- `DATABASE_URL`

## 数据库

本地：

- SQLite 文件：`data/fund_insight.db`
- 未设置 `DATABASE_URL` 时自动使用。

生产：

- PostgreSQL/Supabase。
- 连接池设置见 `src/models/database.py` 的 `_get_postgres_pool_settings()`。
- SQLAlchemy 会在启动时 `Base.metadata.create_all(engine)`，但这不是完整迁移系统。

重要限制：

- 项目没有 Alembic。
- 修改 `src/models/database.py` 后，必须评估 Supabase 生产表结构是否需要手动迁移。
- 数据库导入接口 `/api/import-database` 默认关闭；开启后还需要确认头 `X-Danger-Confirm: import-production-database`。

## 配置持久化

LLM 配置来源优先级：

1. 环境变量。
2. PostgreSQL `system_config` 表。
3. 本地 `data/llm_config.json`。

相关代码：

- `src/core/config.py`
- `src/api/routes/config.py`

## 健康检查

```text
GET /api/health
GET /api/health/detail
```

健康检查不需要访问密码。

`/api/health/detail` 会返回：

- 数据库状态。
- 当前数据库类型。
- LLM 是否配置。
- 爬虫是否启用。
- 启动迁移是否启用。
- 本地调度器是否运行。

## 常见运维命令

```bash
# 本地初始化数据库
python -m src --init-db

# 本地模拟每日任务
python scripts/run_scheduled_tasks.py daily

# 手动抓取板块资金流
python scripts/fetch_sector_flow.py

# 检查最近预测
python scripts/check_today_predictions.py

# 重新验证预测
python scripts/reverify_predictions.py

# 重新计算博主分数
python scripts/recalculate_blogger_scores.py
```

维护脚本很多，运行前先读脚本顶部逻辑，特别是会写库的脚本。

## 安全注意事项

- 不要把 `.env`、API key、`DATABASE_URL`、访问密码提交到仓库。
- `/api/import-database` 是高风险接口，默认关闭。
- 清理接口可能删除数据，前端已有预览和确认逻辑，后端也需要确认头。
- 生产数据库结构变更前必须备份或确认迁移 SQL。
- 爬虫遵守频率控制，不做高频采集。

## 故障排查

### 前端提示 Unauthorized

检查登录密码是否等于 `ACCESS_PASSWORD`，请求头是否带 `X-Access-Password`。

### LLM 分析失败

检查：

- `LLM_PROVIDER`
- `LLM_API_KEY` 或 `VOLCENGINE_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `/api/config/test-llm`

### 生产数据库连接失败

检查：

- `DATABASE_URL` 是否是 `postgresql://...`
- Supabase 是否允许连接。
- Render secret 是否配置。
- 连接池参数是否过大。

### 板块资金流没有更新

检查：

- Render Cron 运行记录。
- GitHub Actions `Sector Flow Crawler` 运行记录。
- `sector_flow_fetch_runs` 表。
- `/api/sector-flow/fetch-status`。

### 预测没有验证

检查：

- 预测 `target_date` 是否到期。
- 基金是否有 `fund_history` 起点和终点附近净值。
- `/api/predictions/verify-all/status`。
- Render Cron 日志。

## 发布前检查

```bash
pytest tests/unit/test_deployment_optimization.py tests/unit/test_production_hardening.py -v
python -m src --init-db
codegraph sync .
codegraph status .
```

如果改了业务逻辑，还要跑对应模块测试。
