# 模块记录 - Services

## 模块定位

`src/services/` 是业务逻辑层。API 层负责接收请求，服务层负责事务、查询、验证、统计、后台任务状态和跨模块协调。

## 当前职责

- 封装基础 CRUD。
- 管理博主、帖子、预测、基金、观点、建议。
- 执行预测验证、批量验证状态管理、基金更新状态管理。
- 处理板块资金流抓取、计算、幂等保存和抓取日志。
- 处理市场数据、观点统计、板块基金映射、测试数据清理。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `base.py` | 通用 `BaseService` |
| `blogger_service.py` | 博主查询、统计、准确率更新 |
| `post_service.py` | 帖子创建、标题生成、同步/异步分析、预测落库 |
| `prediction_service.py` | 预测查询、统计、状态管理 |
| `prediction_verify_service.py` | 预测验证核心，按净值和过程表现打分 |
| `prediction_verify_task.py` | 批量验证后台运行状态 |
| `fund_service.py` | 基金 CRUD、活跃预测计数、最近历史 |
| `fund_update_task.py` | 基金批量更新后台状态 |
| `viewpoint_service.py` | 观点查询、采纳、批量分析、汇总 |
| `viewpoint_stats_service.py` | 观点统计、告警、权重、关联 |
| `advice_service.py` | 投资建议生成和历史 |
| `crawler_service.py` | 爬虫采纳协调 |
| `sector_flow_service.py` | 板块资金流抓取、计算、写库、状态 |
| `sector_flow_calculator.py` | 板块资金流指标计算 |
| `sector_fund_service.py` | 板块-基金映射和审核 |
| `market_data_service.py` | 市场、政策、情绪辅助数据 |
| `stats_service.py` | 总体统计 |
| `test_data_cleanup_service.py` | 测试数据识别和清理 |

## 核心流

### 帖子分析

`PostService` 创建帖子后可调用 LLM 分析，生成 `Prediction`。失败时必须保持帖子状态、分析结果和预测数量一致。

### 预测验证

`PredictionVerifyService` 是准确率核心，读取 `FundHistory`，写回 `Prediction`，并影响 `Blogger` 分数。它包含净值缓存、过程验证、震荡阈值和过期补救逻辑。

### 板块资金流

`SectorFlowService.run_fetch()` 调用 `SectorFlowCrawler`，计算暗盘和主力强度，按日期、板块代码和类型幂等写入 `SectorFundFlow`，并记录 `SectorFlowFetchRun`。

## 高风险点

- `prediction_verify_service.py` 改动会直接改变准确率和历史判断。
- `post_service.py` 改动会影响帖子分析和预测创建事务。
- `sector_flow_service.py` 改动会影响抓取幂等、运行日志和前端排行。
- 后台状态对象是进程内状态，多进程部署不能当作分布式锁。

## 推荐验证

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_prediction_verify_process_metrics.py -v
pytest tests/unit/test_sector_flow_service.py -v
pytest tests/unit/test_fund_update_task.py -v
pytest tests/unit/test_services/test_services.py -v
```
