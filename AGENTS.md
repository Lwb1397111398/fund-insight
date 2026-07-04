# AGENTS.md

This file provides guidance to Codex when working in this repository.

## 项目一句话

Fund Insight 是一个基金博主观点分析系统：用户录入或抓取基金相关帖子/文章，系统用 LLM 提取预测和观点，再用基金净值、板块资金流和后台任务验证预测表现，最终辅助判断哪些博主和板块观点更可靠。

## 当前技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python + FastAPI + SQLAlchemy 2.0 + Pydantic 2.x |
| 前端 | `web/` 原生 HTML/CSS/JS，Vue 3 CDN，axios，本项目没有前端构建链 |
| 数据库 | 本地 SQLite `data/fund_insight.db`；生产 PostgreSQL/Supabase，通过 `DATABASE_URL` 切换 |
| LLM | OpenAI 兼容 SDK，支持硅基流动、DeepSeek、火山引擎 |
| 基金/市场数据 | 天天基金、东方财富、akshare，另有多源基金 API 兜底 |
| 部署 | Render Web Service + Render Cron + Supabase；GitHub Actions 运行板块资金流抓取 |
| 测试 | pytest |
| 代码索引 | `.codegraph/` 是本地索引产物，改代码/文档后运行 `codegraph sync .` |

## 常用命令

```bash
# 启动本地服务，默认 8002
python -m src
python -m src --port 8002

# 仅初始化数据库
python -m src --init-db

# 直接通过 uvicorn 启动
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8002

# 运行测试
pytest tests/ -v
pytest tests/unit/ -v
pytest tests/integration/ -v

# 计划中的关键验证
python -m src --init-db
pytest tests/unit/test_prediction_verify_batch_task.py tests/unit/test_sector_flow_service.py tests/unit/test_scheduler_fixes.py -v

# CodeGraph
codegraph status .
codegraph sync .
```

## 环境变量

复制 `.env.example` 为 `.env`。本地最少需要：

- `LLM_API_KEY`：LLM 密钥。若使用火山引擎，则配置 `VOLCENGINE_API_KEY` 并设置 `LLM_PROVIDER=volcengine`。
- `LLM_BASE_URL` / `LLM_MODEL`：OpenAI 兼容模型地址和模型名。
- `ACCESS_PASSWORD`：所有 `/api/` 请求的访问密码；前端通过请求头 `X-Access-Password` 访问。

生产常见配置：

- `DATABASE_URL`：PostgreSQL/Supabase 连接串。不设置时自动使用 SQLite。
- `CORS_ORIGINS`：生产域名白名单。
- `CRAWLER_ENABLED`：Render 当前设为 `true`，本地默认可保持 `false`。
- `ENABLE_DATABASE_IMPORT=false`：数据库导入接口默认禁用，启用还必须带确认头。
- `ENABLE_STARTUP_MIGRATIONS=false`：启动补列/索引默认禁用，避免生产启动时隐式改结构。

## 系统分层

```text
web/*.html + web/common.* 
        |
        v
src/api/main.py  ->  src/api/routes/*  ->  src/api/schemas/*
        |
        v
src/services/*  业务服务、事务、批处理、统计、验证
        |
        +--> src/analyzer/*  LLM/本地趋势/帖子价值分析
        +--> src/fund/*      基金数据、多源 API、同步、技术指标
        +--> src/crawler/*   文章/帖子/板块资金流抓取与筛选
        |
        v
src/models/database.py  SQLAlchemy ORM，SQLite/PostgreSQL 共用
```

后台任务入口：

- `src/tasks/scheduler.py`：本地常驻调度器，按北京时间窗口运行清理、基金更新、预测验证、板块资金流抓取。
- `scripts/run_scheduled_tasks.py`：Render Cron 一次性入口，执行 `daily`。
- `scripts/fetch_sector_flow.py`：GitHub Actions/手动抢筹数据抓取入口。

## 核心业务流

1. 用户添加博主和帖子：`POST /api/bloggers`，`POST /api/posts`。
2. LLM 分析帖子：`PostService` 调用 `src/analyzer/llm_analyzer.py`，生成标题、预测方向、基金/板块、预测周期和目标日期。
3. 预测入库：`Prediction` 记录关联 `Blogger`、`Post`、基金代码和目标验证日期。
4. 基金数据同步：`src/fund/fund_api.py`、`FundDataManager`、`FundSyncManager` 拉取净值和历史。
5. 预测验证：`src/services/prediction_verify_service.py` 根据起点/终点净值、过程涨跌、震荡阈值和预测方向打分。
6. 博主统计：`blogger_stats`、`BloggerService`、`StatsService` 统计准确率、等级、预测数量。

观点和建议流：

1. 爬虫或人工录入观点，保存到 `Viewpoint`。
2. `ViewpointService` 支持批量分析、汇总观点、权重和有效期。
3. `AdviceService` 与 `LLMAnalyzer.generate_investment_advice_three_stage()` 生成投资建议。

板块资金流流：

1. `src/crawler/sector_flow_crawler.py` 抓东方财富板块资金数据。
2. `src/services/sector_flow_service.py` 计算暗盘、主力强度、行为标签，幂等写入 `sector_fund_flow`。
3. 抓取运行日志写入 `sector_flow_fetch_runs`。
4. API：`/api/sector-flow/fetch`、`/ranking`、`/history`、`/fund-link`、`/stats`、`/fetch-status`。

## 重点文件

| 文件 | 说明 |
| --- | --- |
| `src/api/main.py` | FastAPI 入口、中间件、路由注册、静态页面、危险的数据库导入接口 |
| `src/api/routes/` | 按领域拆分的 REST API |
| `src/models/database.py` | 30+ ORM 表模型，兼容 SQLite/PostgreSQL |
| `src/services/prediction_verify_service.py` | 预测验证核心，涉及准确率和评分，改动需谨慎 |
| `src/services/sector_flow_service.py` | 板块资金流统一服务，负责抓取、计算、幂等写入和状态 |
| `src/services/prediction_verify_task.py` | 批量验证后台状态对象，避免长请求卡死前端 |
| `src/analyzer/llm_analyzer.py` | LLM 核心，包含模型选择、熔断、缓存、解析兜底、预测/建议/图像分析 |
| `src/fund/fund_api.py` | 天天基金与基金数据管理 |
| `src/tasks/scheduler.py` | 本地后台调度 |
| `web/index.html` | 主前端 SPA，大文件，修改前先定位相关 section |
| `render.yaml` | Render Web/Cron 部署配置，通常不要顺手改 |

## API 路由总览

`src/api/main.py` 注册以下路由：

- `/api/bloggers`：博主管理和重算统计。
- `/api/posts`：帖子录入、分析、批量分析、低质量清理。
- `/api/predictions`：预测 CRUD、单条/批量验证、过期验证、统计、导出、合并相似预测。
- `/api/funds`：基金列表、详情、同步、历史、趋势状态。
- `/api/viewpoints`：观点列表、批量分析、汇总、清理。
- `/api/crawler` 和 `/api/crawler/eastmoney-blog`：爬虫状态、抓取、采纳。
- `/api/advice`：投资建议生成、历史、统计。
- `/api/stats`：总体统计。
- `/api/config`：LLM 配置、清理、别名、板块-基金映射、配置导入导出。
- `/api/test-data`：测试数据扫描和清理。
- `/api/batch-analysis`：批量分析任务。
- `/api/sector-flow`：板块资金流抓取、排行、历史、状态。
- `/api/prediction-groups`：相似预测组。

## 数据模型心智图

核心表：

- `bloggers`、`posts`、`predictions`：博主、帖子、预测主链路。
- `fund_info`、`fund_history`：基金基础信息和历史净值。
- `viewpoints`、`crawler_article_records`：观点和爬虫去重记录。
- `investment_advice`、`advice_reasoning`、`advice_performance`、`advice_feedback`：投资建议链路。
- `sector_fund_mapping`、`sector_alias`、`user_fund_bindings`：板块与基金映射。
- `sector_fund_flow`、`sector_flow_fetch_runs`：抢筹/板块资金流和抓取运行日志。
- `batch_analysis_tasks`、`analysis_logs`：批量分析和日志。
- `cleanup_*`：清理任务、规则、日志。
- `market_data`、`policy_data`、`sentiment_data`、`market_events`：市场辅助数据。
- `system_config`：生产环境持久化配置。

## 前端约束

- 主界面是 `web/index.html`，使用 Vue 3 CDN 和 axios。
- 没有构建步骤，不要引入需要打包的新依赖。
- `web/common.js` 与 `web/common.css` 存放跨页面公共逻辑和样式。
- UI 风格应偏数据工具：清晰、克制、可扫描；不要做营销 landing page、大渐变、重动画。
- 所有 `/api/` 请求需要带 `X-Access-Password`。

## 部署和定时任务

- Render Web Service：`uvicorn src.api.main:app --host 0.0.0.0 --port $PORT`。
- Render Cron：每天 10:30 运行 `python scripts/run_scheduled_tasks.py daily`。
- GitHub Actions：`.github/workflows/sector_flow_crawler.yml` 在交易日 13:30 北京时间运行 `scripts/fetch_sector_flow.py`。
- Supabase/PostgreSQL：通过 `DATABASE_URL` 连接；连接池参数见 `render.yaml` 和 `src/models/database.py`。

## 修改规则

- 用户是编程小白，默认自动判断、修改、验证和收尾，不把技术选型抛给用户。
- 不要回滚用户已有改动；当前工作树可能已有未提交文件。
- 不要手改 `.codegraph/codegraph.db`；需要时运行 `codegraph sync .`。
- 数据库结构变更、删除/迁移大量文件、改部署配置、改公共接口、移除依赖等高风险操作必须先确认。
- `src/analyzer/llm_analyzer.py`、`src/models/database.py`、`src/services/prediction_verify_service.py`、`src/services/sector_flow_service.py`、`src/api/main.py`、`web/index.html` 是高风险区域，先读测试和调用方再动。
- 文档类改动也要跑最小验证或至少格式/链接/命令检查。

## 推荐工作流

1. 先看本文件、`ARCHITECTURE.md`、`PRODUCT.md`。
2. 查结构优先用 CodeGraph：`codegraph query`、`codegraph callers`、`codegraph context`；不可用时用 `rg`。
3. 修改前定位对应测试；能写测试就写测试，不能写则运行最小验证。
4. 改完运行相关 pytest，再运行 `python -m src --init-db` 做启动级数据库初始化检查。
5. 涉及索引或文档入口变化后运行 `codegraph sync .`。

## 当前测试基线

最近一次文档重写前的只读收集结果：

- `pytest --collect-only -q` 可收集 123 个测试。
- CodeGraph 状态显示 179 个索引文件、2910 个节点、3589 条边；当时有 3 个新增和 3 个修改待同步。

常用重点测试：

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_sector_flow_service.py -v
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_production_hardening.py -v
```
