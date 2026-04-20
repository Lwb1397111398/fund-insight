# 模块记录文档 - Models 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Models (数据模型模块) |
| **模块路径** | src/models/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

定义项目的所有数据库模型（ORM），管理数据库连接和会话，提供数据持久化的基础能力。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| ORM 模型定义 | 定义 12 个数据表模型 | 已实现 |
| 数据库连接管理 | 创建和管理 SQLite 连接 | 已实现 |
| 会话管理 | 提供数据库会话工厂 | 已实现 |
| 数据库初始化 | 创建所有表 | 已实现 |
| 自动建表 | 启动时自动创建表 | 已实现 |

### 1.3 边界定义

**包含：**
- ORM 模型定义
- 数据库连接配置
- 会话管理
- 数据库初始化

**不包含：**
- 业务逻辑
- 数据验证（由 schemas 负责）
- 复杂查询（由 services 负责）
- 数据迁移

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| database.py | 数据库模型定义 | 342 | Blogger, Post, Prediction, Viewpoint, FundInfo, FundHistory, InvestmentAdvice, SectorFundMapping, VerificationTask, PredictionGroup |
| __init__.py | 模块导出 | 10 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| core | 直接导入 | config.DB_PATH | 低 |
| sqlalchemy | 第三方库 | ORM 框架 | 中 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| api | 直接导入 | 所有模型, get_db |
| analyzer | 直接导入 | 模型类 |
| fund | 直接导入 | FundInfo, FundHistory |
| tasks | 直接导入 | 所有模型, get_db |
| crawler | 直接导入 | Viewpoint |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| sqlalchemy | ^2.0.0 | ORM 框架 |

## 四、核心接口

### 4.1 公开接口

```python
from src.models import (
    Base, engine, SessionLocal, get_db,
    Blogger, Post, Prediction, Viewpoint, 
    FundHistory, FundInfo
)
```

### 4.2 数据模型说明

#### Blogger (博主表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| name | String(100) | 博主名称 |
| platform | String(50) | 平台 |
| description | Text | 描述 |
| accuracy_rate | Float | 准确率 |
| total_predictions | Integer | 总预测数 |
| correct_predictions | Integer | 正确预测数 |
| grade | String(5) | 等级 |
| recent_accuracy | Float | 近期准确率 |
| is_active | Boolean | 是否活跃 |
| created_at | DateTime | 创建时间 |

#### Post (帖子表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| blogger_id | Integer | 博主ID |
| title | String(500) | 标题 |
| content | Text | 内容 |
| post_date | Date | 发帖日期 |
| source_url | String(500) | 来源链接 |
| analyzed | Boolean | 是否已分析 |
| analysis_result | JSON | 分析结果 |
| auto_titled | Boolean | 是否自动生成标题 |
| created_at | DateTime | 创建时间 |

#### Prediction (预测表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| post_id | Integer | 帖子ID |
| blogger_id | Integer | 博主ID |
| fund_code | String(20) | 基金代码 |
| fund_name | String(100) | 基金名称 |
| sector | String(100) | 板块 |
| prediction_type | String(20) | 预测类型(bullish/bearish) |
| prediction_content | Text | 预测内容 |
| confidence | Integer | 置信度 |
| prediction_date | Date | 预测日期 |
| prediction_period | String(20) | 预测周期 |
| target_date | Date | 目标日期 |
| status | String(20) | 状态(pending/verified/expired) |
| start_nav | Float | 起始净值 |
| end_nav | Float | 结束净值 |
| actual_change | Float | 实际涨跌 |
| is_correct | Boolean | 是否正确 |
| verify_count | Integer | 验证次数 |
| is_expired | Boolean | 是否过期 |
| has_active_prediction | Boolean | 是否有活跃预测 |

#### Viewpoint (观点表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| blogger_id | Integer | 博主ID(可空) |
| post_id | Integer | 帖子ID |
| fund_code | String(20) | 基金代码 |
| content | Text | 内容 |
| author | String(100) | 作者 |
| source | String(50) | 来源(manual/crawler) |
| market_direction | String(20) | 市场方向 |
| confidence | Integer | 置信度 |
| time_horizon | String(20) | 时间周期 |
| validity_period | String(20) | 有效期 |
| valid_until | Date | 有效期截止 |

#### FundInfo (基金信息表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| fund_code | String(20) | 基金代码(唯一) |
| fund_name | String(100) | 基金名称 |
| fund_type | String(50) | 基金类型 |
| sector_type | String(50) | 板块类型 |
| latest_nav | Float | 最新净值 |
| nav_date | Date | 净值日期 |
| day_growth | Float | 日涨跌 |
| week_growth | Float | 周涨跌 |
| month_growth | Float | 月涨跌 |
| last_analyze_date | Date | 最后分析日期 |
| active_predictions | Integer | 活跃预测数 |
| can_delete | Boolean | 是否可删除 |

### 4.3 工具函数

```python
def init_db():
    """初始化数据库 - 创建所有表"""
    
def get_db():
    """获取数据库会话（生成器）"""
```

## 五、数据模型

### 5.1 数据库表关系

```
Blogger (博主)
    │
    ├── 1:N ── Post (帖子)
    │              │
    │              └── 1:N ── Prediction (预测)
    │                              │
    │                              └── 1:N ── VerificationTask (验证任务)
    │
    └── 1:N ── Viewpoint (观点)

FundInfo (基金信息)
    │
    ├── 1:N ── FundHistory (基金历史)
    │
    └── 1:N ── Prediction (预测)

SectorFundMapping (板块-基金映射)

InvestmentAdvice (投资建议)

PredictionGroup (预测组)
```

### 5.2 数据流向

```
API 请求 → get_db() → Session → Query/ORM → 数据库
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| DB_PATH | - | data/fund_insight.db | 数据库文件路径 |

## 七、使用示例

### 7.1 基本使用

```python
from sqlalchemy.orm import Session
from src.models import SessionLocal, Blogger, Post

# 创建会话
db: Session = SessionLocal()

try:
    # 查询所有博主
    bloggers = db.query(Blogger).all()
    
    # 创建新博主
    new_blogger = Blogger(
        name="测试博主",
        platform="xiaohongshu"
    )
    db.add(new_blogger)
    db.commit()
    db.refresh(new_blogger)
    
finally:
    db.close()
```

### 7.2 使用 get_db 依赖注入

```python
from fastapi import Depends
from src.models import get_db

@app.get("/bloggers")
def get_bloggers(db: Session = Depends(get_db)):
    return db.query(Blogger).all()
```

### 7.3 复杂查询示例

```python
# 查询博主及其预测
from sqlalchemy.orm import joinedload

blogger = db.query(Blogger).options(
    joinedload(Blogger.predictions)
).filter(Blogger.id == 1).first()
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_models.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| database.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 1.1.0 | 添加 PredictionGroup 表 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |
| 2026-03-10 | 1.2.0 | 移除 FundTrendAnalysis 和 FundTrendAnalysisHistory 表，移除 FundInfo.ai_trend 字段 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| 缺少外键约束 | 中 | 待定 |
| 模型未拆分到独立文件 | 低 | 待定 |
| 缺少数据迁移工具 | 中 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加外键级联删除 | 高 | 1人天 |
| 拆分模型到独立文件 | 中 | 1人天 |
| 添加 Alembic 数据迁移 | 中 | 2人天 |
| 添加模型验证方法 | 低 | 1人天 |
