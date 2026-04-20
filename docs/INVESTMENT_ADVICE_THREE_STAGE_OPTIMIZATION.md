# 投资建议三阶段分析优化报告

## 功能概述
- **功能名称**: 投资建议三阶段分析
- **开发者**: developer
- **日期**: 2026-03-08

## 1. 功能说明

### 1.1 功能描述
优化投资建议生成流程，从单阶段直接生成改为三阶段分析：
1. **第一阶段**：分析观点，生成观点摘要
2. **第二阶段**：分析预测，结合观点摘要生成预测分析报告
3. **第三阶段**：基于观点摘要和预测分析，生成投资建议

同时优化预测获取逻辑，按目标日期筛选而非预测日期。

### 1.2 实现方案

#### 预测获取优化
**修改前**：
```python
# 按预测日期筛选（博主最近30天做出的预测）
predictions = db.query(Prediction).filter(
    Prediction.prediction_date >= date.today() - timedelta(days=30)
)
```

**修改后**：
```python
# 按目标日期筛选（即将到期的预测）
near_term = db.query(Prediction).filter(
    Prediction.target_date >= date.today(),
    Prediction.target_date <= date.today() + timedelta(days=7)
)
mid_term = db.query(Prediction).filter(
    Prediction.target_date > date.today() + timedelta(days=7),
    Prediction.target_date <= date.today() + timedelta(days=30)
)
```

#### 三阶段分析流程

**第一阶段：观点分析**
- 输入：最近7天的所有观点（不限制数量）
- 输出：观点摘要报告
- 内容：市场情绪、热门板块、风险板块、关键观点

**第二阶段：预测分析**
- 输入：观点摘要 + 预测数据（近期+中期）
- 输出：预测分析报告
- 内容：近期/中期趋势、高置信度预测、板块预测分布

**第三阶段：投资建议**
- 输入：观点摘要 + 预测分析 + 博主信息
- 输出：投资建议
- 内容：建议类型、具体建议、市场情绪、操作建议

## 2. 代码清单

### 2.1 新增方法

| 文件路径 | 方法名 | 说明 |
|---------|--------|------|
| src/analyzer/llm_analyzer.py | analyze_viewpoints_stage1 | 第一阶段：观点分析 |
| src/analyzer/llm_analyzer.py | analyze_predictions_stage2 | 第二阶段：预测分析 |
| src/analyzer/llm_analyzer.py | generate_advice_stage3 | 第三阶段：投资建议生成 |
| src/analyzer/llm_analyzer.py | generate_investment_advice_three_stage | 三阶段分析主方法 |

### 2.2 修改文件

| 文件路径 | 修改内容 |
|---------|---------|
| src/services/advice_service.py | 修改预测获取逻辑（按目标日期） |
| src/services/advice_service.py | 修改观点获取逻辑（不限制数量） |
| src/api/routes/advice.py | 调用三阶段分析方法 |

## 3. 接口说明

### 3.1 API 接口

**POST /api/advice**

**请求参数**：
```json
{
  "date": "2026-03-08",
  "force": true
}
```

**返回结果**：
```json
{
  "success": true,
  "message": "投资建议生成成功（三阶段分析）",
  "data": {
    "advice_type": "buy",
    "advice_content": "建议关注科技板块...",
    "market_sentiment": "neutral",
    "confidence": 75,
    "suggested_sectors": ["科技", "医药"],
    "avoid_sectors": ["房地产"],
    "reasoning": "...",
    "risk_warning": "...",
    "viewpoint_summary": {
      "summary": "市场整体观点摘要",
      "market_sentiment": "bullish",
      "hot_sectors": ["科技", "医药"],
      "risk_sectors": ["房地产"],
      "key_points": ["关键点1", "关键点2"]
    },
    "prediction_analysis": {
      "summary": "预测整体分析摘要",
      "near_term_trend": "bullish",
      "mid_term_trend": "neutral",
      "high_confidence_predictions": [...],
      "sector_predictions": {...}
    }
  }
}
```

## 4. 优化对比

### 4.1 预测获取逻辑对比

| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| 筛选依据 | prediction_date（预测日期） | target_date（目标日期） |
| 时间范围 | 过去30天 | 未来7天（近期）+ 8-30天（中期） |
| 关注点 | 博主最近说了什么 | 即将发生什么 |
| 投资建议价值 | 中 | 高 |

### 4.2 分析流程对比

| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| 分析阶段 | 1阶段 | 3阶段 |
| 观点数量 | 限制20条 | 不限制 |
| 预测数量 | 限制30条 | 不限制 |
| 分析深度 | 中 | 高 |
| LLM调用次数 | 1次 | 3次 |
| 响应时间 | 5-8秒 | 15-20秒 |
| 中间结果 | 无 | 保存观点摘要和预测分析 |

## 5. 使用说明

### 5.1 生成投资建议

1. 打开基金洞察系统：http://localhost:8013
2. 点击"💰 投资建议"按钮
3. 系统自动进行三阶段分析：
   - 第一阶段：分析观点（约5秒）
   - 第二阶段：分析预测（约5秒）
   - 第三阶段：生成建议（约5秒）
4. 查看投资建议和中间分析结果

### 5.2 查看中间结果

投资建议返回结果中包含：
- `viewpoint_summary`：观点摘要报告
- `prediction_analysis`：预测分析报告

## 6. 注意事项

### 6.1 性能考虑
- 三阶段分析需要3次LLM调用，响应时间约15-20秒
- 建议在前端添加"分析中..."的提示
- 可以考虑缓存中间结果

### 6.2 成本考虑
- 每次生成投资建议消耗3次LLM调用
- 建议设置合理的生成频率限制

### 6.3 后续优化

1. **缓存中间结果**：
   - 观点摘要可以缓存1小时
   - 预测分析可以缓存30分钟

2. **并行处理**：
   - 第一阶段和第二阶段可以并行执行
   - 进一步优化响应时间

3. **增量分析**：
   - 只分析新增的观点和预测
   - 减少重复分析

---

**开发状态**: ✅ **已完成**
**服务器状态**: ✅ **运行中** (http://localhost:8013)
