# 模块记录文档 - API 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | API (接口模块) |
| **模块路径** | src/api/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 2.0.0 |

## 一、模块概述

### 1.1 职责定义

提供 RESTful API 接口，处理 HTTP 请求，协调各服务模块完成业务逻辑，返回响应数据。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 博主管理 API | 博主 CRUD 操作 | 已实现 |
| 帖子管理 API | 帖子 CRUD 和分析 | 已实现 |
| 预测管理 API | 预测 CRUD 和验证 | 已实现 |
| 基金管理 API | 基金 CRUD 和同步 | 已实现 |
| 观点管理 API | 观点 CRUD 和采纳 | 已实现 |
| 爬虫 API | 触发爬虫任务 | 已实现 |
| 投资建议 API | 生成投资建议 | 已实现 |
| 统计 API | 数据统计和报表 | 已实现 |

### 1.3 边界定义

**包含：**
- FastAPI 应用配置
- 路由定义
- 请求模型定义
- 响应封装
- CORS 配置
- 静态文件服务

**不包含：**
- 业务逻辑（由 services 负责）
- 数据访问（由 models 负责）
- LLM 分析（由 analyzer 负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| main.py | FastAPI 主应用 | ~3500 | app, 各路由函数 |
| eastmoney_routes.py | 东方财富爬虫路由 | ~300 | router |
| prediction_groups.py | 预测组路由 | ~200 | router |
| __init__.py | 模块导出 | 3 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| core | 直接导入 | config | 低 |
| models | 直接导入 | 所有模型, get_db | 高 |
| analyzer | 直接导入 | get_analyzer | 中 |
| fund | 直接导入 | fund_api, fund_data_manager | 中 |
| crawler | 直接导入 | ai_analyzer | 中 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| __main__.py | 直接导入 | app |
| web | HTTP 请求 | API 端点 |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| fastapi | ^0.100.0 | Web 框架 |
| uvicorn | ^0.23.0 | ASGI 服务器 |
| pydantic | ^2.0.0 | 数据验证 |

## 四、核心接口

### 4.1 公开接口

```python
from src.api.main import app
```

### 4.2 API 端点列表

#### 博主相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/bloggers | 获取博主列表 |
| POST | /api/bloggers | 创建博主 |
| GET | /api/bloggers/{id} | 获取博主详情 |
| PUT | /api/bloggers/{id} | 更新博主 |
| DELETE | /api/bloggers/{id} | 删除博主 |
| GET | /api/bloggers/{id}/predictions | 获取博主预测 |

#### 帖子相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/posts | 获取帖子列表 |
| POST | /api/posts | 创建帖子 |
| POST | /api/posts/{id}/analyze | 分析帖子 |
| PUT | /api/posts/{id}/title | 更新标题 |

#### 预测相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/predictions | 获取预测列表 |
| POST | /api/predictions | 创建预测 |
| POST | /api/predictions/{id}/verify | 验证预测 |
| GET | /api/predictions/stats | 预测统计 |

#### 基金相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/funds | 获取基金列表 |
| POST | /api/funds | 添加基金 |
| GET | /api/funds/{code} | 获取基金详情 |
| POST | /api/funds/{code}/sync | 同步基金数据 |

#### 观点相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/viewpoints | 获取观点列表 |
| POST | /api/viewpoints | 创建观点 |
| DELETE | /api/viewpoints/{id} | 删除观点 |

#### 爬虫相关

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/crawler/eastmoney-blog | 抓取东财博客 |
| POST | /api/crawler/sina-finance | 抓取新浪财经 |
| POST | /api/crawler/sina-blog | 抓取新浪博文 |
| POST | /api/crawler/eastmoney-guide | 抓取博客导读 |

#### 投资建议

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/advice | 获取投资建议 |
| POST | /api/advice/generate | 生成投资建议 |

## 五、数据模型

### 5.1 请求模型

```python
class BloggerCreate(BaseModel):
    name: str
    platform: str = "xiaohongshu"
    description: Optional[str] = None

class PostCreate(BaseModel):
    blogger_id: int
    title: Optional[str] = None
    content: str
    post_date: date
    source_url: Optional[str] = None

class FundAdd(BaseModel):
    fund_code: str
    fund_name: Optional[str] = None
```

### 5.2 响应格式

```python
# 成功响应
{
    "success": True,
    "data": {...}
}

# 错误响应
{
    "success": False,
    "message": "错误信息"
}
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| SERVER_HOST | SERVER_HOST | 0.0.0.0 | 服务器主机 |
| SERVER_PORT | SERVER_PORT | 8002 | 服务器端口 |

## 七、使用示例

### 7.1 启动服务器

```python
# 方式1：使用 uvicorn
uvicorn src.api.main:app --host 0.0.0.0 --port 8002

# 方式2：使用模块入口
python -m src --port 8002
```

### 7.2 API 调用示例

```python
import requests

# 获取博主列表
response = requests.get('http://localhost:8002/api/bloggers')
bloggers = response.json()['data']

# 创建博主
response = requests.post('http://localhost:8002/api/bloggers', json={
    'name': '测试博主',
    'platform': 'xiaohongshu'
})

# 分析帖子
response = requests.post('http://localhost:8002/api/posts/1/analyze')
result = response.json()
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_api.py -v
```

### 8.2 集成测试

```bash
# 运行集成测试
pytest tests/integration/test_api.py -v
```

### 8.3 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| main.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 2.0.0 | 添加爬虫路由 | Team |
| 2026-03-07 | 2.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| main.py 文件过大（3500+行） | 高 | 待重构 |
| 缺少请求验证层 | 中 | 待定 |
| 缺少统一错误处理 | 中 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 拆分路由到独立文件 | 高 | 2人天 |
| 创建 services 服务层 | 高 | 3人天 |
| 添加 schemas 验证层 | 中 | 1人天 |
| 添加统一错误处理 | 中 | 0.5人天 |
