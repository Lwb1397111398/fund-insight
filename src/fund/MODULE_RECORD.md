# 模块记录 - Fund

## 模块定位

`src/fund/` 负责基金数据获取、同步、质量校验、智能匹配和技术指标。它为预测验证、基金详情、投资建议和板块映射提供基金基础数据。

## 当前职责

- 从天天基金等来源获取基金信息和历史净值。
- 同步基金信息和历史到数据库。
- 根据板块自动匹配或管理基金。
- 计算技术指标和相对表现。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `fund_api.py` | 天天基金 API、`FundDataManager`、基金信息/历史更新 |
| `fund_sync_manager.py` | 批量同步基金数据 |
| `fund_auto_manager.py` | 板块到基金的自动管理 |
| `technical_analyzer.py` | 技术指标和相对表现分析 |

## 数据流

```text
外部基金 API
  -> FundAPI / FundDataManager
  -> FundSyncManager
  -> FundInfo / FundHistory
  -> PredictionVerifyService / Funds API / AdviceService
```

## 高风险点

- `FundHistory` 是预测验证基础，净值日期和涨跌幅错误会直接影响准确率。
- 外部 API 可能返回空值、异常值或日期不一致，必须保留校验。
- 批量更新应逐个基金提交，避免一个失败回滚全部。
- `active_predictions` 与 `can_delete` 需要和预测创建/删除保持一致。

## 推荐验证

```bash
pytest tests/unit/test_fund_sync_manager.py -v
pytest tests/unit/test_fund_update_task.py -v
pytest tests/unit/test_fund_history_fix.py -v
pytest tests/unit/test_fund_recent_history.py -v
```
