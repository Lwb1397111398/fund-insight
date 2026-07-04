# 模块记录 - API

## 模块定位

`src/api/` 是 Fund Insight 的 HTTP 入口层，负责 FastAPI 应用、访问控制、路由注册、请求/响应结构和静态页面服务。业务逻辑应尽量放在 `src/services/`，不要在路由里堆复杂计算。

## 当前职责

- 创建 FastAPI `app`。
- 注册访问密码中间件：所有 `/api/` 请求要求 `X-Access-Password`，健康检查例外。
- 注册 CORS、静态文件和 `web/` 页面。
- 调用 `init_db()` 并加载持久化 LLM 配置。
- 注册领域路由。
- 提供健康检查、市场情绪和受保护的数据库导入接口。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `main.py` | FastAPI 入口、中间件、生命周期、路由注册、静态页面、数据库导入 |
| `deps.py` | 数据库和服务依赖注入 |
| `decorators.py` | API 异常处理装饰器 |
| `responses.py` | 统一响应辅助 |
| `eastmoney_routes.py` | 东方财富博客抓取和文章采纳 |
| `prediction_groups.py` | 相似预测分组 |
| `routes/*.py` | 按业务领域拆分的 REST 路由 |
| `schemas/*.py` | Pydantic 请求/响应模型 |

## 路由总览

| 前缀 | 文件 | 功能 |
| --- | --- | --- |
| `/api/bloggers` | `routes/bloggers.py` | 博主列表、创建、删除、重算统计 |
| `/api/posts` | `routes/posts.py` | 帖子列表、创建、分析、批量分析、质量清理 |
| `/api/predictions` | `routes/predictions.py` | 预测 CRUD、验证、批量验证、统计、导出、合并 |
| `/api/funds` | `routes/funds.py` | 基金列表、添加、详情、同步、历史、趋势 |
| `/api/viewpoints` | `routes/viewpoints.py` | 观点列表、删除、批量分析、汇总、清理 |
| `/api/crawler` | `routes/crawler.py` | 爬虫状态、自动采纳、微信抓取 |
| `/api/crawler` | `eastmoney_routes.py` | 东方财富博客抓取和采纳 |
| `/api/advice` | `routes/advice.py` | 投资建议生成、历史、统计 |
| `/api/stats` | `routes/stats.py` | 总体统计 |
| `/api/config` | `routes/config.py` | LLM 配置、清理、别名、板块基金映射、导入导出 |
| `/api/test-data` | `routes/test_data.py` | 测试数据扫描和清理 |
| `/api/batch-analysis` | `routes/batch_analysis.py` | 批量分析任务 |
| `/api/sector-flow` | `routes/sector_flow.py` | 板块资金流抓取、排行、历史、状态 |
| `/api/prediction-groups` | `prediction_groups.py` | 相似预测组 |

## 依赖关系

- 上游：`core.config`、`models.database`、`services.*`、`analyzer.*`、`crawler.*`、`fund.*`。
- 下游：`web/` 前端、Render/uvicorn、测试客户端。

## 高风险点

- `main.py` 的访问密码中间件影响所有 API。
- `/api/import-database` 是高风险接口，默认由 `ENABLE_DATABASE_IMPORT=false` 关闭，启用后还要求确认头。
- `routes/config.py` 包含清理、映射和配置导入导出，改动需跑生产硬化测试。
- 路由顺序可能影响 FastAPI 动态路径匹配，新增路径要避免被 `/{id}` 捕获。

## 推荐验证

```bash
pytest tests/integration/test_api.py -v
pytest tests/unit/test_production_hardening.py -v
pytest tests/unit/test_deployment_optimization.py -v
python -m src --init-db
```
