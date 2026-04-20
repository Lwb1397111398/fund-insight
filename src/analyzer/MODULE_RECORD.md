# 模块记录文档 - Analyzer 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Analyzer (分析器模块) |
| **模块路径** | src/analyzer/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

提供基于 LLM 的智能分析服务，包括帖子内容分析、预测验证、投资建议生成、基金趋势分析等功能。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 帖子分析 | 分析帖子内容，提取预测观点 | 已实现 |
| 标题生成 | 根据内容自动生成标题 | 已实现 |
| 预测验证 | 智能验证预测准确性 | 已实现 |
| 投资建议 | 生成投资建议 | 已实现 |
| 基金趋势分析 | 分析基金走势 | 已实现 |
| 板块趋势分析 | 分析板块整体趋势 | 已实现 |
| 观点深度分析 | 对抓取观点进行深度分析 | 已实现 |
| 预测合并分析 | 合并分析多个预测 | 已实现 |
| 多模型策略 | 支持主模型和轻量级模型切换 | 已实现 |

### 1.3 边界定义

**包含：**
- LLM 调用封装
- 帖子内容分析
- 预测验证逻辑
- 投资建议生成
- 趋势分析
- 观点深度分析

**不包含：**
- 数据存储（由 services 负责）
- HTTP 接口（由 api 负责）
- 数据抓取（由 crawler 负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| llm_analyzer.py | LLM 分析器核心 | 958 | LLMAnalyzer, get_analyzer, reset_analyzer, merge_predictions_analysis |
| viewpoint_analyzer.py | 观点深度分析器 | 163 | ViewpointAnalyzer, get_viewpoint_analyzer |
| __init__.py | 模块导出 | 6 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| core | 直接导入 | config.LLM_* | 低 |
| openai | 第三方库 | OpenAI SDK | 中 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| api | 直接导入 | LLMAnalyzer, get_analyzer |
| crawler | 直接导入 | get_analyzer |
| fund | 直接导入 | get_analyzer |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| openai | ^1.0.0 | LLM API 调用 |

## 四、核心接口

### 4.1 公开接口

```python
from src.analyzer import LLMAnalyzer, get_analyzer
from src.analyzer.viewpoint_analyzer import ViewpointAnalyzer, get_viewpoint_analyzer
from src.analyzer.llm_analyzer import merge_predictions_analysis, reset_analyzer
```

### 4.2 接口说明

#### LLMAnalyzer 类

```python
class LLMAnalyzer:
    """LLM 分析器 - 支持多模型策略"""
    
    def generate_title(self, content: str, retry_count: int = 2) -> str:
        """根据帖子内容自动生成简短标题"""
        
    def analyze_post(self, title: str, content: str, post_date: str = None) -> Dict:
        """分析帖子内容，提取预测观点"""
        
    def verify_prediction(self, prediction_content: str, actual_change: float,
                          prediction_type: str, confidence: int) -> Dict:
        """智能验证预测结果"""
        
    def generate_investment_advice(self, bloggers: List[Dict], predictions: List[Dict],
                                    fund_trends: Dict = None, viewpoints: List[Dict] = None) -> Dict:
        """生成投资建议"""
        
    def analyze_fund_trend(self, fund_code: str, fund_name: str, history: List[Dict]) -> Dict:
        """分析基金趋势"""
        
    def analyze_fund_trend_detailed(self, fund_code: str, fund_name: str, history: List[Dict]) -> Dict:
        """详细分析基金趋势（用于预测验证）"""
        
    def analyze_sector_trend(self, sector_name: str, funds_data: List[Dict]) -> Dict:
        """分析整个板块的趋势"""
        
    def get_fund_for_sector(self, sector: str) -> Optional[Dict]:
        """根据板块名称获取对应基金"""
        
    def calculate_target_date(self, prediction_date: date, period: str) -> date:
        """根据预测周期计算目标验证日期"""
        
    def calculate_next_verify_date(self, prediction_date: date, target_date: date) -> date:
        """计算下次验证日期"""
```

#### ViewpointAnalyzer 类

```python
class ViewpointAnalyzer:
    """观点深度分析器"""
    
    def analyze_viewpoint(self, title: str, content: str, author: str = "", 
                          source: str = "") -> Dict:
        """对观点进行深度LLM分析"""
```

#### 工具函数

```python
def get_analyzer() -> LLMAnalyzer:
    """获取分析器单例（支持配置热更新）"""
    
def reset_analyzer():
    """重置分析器（用于配置更新后）"""
    
def merge_predictions_analysis(blogger_name: str, fund_code: str, fund_name: str,
                                predictions: List[Dict]) -> Dict:
    """合并分析同一博主对同一基金的多个预测"""
```

## 五、数据模型

### 5.1 使用的数据库表

无直接数据库操作

### 5.2 数据流向

```
调用方 → get_analyzer() → LLMAnalyzer → OpenAI API → 返回分析结果
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| LLM_API_KEY | LLM_API_KEY | "" | LLM API 密钥 |
| LLM_BASE_URL | LLM_BASE_URL | https://api.siliconflow.cn/v1 | LLM API 地址 |
| LLM_MODEL | LLM_MODEL | deepseek-ai/DeepSeek-V3.2 | 主模型 |
| LLM_LIGHT_MODEL | LLM_LIGHT_MODEL | Qwen/Qwen2.5-7B-Instruct | 轻量级模型 |
| LLM_STRATEGY | LLM_STRATEGY | auto | 模型选择策略 |

## 七、使用示例

### 7.1 基本使用

```python
from src.analyzer import get_analyzer

# 获取分析器实例
analyzer = get_analyzer()

# 分析帖子
result = analyzer.analyze_post(
    title="看好白酒板块反弹",
    content="白酒板块调整充分，看好后续反弹..."
)

# 提取预测
predictions = result.get("predictions", [])
viewpoint = result.get("viewpoint", {})
```

### 7.2 预测验证

```python
from src.analyzer import get_analyzer

analyzer = get_analyzer()

# 验证预测
verify_result = analyzer.verify_prediction(
    prediction_content="看好白酒板块反弹",
    actual_change=2.5,  # 实际涨跌 +2.5%
    prediction_type="up",
    confidence=70
)

is_correct = verify_result.get("is_correct", False)
```

### 7.3 生成投资建议

```python
from src.analyzer import get_analyzer

analyzer = get_analyzer()

# 生成投资建议
advice = analyzer.generate_investment_advice(
    bloggers=[{"name": "博主A", "accuracy_rate": 0.8}],
    predictions=[{"sector": "白酒", "prediction_type": "up"}],
    viewpoints=[{"market_direction": "bullish"}]
)

print(advice["advice_content"])
```

### 7.4 观点深度分析

```python
from src.analyzer.viewpoint_analyzer import get_viewpoint_analyzer

analyzer = get_viewpoint_analyzer()

result = analyzer.analyze_viewpoint(
    title="白酒板块迎来布局良机",
    content="经过调整后，白酒板块估值合理...",
    author="分析师A",
    source="eastmoney"
)

print(result["market_direction"])  # bullish/bearish/neutral
print(result["confidence"])  # 0-100
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_analyzer.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| llm_analyzer.py | 待测试 | 80% |
| viewpoint_analyzer.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 1.1.0 | 添加多模型策略 | Team |
| 2024-06-01 | 1.2.0 | 添加观点深度分析 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| LLM 调用可能超时 | 中 | 待定 |
| JSON 解析可能失败 | 低 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 添加重试机制 | 高 | 0.5人天 |
| 添加缓存机制 | 中 | 1人天 |
| 添加异步支持 | 中 | 2人天 |
| 添加成本统计 | 低 | 0.5人天 |
