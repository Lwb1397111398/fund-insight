# 模块记录文档 - Core 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Core (核心配置模块) |
| **模块路径** | src/core/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

集中管理项目的所有配置项，包括 LLM 配置、数据库路径、服务器配置和爬虫配置，为其他模块提供统一的配置访问入口。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| LLM 配置管理 | 管理 LLM API Key、Base URL、模型选择 | 已实现 |
| 多模型策略 | 支持主模型和轻量级模型切换 | 已实现 |
| 数据库配置 | 管理数据库路径 | 已实现 |
| 服务器配置 | 管理服务器主机和端口 | 已实现 |
| 爬虫配置 | 管理爬虫开关、请求延迟、超时等 | 已实现 |
| 环境变量加载 | 从 .env 文件加载配置 | 已实现 |

### 1.3 边界定义

**包含：**
- 配置项定义和默认值
- 环境变量加载
- 配置验证
- 路径管理

**不包含：**
- 业务逻辑
- 数据库操作
- 外部服务调用
- 配置热更新（未来可能添加）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| config.py | 配置管理核心文件 | 41 | Config, config |
| __init__.py | 模块导出 | 5 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| python-dotenv | 直接导入 | load_dotenv | 低 |
| os/pathlib | 标准库 | 环境变量、路径 | 低 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| models | 直接导入 | config.DB_PATH |
| analyzer | 直接导入 | config.LLM_* |
| crawler | 直接导入 | config.CRAWLER_* |
| api | 直接导入 | config.SERVER_* |
| tasks | 直接导入 | config |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| python-dotenv | ^1.0.0 | 加载 .env 文件 |

## 四、核心接口

### 4.1 公开接口

```python
from src.core import config
```

### 4.2 接口说明

#### Config 类

```python
class Config:
    """配置管理类，包含所有配置项"""
    
    # LLM 配置
    LLM_API_KEY: str          # LLM API 密钥
    LLM_BASE_URL: str         # LLM API 地址
    LLM_MODEL: str            # 主模型名称
    LLM_LIGHT_MODEL: str      # 轻量级模型名称
    LLM_STRATEGY: str         # 模型选择策略
    
    # 数据库配置
    DB_PATH: Path             # 数据库文件路径
    
    # 服务器配置
    SERVER_HOST: str          # 服务器主机
    SERVER_PORT: int          # 服务器端口
    
    # 爬虫配置
    CRAWLER_ENABLED: bool     # 爬虫开关
    CRAWLER_REQUEST_DELAY: float  # 请求间隔
    MAX_POSTS_PER_FUND: int   # 每基金最大帖子数
    CRAWLER_TIMEOUT: int      # 请求超时
```

#### config 实例

```python
config = Config()  # 全局单例配置实例
```

## 五、数据模型

### 5.1 使用的数据库表

无

### 5.2 数据流向

```
.env 文件 → load_dotenv() → Config 类 → config 实例 → 其他模块
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| LLM_API_KEY | LLM_API_KEY | "" | LLM API 密钥 |
| LLM_BASE_URL | LLM_BASE_URL | https://api.siliconflow.cn/v1 | LLM API 地址 |
| LLM_MODEL | LLM_MODEL | deepseek-ai/DeepSeek-V3.2 | 主模型 |
| LLM_LIGHT_MODEL | LLM_LIGHT_MODEL | Qwen/Qwen2.5-7B-Instruct | 轻量级模型 |
| LLM_STRATEGY | LLM_STRATEGY | auto | 模型选择策略 |
| DB_PATH | - | data/fund_insight.db | 数据库路径 |
| SERVER_HOST | SERVER_HOST | 0.0.0.0 | 服务器主机 |
| SERVER_PORT | SERVER_PORT | 8002 | 服务器端口 |
| CRAWLER_ENABLED | CRAWLER_ENABLED | false | 爬虫开关 |
| CRAWLER_REQUEST_DELAY | CRAWLER_REQUEST_DELAY | 2.0 | 请求间隔(秒) |
| MAX_POSTS_PER_FUND | MAX_POSTS_PER_FUND | 10 | 每基金最大帖子数 |
| CRAWLER_TIMEOUT | CRAWLER_TIMEOUT | 10 | 请求超时(秒) |

## 七、使用示例

### 7.1 基本使用

```python
from src.core import config

# 获取 LLM 配置
api_key = config.LLM_API_KEY
base_url = config.LLM_BASE_URL

# 获取数据库路径
db_path = config.DB_PATH

# 获取服务器配置
host = config.SERVER_HOST
port = config.SERVER_PORT

# 检查爬虫是否启用
if config.CRAWLER_ENABLED:
    # 执行爬虫逻辑
    pass
```

### 7.2 高级用法

```python
from src.core.config import Config

# 创建自定义配置（用于测试）
test_config = Config()
test_config.DB_PATH = "test.db"
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_core.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| config.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| 无 | - | - |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加配置验证 | 中 | 0.5人天 |
| 添加配置热更新 | 低 | 1人天 |
| 添加自定义异常类 | 中 | 0.5人天 |
