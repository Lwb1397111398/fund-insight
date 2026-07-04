# Fund Insight 架构文档

最后更新：2026-07-04

## 这个项目是什么

Fund Insight 是一个 Python 全栈 Web 应用，用于追踪基金博主、财经文章和板块资金流。系统把非结构化文本转成结构化预测和观点，再结合基金净值、历史行情、板块资金流和后台任务做验证、统计和建议生成。

核心目标不是自动交易，而是让用户能回答：

- 哪些博主预测更准？
- 某个基金或板块最近被谁看多/看空？
- 某条预测是否已经到了验证时间？
- 哪些板块出现主力抢筹、建仓、洗盘或卖出迹象？
- 当前投资建议引用了哪些观点和预测？

## 总体结构

```text
浏览器
  |
  |  HTML/JS/Vue CDN/axios
  v
web/index.html + web/*.html
  |
  |  HTTP + X-Access-Password
  v
src/api/main.py
  |
  +--> 中间件：CORS、访问密码、静态文件
  +--> 页面：/, /index.html, /cleanup-manager.html 等
  +--> 路由：src/api/routes/* + src/api/eastmoney_routes.py + prediction_groups.py
          |
          v
src/services/*
  |
  +--> analyzer/  LLM 分析、帖子价值判断、本地趋势
  +--> fund/      天天基金、多源基金 API、同步、技术指标
  +--> crawler/   文章/帖子/板块资金流抓取和过滤
  +--> tasks/     调度、清理、定时验证
          |
          v
src/models/database.py
  |
  +--> SQLite: data/fund_insight.db
  +--> PostgreSQL/Supabase: DATABASE_URL
```

## 代码分层

| 层 | 路径 | 职责 |
| --- | --- | --- |
| 前端层 | `web/` | 原生 HTML/JS SPA，Vue CDN，用户录入、列表、统计、清理、配置页面 |
| API 层 | `src/api/` | FastAPI 应用、路由注册、请求校验、响应封装、静态页面 |
| 服务层 | `src/services/` | 业务逻辑、事务边界、预测验证、基金同步、观点统计、板块资金流 |
| 分析层 | `src/analyzer/` | LLM 调用、模型选择、结果解析、帖子价值判断、本地趋势 |
| 数据采集层 | `src/crawler/`、`src/fund/` | 外部文章、天天基金、东方财富、akshare、多源基金数据 |
| 数据层 | `src/models/database.py` | ORM 模型、连接池、SQLite/PostgreSQL 兼容、建表 |
| 后台任务 | `src/tasks/`、`scripts/` | 本地调度、Render Cron、GitHub Actions 抓取、维护脚本 |

## API 架构

`src/api/main.py` 做这些事：

- 初始化 FastAPI `app` 和 lifespan。
- 启动时执行 `init_db()`，按配置加载 LLM 持久化配置。
- 注册访问密码中间件：所有 `/api/` 请求需要 `X-Access-Password`，健康检查例外。
- 注册 CORS 和静态文件。
- 注册所有业务路由。
- 暴露健康检查、市场情绪、静态页面和数据库导入接口。

主要路由：

| 路由 | 文件 | 功能 |
| --- | --- | --- |
| `/api/bloggers` | `routes/bloggers.py` | 博主 CRUD、重算统计 |
| `/api/posts` | `routes/posts.py` | 帖子录入、分析、批量分析、低质量清理 |
| `/api/predictions` | `routes/predictions.py` | 预测 CRUD、验证、批量验证、统计、导出、合并 |
| `/api/funds` | `routes/funds.py` | 基金列表、详情、同步、历史、趋势 |
| `/api/viewpoints` | `routes/viewpoints.py` | 观点列表、批量分析、汇总、清理 |
| `/api/crawler` | `routes/crawler.py`、`eastmoney_routes.py` | 爬虫状态、抓取、采纳 |
| `/api/advice` | `routes/advice.py` | 投资建议生成、历史、统计 |
| `/api/stats` | `routes/stats.py` | 总体统计 |
| `/api/config` | `routes/config.py` | LLM 配置、清理、别名、板块基金映射、导入导出 |
| `/api/batch-analysis` | `routes/batch_analysis.py` | 批量分析任务 |
| `/api/sector-flow` | `routes/sector_flow.py` | 板块资金流抓取、排行、历史、状态 |
| `/api/test-data` | `routes/test_data.py` | 测试数据查找和清理 |
| `/api/prediction-groups` | `prediction_groups.py` | 相似预测组合并和详情 |

## 数据模型

所有模型集中在 `src/models/database.py`。目前没有 Alembic 迁移体系，模型变更必须同步考虑 SQLite 本地库和 Supabase/PostgreSQL 生产库。

核心表：

| 表 | 模型 | 说明 |
| --- | --- | --- |
| `bloggers` | `Blogger` | 博主、平台、准确率、等级、短期表现 |
| `posts` | `Post` | 帖子正文、发布日期、分析结果 |
| `predictions` | `Prediction` | 预测方向、基金/板块、周期、验证状态、软删除 |
| `fund_info` | `FundInfo` | 基金信息、最新净值、技术指标、活跃预测数 |
| `fund_history` | `FundHistory` | 基金历史净值，预测验证依赖它 |
| `viewpoints` | `Viewpoint` | 观点、方向、板块、权重、有效期、汇总观点 |
| `crawler_article_records` | `CrawlerArticleRecord` | 爬虫文章去重和采纳状态 |
| `sector_fund_mapping` | `SectorFundMapping` | 板块到基金的映射，可人工审核 |
| `sector_alias` | `SectorAlias` | 板块别名和黑话映射 |
| `investment_advice` | `InvestmentAdvice` | 投资建议主表 |
| `sector_fund_flow` | `SectorFundFlow` | 板块资金流、暗盘、主力强度、行为标签 |
| `sector_flow_fetch_runs` | `SectorFlowFetchRun` | 板块资金流抓取运行日志 |
| `batch_analysis_tasks` | `BatchAnalysisTask` | 批量分析任务状态 |
| `analysis_logs` | `AnalysisLog` | LLM 分析过程日志 |
| `cleanup_logs` 等 | `Cleanup*` | 清理任务、规则和明细 |
| `system_config` | `SystemConfig` | 生产持久化 LLM 配置 |

## 核心业务流

### 帖子到预测

```text
用户粘贴帖子
  -> POST /api/posts
  -> PostService.create_post_with_analysis()
  -> LLMAnalyzer.generate_title()/analyze_post()
  -> Prediction 入库
  -> FundInfo.active_predictions 更新
  -> 前端展示预测
```

关键点：

- 帖子分析依赖 `src/analyzer/llm_analyzer.py` 的 JSON 解析、时间周期识别、板块基金匹配。
- 板块到基金优先查数据库映射，再查常量映射和外部 API。
- 分析失败时要保持帖子和预测状态一致。

### 预测验证

```text
预测达到 target_date
  -> PredictionVerifyService.verify_all_pending()
  -> 查 FundHistory 起点/终点/过程净值
  -> 判断 bullish/bearish/neutral 是否命中
  -> 写入 verify_history、verify_score、actual_change、status
  -> 更新 Blogger 准确率
```

关键点：

- `PredictionVerifyService` 有净值缓存和过程指标判断。
- `prediction_verify_task` 负责批量验证的运行状态，避免前端长请求超时。
- Render Cron 和本地调度器都会触发验证。

### 观点和投资建议

```text
人工观点或爬虫文章
  -> Viewpoint
  -> ViewpointService 批量分析/汇总/权重
  -> AdviceService
  -> LLMAnalyzer.generate_investment_advice_three_stage()
  -> InvestmentAdvice
```

关键点：

- 观点支持来源、作者、板块、置信度、权重、有效期。
- 建议生成应带风险提示，不应给确定性收益承诺。

### 板块资金流

```text
GitHub Actions/Render Cron/API 手动触发
  -> scripts/fetch_sector_flow.py 或 /api/sector-flow/fetch
  -> SectorFlowService.run_fetch()
  -> SectorFlowCrawler 抓数据
  -> 计算 dark_pool、main_intensity、behavior
  -> 幂等写入 sector_fund_flow
  -> 写 sector_flow_fetch_runs
```

行为标签含义：

- `grab`：抢筹。
- `build`：建仓。
- `wash`：洗盘。
- `sell`：卖出。

## 后台任务

`src/tasks/scheduler.py` 使用北京时间：

- 启动时先执行基金更新、预测验证、过期补救验证。
- 02:00-02:59：清理任务。
- 10:00-10:59：基金更新、预测验证、过期补救验证。
- 15:30-15:59：基金更新。

Render Cron 不依赖常驻线程，而是运行：

```bash
python scripts/run_scheduled_tasks.py daily
```

该入口会初始化数据库，运行板块资金流抓取、基金更新、预测验证、过期补救验证。

GitHub Actions 中 `.github/workflows/sector_flow_crawler.yml` 在交易日 13:30 北京时间运行 `scripts/fetch_sector_flow.py`。

## 前端架构

- `web/index.html`：主 SPA，包含登录、仪表盘、博主、帖子、预测、基金、观点、配置、清理等主要界面。
- `web/common.js`：公共 API 请求、密码处理、通用工具。
- `web/common.css`：公共样式。
- `web/cleanup-manager.html`、`web/article-crawler.html`、`web/viewpoint-manager.html` 等是辅助页面。

约束：

- 不引入构建链。
- 不引入重 UI 框架。
- 修改 `web/index.html` 前先用 `rg` 定位相关方法、模板和样式。
- 前端 API 请求要带 `X-Access-Password`。

## 部署架构

```text
Render Web Service
  -> uvicorn src.api.main:app
  -> Supabase PostgreSQL

Render Cron
  -> python scripts/run_scheduled_tasks.py daily
  -> Supabase PostgreSQL

GitHub Actions
  -> python scripts/fetch_sector_flow.py
  -> Supabase PostgreSQL
```

生产注意：

- Render 的 `PYTHON_VERSION` 当前配置为 `3.10.12`；GitHub Actions 使用 Python 3.12。
- `DATABASE_URL`、`ACCESS_PASSWORD`、`VOLCENGINE_API_KEY` 是 secret，不写入文档明文。
- PostgreSQL 连接池参数在 `render.yaml` 和 `_get_postgres_pool_settings()` 中。
- 数据库导入接口默认关闭，启用后仍要求 `X-Danger-Confirm: import-production-database`。

## 高风险区域

| 文件 | 风险 |
| --- | --- |
| `src/analyzer/llm_analyzer.py` | 影响所有 LLM 分析、预测提取、建议生成、解析兜底 |
| `src/models/database.py` | 影响所有表和生产数据库兼容 |
| `src/services/prediction_verify_service.py` | 影响准确率、评分和历史验证 |
| `src/services/sector_flow_service.py` | 影响板块资金流写入、幂等和状态 |
| `src/api/main.py` | 影响鉴权、路由、健康检查、危险导入接口 |
| `web/index.html` | 主界面大文件，容易误改跨功能状态 |
| `render.yaml` | 生产启动和定时任务配置 |

## 测试和验证

常规验证：

```bash
pytest tests/ -v
python -m src --init-db
```

重点测试：

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_sector_flow_service.py -v
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_production_hardening.py -v
```

文档或索引改动：

```bash
rg "过时关键词或旧命令" .
codegraph sync .
codegraph status .
```

## 接手建议

1. 先读 `AGENTS.md` 或 `CLAUDE.md`。
2. 要理解业务读本文件。
3. 要理解产品风格读 `PRODUCT.md`。
4. 要部署读 `DEPLOYMENT.md`。
5. 改功能前用 CodeGraph 或 `rg` 找调用方和测试。
6. 没有验证证据，不要声称完成。
