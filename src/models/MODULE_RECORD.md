# 模块记录 - Models

## 模块定位

`src/models/database.py` 集中定义数据库连接、会话和所有 SQLAlchemy ORM 模型。它同时支持本地 SQLite 和生产 PostgreSQL/Supabase。

## 当前职责

- 根据 `DATABASE_URL` 选择 PostgreSQL 或 SQLite。
- 配置 PostgreSQL 连接池和 SSL keepalive。
- 为 SQLite 启用外键约束。
- 提供 `Base`、`engine`、`SessionLocal`、`init_db()`。
- 定义项目全部 ORM 表模型。

## 数据库选择

- `DATABASE_URL` 以 `postgresql` 开头：使用 PostgreSQL。
- 未设置或驱动不可用：回退 SQLite `data/fund_insight.db`。
- `SessionLocal` 使用 `_RetrySession`，对部分 PostgreSQL SSL 断开做有限重试。

## 核心模型分组

| 分组 | 模型 |
| --- | --- |
| 博主/帖子/预测 | `Blogger`、`Post`、`Prediction`、`VerificationTask`、`PredictionGroup` |
| 基金 | `FundInfo`、`FundHistory`、`FundHolding`、`FundSyncRetry`、`SyncLog` |
| 观点/爬虫 | `Viewpoint`、`CrawlerArticleRecord` |
| 板块映射 | `SectorFundMapping`、`SectorAlias`、`UserFundBinding` |
| 投资建议 | `InvestmentAdvice`、`AdviceReasoning`、`AdvicePerformance`、`AdviceFeedback` |
| 批量分析 | `BatchAnalysisTask`、`AnalysisLog` |
| 清理 | `CleanupLog`、`CleanupItemLog`、`CleanupTask`、`CleanupRule`、`CleanupSchedule` |
| 市场数据 | `MarketData`、`PolicyData`、`SentimentData`、`MarketEvent`、`SectorFundFlow`、`SectorFlowFetchRun` |
| 用户/配置 | `UserProfile`、`SystemConfig` |

## 关键表关系

```text
Blogger 1 -> N Post
Blogger 1 -> N Prediction
Post    1 -> N Prediction
FundInfo(fund_code) -> FundHistory(fund_code)
Viewpoint 可关联 Blogger/Post/Fund
SectorFundFlow 记录按日期和板块的资金流
SectorFlowFetchRun 记录每次抓取运行状态
InvestmentAdvice 可引用 bloggers/predictions/viewpoints 数据
```

## 高风险点

- 项目没有 Alembic。新增/修改字段后，生产 Supabase 需要手动迁移或专门脚本。
- SQLite 和 PostgreSQL 类型行为不同，尤其是 JSON、DateTime、Numeric、外键约束。
- `Base.metadata.create_all()` 只会建不存在的表，不会完整迁移已有表。
- 改索引、唯一约束或外键前必须评估现有数据。

## 推荐验证

```bash
python -m src --init-db
pytest tests/unit/test_production_hardening.py -v
pytest tests/unit/test_deployment_optimization.py -v
```
