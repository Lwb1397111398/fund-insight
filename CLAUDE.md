# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## 项目概述

**Fund Insight（基金博主分析系统）** 是一个 Python 全栈 Web 应用，用于追踪和分析博主在天天基金等平台发布的基金投资预测。用户手动粘贴帖子内容，系统通过 LLM 提取预测观点（看涨/看跌/震荡），关联具体基金，并自动对比基金净值验证预测准确率。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy 2.0 + Pydantic 2.0
- **前端**: 原生 HTML/JS SPA（web/ 目录），使用 Vue.js 3（CDN）和 axios
- **数据库**: SQLite（本地开发）/ PostgreSQL（Render 云端部署，通过 psycopg2）
- **LLM**: OpenAI 兼容 SDK，支持 DeepSeek、硅基流动、火山引擎
- **基金数据**: 天天基金 API
- **部署**: Render.com（云端）/ PyInstaller（Windows 独立 EXE）/ PythonAnywhere（WSGI）
- **测试**: pytest

## 常用命令

```bash
# 启动服务（默认端口 8002）
python -m src
python -m src --port 8002

# 或直接通过 uvicorn
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8002

# 仅初始化数据库
python -m src --init-db

# 运行所有测试
pytest tests/ -v

# 仅运行单元测试
pytest tests/unit/ -v

# 仅运行集成测试
pytest tests/integration/ -v

# 构建 Windows EXE
python build_exe.py
```

## 环境配置

复制 `.env.example` → `.env`，至少配置：
- `LLM_API_KEY` — LLM API 密钥（推荐 DeepSeek）
- `LLM_BASE_URL` — 提供商地址（如 `https://api.deepseek.com/v1`）
- `LLM_MODEL` — 模型名称（如 `deepseek-ai/DeepSeek-V3`）
- `ACCESS_PASSWORD` — 访问密码（默认: `Lwb1397111398`）

## 架构概览

### 分层结构

```
web/index.html  ←──HTTP──→  src/api/main.py (FastAPI)
                               │
                          src/api/routes/    — REST 路由（11 个模块）
                          src/api/schemas/   — Pydantic 请求/响应校验
                               │
                          src/services/      — 业务逻辑层（10+ 服务）
                               │
                    ┌──────────┼──────────┐
                    │          │          │
              src/analyzer/  src/crawler/  src/fund/
              LLM分析引擎    爬虫模块      基金数据获取
                    │          │          │
                    └──────────┼──────────┘
                               │
                          src/models/database.py — 30+ SQLAlchemy ORM 模型
                               │
                     SQLite (本地) / PostgreSQL (云端)
```

### 核心数据流

1. 用户添加博主 → `POST /api/bloggers`
2. 用户粘贴帖子 → `POST /api/posts`
3. AI 分析帖子 → `POST /api/posts/{id}/analyze` → `src/analyzer/llm_analyzer.py`
4. LLM 提取：预测方向、目标基金、时间周期、置信度 → 存入 `predictions` 表
5. 定时任务每日 10:00 运行（`src/tasks/scheduler.py`）→ 从天天基金获取最新净值 → 验证预测

### 关键模块

| 模块 | 说明 |
|------|------|
| `src/analyzer/llm_analyzer.py` | LLM 分析核心（1000+ 行），含熔断器、指数退避重试、结果缓存、并发分析 |
| `src/api/main.py` | FastAPI 应用入口（860 行），含中间件、页面路由、导入接口 |
| `src/models/database.py` | 30+ ORM 模型（1075 行），含博主、帖子、预测、基金、观点等实体 |
| `src/fund/fund_api.py` | 天天基金 API 客户端，获取基金净值数据 |
| `src/services/prediction_verify_service.py` | 预测验证服务，对比基金实际涨跌 |
| `src/tasks/scheduler.py` | 后台定时任务调度（清理、验证、基金更新） |
| `src/crawler/` | 爬虫模块（默认禁用，`CRAWLER_ENABLED=false`），支持天天基金吧、东方财富博客等 |

### API 路由模块

| 路由文件 | 端点前缀 | 功能 |
|----------|---------|------|
| `routes/bloggers.py` | `/api/bloggers` | 博主 CRUD |
| `routes/posts.py` | `/api/posts` | 帖子 CRUD + AI 分析 |
| `routes/predictions.py` | `/api/predictions` | 预测 CRUD |
| `routes/funds.py` | `/api/funds` | 基金 CRUD |
| `routes/viewpoints.py` | `/api/viewpoints` | 市场观点管理 |
| `routes/crawler.py` | `/api/crawler` | 爬虫控制 |
| `routes/advice.py` | `/api/advice` | AI 投资建议 |
| `routes/stats.py` | `/api/stats` | 统计分析 |
| `routes/config.py` | `/api/config` | 配置管理 |
| `routes/batch_analysis.py` | `/api/batch-analysis` | 批量分析 |
| `routes/test_data.py` | `/api/test-data` | 测试数据管理 |

### 数据存储

- **本地**: `data/fund_insight.db`（SQLite）
- **配置持久化**: `data/llm_config.json`（LLM 配置，重启保留）
- **数据库迁移**: 根目录有 7 个 `migrate_*.py` 脚本
- **维护脚本**: `scripts/` 目录下有 22 个工具脚本（数据修复、重分析、检查等）

## 注意事项

- 数据库模型变更后需要编写迁移脚本（参考根目录 `migrate_*.py`）
- LLM 分析模块 (`llm_analyzer.py`) 是核心复杂模块，修改需谨慎
- 爬虫模块默认禁用，修改爬虫代码时注意 `CRAWLER_ENABLED` 配置
- 前端是纯 HTML/JS，不经过构建工具，直接修改 `web/` 目录下的文件即可
