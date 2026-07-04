# Fund Insight - 基金博主分析系统

Fund Insight 用来记录基金博主和财经文章观点，通过 LLM 提取结构化预测，再用基金净值和板块资金流验证这些观点是否靠谱。

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 博主和帖子管理 | 添加博主，手动粘贴帖子，保留来源和发布日期 |
| AI 预测提取 | 从帖子中提取看涨/看跌/震荡、基金或板块、预测周期、置信度 |
| 预测验证 | 到期后同步基金净值，按方向、涨跌幅和过程表现验证预测 |
| 博主评分 | 统计总准确率、短期准确率、预测数量、等级等指标 |
| 观点库 | 接收人工观点或爬虫采纳文章，支持批量分析和汇总 |
| 投资建议 | 汇总观点、预测和市场数据，生成带风险提示的建议 |
| 板块资金流 | 抓取东方财富板块资金数据，计算暗盘、主力强度、行为标签 |
| 清理工具 | 清理测试数据、旧数据、孤儿基金和抓取日志 |

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境
copy .env.example .env
# 编辑 .env，至少填写 LLM_API_KEY 或 VOLCENGINE_API_KEY

# 3. 初始化数据库
python -m src --init-db

# 4. 启动服务
python -m src --port 8002
```

访问：

```text
http://localhost:8002
```

所有 `/api/` 请求需要访问密码。前端登录后会把密码放入 `X-Access-Password` 请求头。

## 常用命令

```bash
# 启动服务
python -m src
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8002

# 数据库初始化
python -m src --init-db

# 测试
pytest tests/ -v
pytest tests/unit/ -v
pytest tests/integration/ -v

# Render Cron 同款每日任务
python scripts/run_scheduled_tasks.py daily

# 手动抓取板块资金流
python scripts/fetch_sector_flow.py

# CodeGraph 索引
codegraph status .
codegraph sync .
```

## 环境配置

本地默认使用 SQLite：`data/fund_insight.db`。

生产设置 `DATABASE_URL` 后使用 PostgreSQL/Supabase。

关键变量：

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | `siliconflow` 或 `volcengine` |
| `LLM_API_KEY` | OpenAI 兼容服务密钥 |
| `LLM_BASE_URL` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | 主模型 |
| `LLM_LIGHT_MODEL` | 轻量模型 |
| `VOLCENGINE_API_KEY` | 火山引擎密钥 |
| `DATABASE_URL` | PostgreSQL/Supabase 连接 |
| `ACCESS_PASSWORD` | 前端和 API 访问密码 |
| `CRAWLER_ENABLED` | 爬虫功能开关 |
| `CORS_ORIGINS` | 允许访问的前端域名 |

## 使用流程

```text
添加博主
  ↓
粘贴帖子并选择日期
  ↓
AI 分析帖子，生成预测
  ↓
同步基金净值和历史数据
  ↓
到期验证预测
  ↓
查看博主准确率、基金表现、建议和板块资金流
```

## 项目结构

```text
src/
  api/        FastAPI 应用、路由、schemas、响应封装
  services/   业务逻辑、验证、统计、清理、板块资金流服务
  models/     SQLAlchemy ORM 和数据库连接
  analyzer/   LLM 分析、本地趋势分析、帖子价值分析
  fund/       基金 API、多源数据、同步、技术指标
  crawler/    外部文章/帖子/板块资金流抓取
  tasks/      本地调度器和清理任务
  utils/      时间解析、基金匹配、统计、并发工具
web/          原生 HTML/JS 前端，无构建链
scripts/      维护、抓取、重分析、检查脚本
tests/        pytest 单元和集成测试
docs/         设计、计划、修复报告和功能文档
```

更多架构细节见 `ARCHITECTURE.md`。给 AI 接手项目时优先读 `AGENTS.md` 或 `CLAUDE.md`。

## 部署

当前部署方式：

- Render Web Service：启动 FastAPI。
- Render Cron：每天运行 `scripts/run_scheduled_tasks.py daily`。
- Supabase/PostgreSQL：生产数据库。
- GitHub Actions：交易日抓取板块资金流。

详见 `DEPLOYMENT.md`。

## 注意事项

- `src/analyzer/llm_analyzer.py`、`src/models/database.py`、`src/services/prediction_verify_service.py`、`src/services/sector_flow_service.py`、`web/index.html` 是高风险文件。
- 数据库模型变更后要同步考虑 Supabase 表结构，当前项目没有 Alembic 迁移体系。
- 前端没有构建工具，直接修改 `web/` 文件即可。
- `.codegraph/` 是本地索引产物，不要手工编辑数据库文件。
- 本项目是辅助研究工具，不构成投资建议。
