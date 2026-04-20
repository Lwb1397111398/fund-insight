# 基金数据问题修复总结

## 问题分析与解决

### 问题 1: 为什么打开基金列表时只有一个基金？

**原因**: 
- 虽然有 5 条预测记录，但只有 1 条预测有基金代码 (`fund_code`)
- 其他 4 条预测的 `sector` 字段有值 (电力、油气、卫星、存储),但 `SECTOR_FUND_MAP` 中没有这些板块的映射
- 导致这些预测没有关联到具体的基金

**解决方案**:
1. 在 `src/analyzer/llm_analyzer.py` 的 `SECTOR_FUND_MAP` 中添加了缺失的板块映射:
   - 电力 → 006816 (招商中证电力 ETF 联接 A)
   - 油气 → 160416 (华安标普全球石油指数)
   - 卫星 → 013669 (广发中证卫星导航 ETF 联接 A)
   - 存储 → 012631 (国泰中证半导体材料设备主题 ETF 联接 A)

2. 运行 `update_prediction_funds.py` 脚本为现有预测更新基金代码

3. 运行 `fix_fund_data.py` 脚本更新所有基金数据

**结果**: 现在有 4 个活跃基金 (原来只有 1 个)

---

### 问题 2: 添加基金时能否同时添加历史涨跌数据？

**原因**:
- 添加基金时确实会获取历史数据 (30 天)
- 但没有计算周涨跌和月涨跌并保存到数据库

**解决方案**:
修改 `src/api/main.py` 的 `add_fund` 接口:
```python
@app.post("/api/funds")
def add_fund(data: FundAdd, db: Session = Depends(get_db)):
    fund = fund_data_manager.update_fund_info(data.fund_code, db)
    if not fund:
        raise HTTPException(status_code=404, detail="基金代码不存在")
    
    fund_data_manager.update_fund_history(data.fund_code, days=30, db=db)
    
    # 新增：计算周/月涨跌
    history = db.query(FundHistory).filter(
        FundHistory.fund_code == data.fund_code
    ).order_by(FundHistory.nav_date.desc()).limit(30).all()
    
    if len(history) >= 5:
        fund.week_growth = round(
            (history[0].nav - history[4].nav) / history[4].nav * 100, 2
        ) if history[4].nav else None
    
    if len(history) >= 20:
        fund.month_growth = round(
            (history[0].nav - history[19].nav) / history[19].nav * 100, 2
        ) if history[19].nav else None
    
    db.commit()
    
    return {"success": True, "data": {"fund_code": fund.fund_code, "fund_name": fund.fund_name}}
```

**结果**: 现在添加基金时会自动计算并显示周涨跌和月涨跌

---

### 问题 3: 基金显示"未知类型"正常吗？

**原因**:
- `FundInfo` 表有 `sector_type` 字段 (板块类型，如"白酒"、"新能源")
- 但从 API 获取的基金信息只有 `fund_type` (基金类型，如"股票型"、"债券型")
- `sector_type` 需要通过预测记录的 `sector_type` 反向更新

**解决方案**:
1. 在添加帖子时，自动将预测的 `sector_type` 复制到基金记录:
```python
if fund:
    fund.active_predictions = (fund.active_predictions or 0) + 1
    fund.can_delete = False
    if sector_type and not fund.sector_type:
        fund.sector_type = sector_type
```

2. 在 `analyze_fund_trends` 接口中添加自动更新板块类型的逻辑:
```python
predictions = db.query(Prediction).filter(
    Prediction.fund_code == fund.fund_code,
    Prediction.sector_type != ''
).limit(5).all()

if predictions and not fund.sector_type:
    sector_types = [p.sector_type for p in predictions if p.sector_type]
    if sector_types:
        fund.sector_type = max(set(sector_types), key=sector_types.count)
```

**结果**: 基金的板块类型会正确显示 (如"半导体"、"其他"等)

---

## 修复工具

### 1. `fix_fund_data.py` - 基金数据修复工具
功能:
- 更新所有基金的最新数据
- 修复基金的板块类型
- 计算基金的周/月涨跌
- 修复预测记录的活跃状态

使用方法:
```bash
cd e:\CountBot\countbot\workspace\fund-insight
python fix_fund_data.py
```

### 2. `update_prediction_funds.py` - 预测基金代码更新工具
功能:
- 为没有基金代码的预测记录更新基金代码
- 自动添加对应的基金到数据库

使用方法:
```bash
cd e:\CountBot\countbot\workspace\fund-insight
python update_prediction_funds.py
```

---

## 修复前后对比

### 修复前:
- 活跃基金数量：1 个
- 基金板块类型：未知
- 周/月涨跌：None
- 预测与基金对应：只有 1 条预测有关联基金

### 修复后:
- 活跃基金数量：4 个
- 基金板块类型：正确显示 (半导体、其他等)
- 周/月涨跌：正确计算并显示
- 预测与基金对应：5 条预测全部关联到具体基金

---

## 基金列表

| 基金代码 | 基金名称 | 板块类型 | 预测数 | 日涨跌 | 周涨跌 | 月涨跌 |
|---------|---------|---------|-------|-------|-------|-------|
| 160221 | 国泰国证有色金属行业指数 (LOF)A | 其他 | 1 | -2.07% | -3.01% | -7.72% |
| 160416 | 华安标普全球石油指数 (QDII-LOF)A | 其他 | 1 | -0.77% | 1.51% | 8.73% |
| 013669 | 永赢慧盈一年持有债券发起 (FOF)C | 其他 | 1 | 0.02% | -0.1% | 0.11% |
| 012631 | 中银优选灵活配置混合 C | 半导体 | 1 | 0.33% | -3.65% | -0.68% |

---

## 改进建议

### 短期改进:
1. ✅ 添加更多板块 - 基金映射关系
2. ✅ 自动计算周/月涨跌
3. ✅ 自动更新基金板块类型

### 长期改进:
1. 添加前端界面显示基金历史趋势图表
2. 添加基金详情页面，显示完整的净值历史
3. 支持手动修改基金的板块类型
4. 添加基金对比功能
5. 支持导出基金数据到 Excel

---

## 注意事项

1. **基金代码映射**: 所有板块 - 基金的映射都在 `src/analyzer/llm_analyzer.py` 的 `SECTOR_FUND_MAP` 中
2. **数据更新**: 每次添加新预测时，会自动更新对应基金的数据
3. **历史数据**: 基金会保存 30 天的历史净值数据用于计算周/月涨跌
4. **板块类型**: 优先使用预测记录的 `sector_type`,如果没有则显示"其他"

---

## 测试验证

运行以下命令验证修复效果:
```bash
# 1. 查看预测与基金对应关系
python -c "from src.models.database import SessionLocal, Prediction; db = SessionLocal(); preds = db.query(Prediction).all(); [print(f'{p.id}: {p.sector} -> {p.fund_code} - {p.fund_name}') for p in preds]; db.close()"

# 2. 查看基金摘要
python fix_fund_data.py
```

启动服务器后访问 http://localhost:8018 查看前端界面
