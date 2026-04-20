# 模块记录文档 - Fund 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Fund (基金模块) |
| **模块路径** | src/fund/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

提供基金数据获取、管理、同步和分析功能，包括基金信息抓取、历史净值存储、智能基金匹配和趋势分析。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 基金信息获取 | 从天天基金获取实时数据 | 已实现 |
| 历史净值获取 | 获取基金历史净值数据 | 已实现 |
| 基金搜索 | 搜索基金代码和名称 | 已实现 |
| 智能基金匹配 | 根据板块自动匹配基金 | 已实现 |
| 板块分类 | 智能分类基金到板块 | 已实现 |
| 数据同步 | 同步基金数据到数据库 | 已实现 |
| 趋势分析 | 分析基金走势趋势 | 已实现 |

### 1.3 边界定义

**包含：**
- 基金 API 封装
- 基金数据管理
- 智能基金匹配
- 数据同步
- 趋势分析

**不包含：**
- HTTP 接口（由 api 负责）
- LLM 分析（由 analyzer 负责）
- 数据模型定义（由 models 负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| fund_api.py | 基金 API 封装 | ~200 | FundAPI, FundDataManager, fund_api, fund_data_manager |
| fund_auto_manager.py | 智能基金自动管理 | ~150 | FundAutoManager |
| fund_sync_manager.py | 基金数据同步 | ~100 | FundSyncManager |
| technical_analyzer.py | 技术指标分析 | ~400 | TechnicalIndicatorCalculator, RelativePerformanceAnalyzer |
| __init__.py | 模块导出 | 6 | - |

**注意：** `trend_analyzer.py` 已被移除，趋势分析功能已迁移至 `src/analyzer/local_trend_analyzer.py`

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| core | 直接导入 | config | 低 |
| models | 直接导入 | FundInfo, FundHistory, SessionLocal | 中 |
| analyzer | 直接导入 | get_analyzer | 中 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| api | 直接导入 | fund_api, fund_data_manager |
| tasks | 直接导入 | fund_data_manager |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| requests | ^2.28.0 | HTTP 请求 |

## 四、核心接口

### 4.1 公开接口

```python
from src.fund import FundAPI, FundDataManager, fund_api, fund_data_manager
```

### 4.2 接口说明

#### FundAPI 类

```python
class FundAPI:
    """天天基金 API 封装"""
    
    def get_fund_info(self, fund_code: str) -> Optional[Dict]:
        """获取基金实时信息"""
        
    def get_fund_history(self, fund_code: str, days: int = 30) -> List[Dict]:
        """获取基金历史净值"""
        
    def search_fund(self, keyword: str, max_results: int = 10) -> List[Dict]:
        """搜索基金"""
```

#### FundDataManager 类

```python
class FundDataManager:
    """基金数据管理器"""
    
    def update_fund_info(self, db: Session, fund_code: str) -> Optional[FundInfo]:
        """更新基金信息"""
        
    def get_fund_by_code(self, db: Session, fund_code: str) -> Optional[FundInfo]:
        """根据代码获取基金"""
        
    def get_all_funds(self, db: Session) -> List[FundInfo]:
        """获取所有基金"""
        
    def sync_fund_history(self, db: Session, fund_code: str, days: int = 30) -> List[FundHistory]:
        """同步基金历史净值"""
```

#### FundAutoManager 类

```python
class FundAutoManager:
    """智能基金自动管理器"""
    
    def get_category_for_sector(self, sector: str) -> str:
        """获取板块所属的标准分类"""
        
    def get_fund_for_sector(self, sector: str) -> Optional[Dict]:
        """根据板块获取对应基金"""
        
    def auto_add_fund_for_sector(self, db: Session, sector: str) -> Optional[FundInfo]:
        """自动添加板块对应的基金"""
```

## 五、数据模型

### 5.1 使用的数据库表

| 表名 | 用途 | 访问模式 |
|------|------|----------|
| fund_info | 基金信息 | 读/写 |
| fund_history | 基金历史净值 | 读/写 |

### 5.2 数据流向

```
天天基金 API → FundAPI → FundDataManager → 数据库
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| CRAWLER_TIMEOUT | CRAWLER_TIMEOUT | 10 | 请求超时(秒) |

## 七、使用示例

### 7.1 基本使用

```python
from src.fund import fund_api, fund_data_manager
from src.models import SessionLocal

# 获取基金信息
info = fund_api.get_fund_info('000001')
print(info['fund_name'])

# 获取历史净值
history = fund_api.get_fund_history('000001', days=30)

# 更新数据库
db = SessionLocal()
fund = fund_data_manager.update_fund_info(db, '000001')
db.close()
```

### 7.2 智能匹配

```python
from src.fund.fund_auto_manager import FundAutoManager

manager = FundAutoManager()

# 获取板块对应的基金
fund = manager.get_fund_for_sector('白酒')
print(fund['code'], fund['name'])

# 获取板块分类
category = manager.get_category_for_sector('半导体')
print(category)  # '科技'
```

### 7.3 趋势分析

```python
from src.analyzer.local_trend_analyzer import get_local_trend_analyzer

analyzer = get_local_trend_analyzer()

# 分析趋势（传入历史净值数据）
history = [{"date": "2024-01-01", "nav": 1.0, "day_growth": 0.01}, ...]
trend = analyzer.analyze_trend(history, max_periods=5)
print(trend['trend_summary'])  # 趋势总结
print(trend['periods'])  # 各阶段分析
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_fund.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| fund_api.py | 待测试 | 80% |
| fund_auto_manager.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 1.1.0 | 添加智能基金匹配 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| API 可能超时 | 中 | 待定 |
| 缺少缓存机制 | 低 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加缓存机制 | 中 | 1人天 |
| 添加异步支持 | 低 | 2人天 |
| 添加更多数据源 | 低 | 2人天 |
