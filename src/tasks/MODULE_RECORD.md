# 模块记录文档 - Tasks 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Tasks (定时任务模块) |
| **模块路径** | src/tasks/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

提供定时任务调度和执行功能，包括过期数据清理、趋势分析等后台任务。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 任务调度 | 定时执行后台任务 | 已实现 |
| 过期预测清理 | 清理过期的预测记录 | 已实现 |
| 过期观点清理 | 清理过期的观点记录 | 已实现 |
| 空帖子清理 | 清理没有预测的空帖子 | 已实现 |
| 基金趋势分析 | 定期分析基金趋势 | 已实现 |

### 1.3 边界定义

**包含：**
- 任务调度器
- 清理任务
- 趋势分析任务

**不包含：**
- HTTP 接口（由 api 负责）
- 业务逻辑（由各服务模块负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| scheduler.py | 任务调度器 | ~150 | TaskScheduler |
| cleanup_tasks.py | 清理任务 | ~150 | CleanupManager, run_cleanup_task |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| models | 直接导入 | Prediction, Viewpoint, Post, get_db | 中 |
| analyzer | 直接导入 | get_local_trend_analyzer | 低 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| __main__.py | 直接导入 | TaskScheduler |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| threading | 内置 | 多线程 |
| logging | 内置 | 日志 |

## 四、核心接口

### 4.1 公开接口

```python
from src.tasks.scheduler import TaskScheduler
from src.tasks.cleanup_tasks import CleanupManager, run_cleanup_task
```

### 4.2 接口说明

#### TaskScheduler 类

```python
class TaskScheduler:
    """任务调度器"""
    
    def __init__(self):
        self.running = False
        self.thread = None
        self.cleanup_interval_hours = 24  # 每天运行一次
    
    def start(self):
        """启动调度器"""
        
    def stop(self):
        """停止调度器"""
        
    def _run_scheduler(self):
        """运行调度循环"""
        
    def _run_cleanup(self):
        """执行清理任务"""
        
    def _run_fund_trend_analysis(self):
        """执行基金趋势分析任务"""
```

#### CleanupManager 类

```python
class CleanupManager:
    """清理管理器"""
    
    def cleanup_expired_predictions(self) -> dict:
        """清理过期的预测（target_date + 7天后自动删除）"""
        
    def cleanup_expired_viewpoints(self) -> dict:
        """清理过期的观点（valid_until + 7天后自动删除）"""
        
    def _cleanup_empty_posts(self, affected_posts: set) -> int:
        """清理没有预测的空帖子"""
```

#### 工具函数

```python
def run_cleanup_task() -> dict:
    """运行清理任务（入口函数）"""
```

## 五、数据模型

### 5.1 使用的数据库表

| 表名 | 用途 | 访问模式 |
|------|------|----------|
| predictions | 预测记录 | 删除 |
| viewpoints | 观点记录 | 删除 |
| posts | 帖子记录 | 删除 |
| fund_trend_analysis | 趋势分析 | 写 |

### 5.2 清理规则

| 数据类型 | 清理规则 | 说明 |
|----------|----------|------|
| 预测 | target_date + 7天 | 预测目标日期后7天删除 |
| 观点 | valid_until + 7天 | 观点有效期后7天删除 |
| 空帖子 | 无预测关联 | 删除没有预测的帖子 |

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| cleanup_interval_hours | - | 24 | 清理间隔(小时) |

## 七、使用示例

### 7.1 启动调度器

```python
from src.tasks.scheduler import TaskScheduler

# 创建调度器
scheduler = TaskScheduler()

# 启动调度器（后台线程运行）
scheduler.start()

# 停止调度器
scheduler.stop()
```

### 7.2 手动执行清理

```python
from src.tasks.cleanup_tasks import run_cleanup_task

# 执行清理任务
result = run_cleanup_task()

print(f"删除预测: {result['predictions']['deleted_predictions']}")
print(f"删除观点: {result['viewpoints']['deleted_viewpoints']}")
```

### 7.3 使用清理管理器

```python
from src.tasks.cleanup_tasks import CleanupManager

manager = CleanupManager()

# 清理过期预测
result = manager.cleanup_expired_predictions()
print(f"删除了 {result['deleted_predictions']} 个预测")

# 清理过期观点
result = manager.cleanup_expired_viewpoints()
print(f"删除了 {result['deleted_viewpoints']} 个观点")
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_tasks.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| scheduler.py | 待测试 | 80% |
| cleanup_tasks.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 1.1.0 | 添加趋势分析任务 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| 缺少任务执行日志持久化 | 低 | 待定 |
| 缺少任务失败重试机制 | 中 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加任务执行日志 | 中 | 0.5人天 |
| 添加失败重试机制 | 中 | 1人天 |
| 添加任务执行通知 | 低 | 1人天 |
