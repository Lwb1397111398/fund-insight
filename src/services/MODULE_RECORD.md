# 模块记录文档 - Services 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Services (服务层模块) |
| **模块路径** | src/services/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2026-03-07 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

封装业务逻辑，提供统一的服务接口，隔离 API 层与数据层的直接耦合，实现关注点分离。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 基础 CRUD | 通用增删改查操作 | 已实现 |
| 博主服务 | 博主相关业务逻辑 | 已实现 |
| 帖子服务 | 帖子相关业务逻辑 | 已实现 |
| 预测服务 | 预测相关业务逻辑 | 已实现 |
| 基金服务 | 基金相关业务逻辑 | 已实现 |
| 观点服务 | 观点相关业务逻辑 | 已实现 |

### 1.3 边界定义

**包含：**
- 业务逻辑封装
- 数据访问抽象
- 跨模块协调
- 数据转换

**不包含：**
- HTTP 请求处理（由 api 负责）
- 数据模型定义（由 models 负责）
- LLM 分析（由 analyzer 负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| base.py | 基础服务类 | ~100 | BaseService |
| blogger_service.py | 博主服务 | ~150 | BloggerService |
| post_service.py | 帖子服务 | ~130 | PostService |
| prediction_service.py | 预测服务 | ~180 | PredictionService |
| fund_service.py | 基金服务 | ~180 | FundService |
| viewpoint_service.py | 观点服务 | ~180 | ViewpointService |
| __init__.py | 模块导出 | 15 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| models | 直接导入 | 所有模型类 | 中 |
| analyzer | 可选导入 | get_analyzer | 低 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| api | 直接导入 | 所有服务类 |

## 四、核心接口

### 4.1 公开接口

```python
from src.services import (
    BaseService,
    BloggerService,
    PostService,
    PredictionService,
    FundService,
    ViewpointService,
)
```

### 4.2 接口说明

#### BaseService 基类

```python
class BaseService(Generic[ModelType]):
    """基础服务类"""
    
    def __init__(self, db: Session, model: Type[ModelType]):
        """初始化"""
        
    def get(self, id: int) -> Optional[ModelType]:
        """根据 ID 获取单个记录"""
        
    def get_all(self, skip: int = 0, limit: int = 100) -> List[ModelType]:
        """获取所有记录（分页）"""
        
    def create(self, obj_in: dict) -> ModelType:
        """创建新记录"""
        
    def update(self, id: int, obj_in: dict) -> Optional[ModelType]:
        """更新记录"""
        
    def delete(self, id: int) -> bool:
        """删除记录"""
        
    def count(self) -> int:
        """获取记录总数"""
        
    def exists(self, id: int) -> bool:
        """检查记录是否存在"""
```

#### BloggerService

```python
class BloggerService(BaseService[Blogger]):
    """博主服务"""
    
    def get_by_name(self, name: str) -> Optional[Blogger]:
        """根据名称获取博主"""
        
    def get_by_platform(self, platform: str) -> List[Blogger]:
        """根据平台获取博主列表"""
        
    def get_active_bloggers(self) -> List[Blogger]:
        """获取活跃博主列表"""
        
    def get_top_bloggers(self, limit: int = 10) -> List[Blogger]:
        """获取准确率最高的博主"""
        
    def get_with_stats(self, blogger_id: int) -> Optional[Dict]:
        """获取博主及其统计数据"""
        
    def update_accuracy(self, blogger_id: int) -> Optional[Blogger]:
        """更新博主准确率"""
```

#### PredictionService

```python
class PredictionService(BaseService[Prediction]):
    """预测服务"""
    
    def get_by_blogger(self, blogger_id: int) -> List[Prediction]:
        """获取博主的预测列表"""
        
    def get_by_fund(self, fund_code: str) -> List[Prediction]:
        """获取基金的预测列表"""
        
    def get_active(self) -> List[Prediction]:
        """获取活跃预测"""
        
    def get_pending_verification(self, days: int = 7) -> List[Prediction]:
        """获取待验证的预测"""
        
    def verify(self, prediction_id: int, actual_change: float, 
               is_correct: bool) -> Optional[Prediction]:
        """验证预测"""
        
    def get_stats(self, blogger_id: int = None) -> Dict:
        """获取预测统计"""
```

## 五、使用示例

### 5.1 基本使用

```python
from sqlalchemy.orm import Session
from src.models.database import SessionLocal
from src.services import BloggerService, PostService

# 创建数据库会话
db: Session = SessionLocal()

# 创建服务实例
blogger_service = BloggerService(db)
post_service = PostService(db)

# 获取博主
blogger = blogger_service.get(1)

# 获取博主的帖子
posts = post_service.get_by_blogger(blogger.id)

# 关闭会话
db.close()
```

### 5.2 依赖注入方式

```python
from fastapi import Depends
from sqlalchemy.orm import Session
from src.models.database import get_db
from src.services import BloggerService

@app.get("/bloggers/{blogger_id}")
def get_blogger(
    blogger_id: int,
    db: Session = Depends(get_db)
):
    service = BloggerService(db)
    return service.get(blogger_id)
```

### 5.3 创建记录

```python
from src.services import PostService

service = PostService(db)

# 创建帖子
post = service.create({
    "blogger_id": 1,
    "title": "看好白酒板块",
    "content": "白酒板块调整充分...",
    "post_date": date.today()
})
```

### 5.4 更新记录

```python
from src.services import PredictionService

service = PredictionService(db)

# 验证预测
prediction = service.verify(
    prediction_id=1,
    actual_change=2.5,
    is_correct=True,
    ai_judgment="预测正确，实际涨幅2.5%"
)
```

## 六、测试指南

### 6.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_services.py -v
```

### 6.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| base.py | 待测试 | 80% |
| blogger_service.py | 待测试 | 80% |
| post_service.py | 待测试 | 80% |
| prediction_service.py | 待测试 | 80% |
| fund_service.py | 待测试 | 80% |
| viewpoint_service.py | 待测试 | 80% |

## 七、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2026-03-07 | 1.0.0 | 初始版本，创建服务层 | Agent |

## 八、已知问题与改进计划

### 8.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| 无 | - | - |

### 8.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加事务管理 | 中 | 0.5人天 |
| 添加缓存支持 | 低 | 1人天 |
| 添加异步支持 | 低 | 2人天 |
