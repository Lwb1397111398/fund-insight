# Fund Insight 项目架构文档

## 项目概述

**项目名称**: Fund Insight - 基金博主分析系统  
**版本**: 2.1.0  
**最后更新**: 2026-03-07

---

## 一、项目结构

```
fund-insight/
├── src/                          # 源代码目录
│   ├── core/                     # 核心配置层
│   │   ├── config.py             # 配置管理
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── models/                   # 数据模型层
│   │   ├── database.py           # ORM 模型定义
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── services/                 # 服务层（新增）
│   │   ├── __init__.py
│   │   ├── base.py               # 基础服务类
│   │   ├── blogger_service.py    # 博主服务
│   │   ├── post_service.py       # 帖子服务
│   │   ├── prediction_service.py # 预测服务
│   │   ├── fund_service.py       # 基金服务
│   │   ├── viewpoint_service.py  # 观点服务
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── api/                      # API 层（重构）
│   │   ├── main.py               # 主应用入口
│   │   ├── deps.py               # 依赖注入
│   │   ├── routes/               # 路由模块
│   │   │   ├── bloggers.py
│   │   │   ├── posts.py
│   │   │   ├── predictions.py
│   │   │   ├── funds.py
│   │   │   ├── viewpoints.py
│   │   │   ├── crawler.py
│   │   │   ├── advice.py
│   │   │   └── stats.py
│   │   ├── schemas/              # 数据验证模式（新增）
│   │   │   ├── blogger.py
│   │   │   ├── post.py
│   │   │   ├── prediction.py
│   │   │   ├── fund.py
│   │   │   ├── viewpoint.py
│   │   │   └── common.py
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── analyzer/                 # 分析器层
│   │   ├── llm_analyzer.py       # LLM 分析器
│   │   ├── viewpoint_analyzer.py # 观点分析器
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── crawler/                  # 爬虫层（重构）
│   │   ├── base.py               # 爬虫基类（新增）
│   │   ├── tiantian_crawler.py
│   │   ├── eastmoney_blog_crawler.py
│   │   ├── eastmoney_guide_crawler.py
│   │   ├── sina_blog_crawler.py
│   │   ├── sina_finance_crawler.py
│   │   ├── filters/              # 筛选器（新增）
│   │   │   ├── quality_filter.py
│   │   │   └── ai_filter.py
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   ├── fund/                     # 基金服务层
│   │   ├── fund_api.py
│   │   ├── fund_auto_manager.py
│   │   └── MODULE_RECORD.md      # 模块文档
│   │
│   └── tasks/                    # 定时任务层
│       ├── scheduler.py
│       ├── cleanup_tasks.py
│       └── MODULE_RECORD.md      # 模块文档
│
├── tests/                        # 测试目录（新增）
│   ├── conftest.py               # pytest 配置
│   ├── unit/                     # 单元测试
│   │   ├── test_services/
│   │   └── test_crawler/
│   └── integration/              # 集成测试
│
├── web/                          # 前端页面
├── data/                         # 数据库文件
└── docs/                         # 文档目录
```

---

## 二、分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        表现层 (Presentation)                      │
│  web/ (HTML) + api/routes/ (REST API) + schemas/ (数据验证)      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                        服务层 (Service)                          │
│  services/ (业务逻辑封装、事务管理、跨模块协调)                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                        领域层 (Domain)                           │
│  analyzer/ (分析服务) + crawler/ (数据采集) + fund/ (基金服务)   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                        数据层 (Data)                             │
│  models/ (ORM模型) + database.py (连接管理)                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                        基础设施层 (Infrastructure)               │
│  core/ (配置) + tasks/ (定时任务)                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| **core** | 配置管理 | 无 |
| **models** | 数据模型定义 | core |
| **services** | 业务逻辑封装 | models, core |
| **api** | HTTP 接口 | services, schemas |
| **analyzer** | LLM 分析 | core |
| **crawler** | 数据抓取 | core, analyzer |
| **fund** | 基金服务 | core, models, analyzer |
| **tasks** | 定时任务 | models, fund |

---

## 四、数据流

```
用户请求 → API 路由 → 服务层 → 数据层 → 数据库
                ↓
           Schemas 验证
                ↓
           领域层处理
```

---

## 五、关键设计决策

### 5.1 服务层设计

- **目的**: 隔离业务逻辑与 API 层
- **模式**: 使用泛型基类提供通用 CRUD
- **好处**: 代码复用、易于测试、职责清晰

### 5.2 路由拆分

- **目的**: 降低 main.py 复杂度
- **模式**: 按业务领域拆分路由
- **好处**: 易于维护、团队协作

### 5.3 Schemas 验证层

- **目的**: 统一请求数据验证
- **模式**: Pydantic 模型
- **好处**: 自动验证、文档生成

### 5.4 爬虫重构

- **目的**: 分离数据抓取和筛选逻辑
- **模式**: 基类 + 筛选器
- **好处**: 易于扩展新爬虫

---

## 六、使用指南

### 6.1 启动服务

```bash
# 方式1：使用启动脚本
start.bat

# 方式2：Python 模块方式
python -m src --port 8002
```

### 6.2 运行测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行单元测试
pytest tests/unit/ -v

# 运行集成测试
pytest tests/integration/ -v
```

### 6.3 添加新功能

1. 在 `models/` 添加数据模型
2. 在 `services/` 添加服务类
3. 在 `api/schemas/` 添加验证模式
4. 在 `api/routes/` 添加路由
5. 在 `tests/` 添加测试

---

## 七、变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2024-01-01 | 1.0.0 | 初始版本 |
| 2024-06-01 | 2.0.0 | 添加爬虫模块 |
| 2026-03-07 | 2.1.0 | 模块化重构 |
