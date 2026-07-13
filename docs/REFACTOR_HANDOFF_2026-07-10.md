# Fund Insight 重构接手说明（2026-07-10）

本文记录本轮全面审查和稳健重构的实际结果。后续维护者应先阅读
`AGENTS.md`、`docs/ARCHITECTURE.md`、`docs/PRODUCT.md`，再修改高风险文件。

## 数据安全底线

- 未向 Render、Supabase 或任何生产数据库执行写入、清空、迁移或恢复操作。
- 真实备份 `E:/暂时压缩/fund_insight_export_2026-07-09.json` 仅用于本地临时 SQLite 验收，未复制或提交到仓库。
- `fund_insight_export_*.json` 已加入 `.gitignore`，防止日后把导出数据误提交。
- 数据删除默认关闭：`ENABLE_DATA_CLEANUP=false`、`ENABLE_TEST_DATA_CLEANUP=false`。即使显式启用，清理 API 仍要求 `X-Danger-Confirm: cleanup-data`。
- 所有导入失败都回滚整个事务；响应会显示 `total_imported: 0`、`rolled_back: true` 和 `total_rolled_back`，不会再把未提交的暂存数量表述为已导入。

## 备份恢复

导入导出逻辑由 `src/services/data_portability_service.py` 统一承载，路由层只做 HTTP 收发：

- `GET /api/config/export`：保持 `export_version=1.0` JSON 格式。
- `POST /api/config/import`：请求仍为 `{ "data": { ... } }`，默认合并导入，不清空已有数据。
- 支持的区块：`bloggers`、`posts`、`predictions`、`viewpoints`、`fund_info`、`fund_history`、`sector_alias`、`sector_fund_mapping`、`investment_advice`。
- 按 SQLAlchemy 列定义转换 `Date`、`DateTime`、`Boolean`、`JSON`、`Integer`、`Float` 和 `Numeric`。
- 去重规则：业务主表按 `id`，基金按 `fund_code`，历史净值按 `fund_code + nav_date`，别名按 `alias_name`，板块映射按 `sector_name + fund_code`。
- 导入成功后会重置 SQLite/PostgreSQL 的自增序列，避免后续创建记录时 ID 冲突。
- 旧备份可能含有不存在于 `fund_info` 的板块映射基金。为满足 `sector_fund_mapping.fund_code` 外键，导入会创建最小 `FundInfo` 占位记录：`data_quality=recovery_placeholder`、`can_delete=false`，并在 `data.created_dependencies` 与 `warnings` 中明确报告。它们没有净值数据，应在基金页同步，不是伪造行情。
- 危险的 `/api/import-database` 仍默认禁用，仅用于受控维护；它不是普通 JSON 备份恢复入口。

### 真实备份验收

2026-07-10 使用独立的系统临时 SQLite 数据库导入真实 `1.0` 备份，首次导入成功：

| 区块 | 原始导入记录数 |
| --- | ---: |
| `bloggers` | 21 |
| `posts` | 328 |
| `predictions` | 1,527 |
| `viewpoints` | 483 |
| `fund_info` | 87 |
| `fund_history` | 4,244 |
| `sector_alias` | 0 |
| `sector_fund_mapping` | 57 |
| `investment_advice` | 22 |
| 合计 | 6,769 |

该备份另有 31 个映射基金没有基础基金记录，因此临时库最终有 118 条 `fund_info`：87 条来自备份，31 条为上述受控占位记录。首次导入 `total_failed=0`；第二次导入成功，`total_imported=0`、`total_skipped=6,769`，证明幂等。

## 已修复的问题

### 帖子、分析与预测

- `src/api/routes/posts.py`：创建帖子后，顶层 `success` 真实透传 `PostService.create_post_with_analysis()` 的结果，不再将分析失败包装成成功。
- `src/services/post_service.py`：移除会覆盖数据库人工板块映射的旧校验分支。同步、异步、批量分析统一采用“人工/数据库映射优先，LLM 与默认映射兜底”。

### 基金、建议与板块映射

- `src/fund/fund_sync_manager.py`：外部 API 返回非法日期时，不再以“今天”覆盖已有可信净值日期。
- `src/services/advice_service.py`：建议缓存哈希与建议数据口径一致，软删除或已过期观点不会继续命中旧建议缓存。
- `src/services/sector_fund_service.py`：调整板块映射只停用冲突映射，绝不删除 `FundInfo` 和基金历史数据。

### 清理安全与前端

- `src/core/safety.py`、`src/api/routes/config.py`、`src/api/routes/test_data.py`、`src/api/routes/posts.py`、`src/api/routes/viewpoints.py`、`src/tasks/cleanup_tasks.py` 和 `src/tasks/scheduler.py`：删除型清理默认禁用，前端先预览；开关未开启时隐藏或禁用清理操作。
- `web/index.html`：导入结果已展示导入、跳过、失败和警告信息；新增的映射依赖占位数量通过警告展示。
- `tests/conftest.py`、`src/models/database.py`：显式 `DATABASE_URL=sqlite:///...` 会被尊重，避免测试或恢复验收意外落到默认本地库；测试会话建立表结构，不依赖开发机既有数据库。

## 删除和保留

- 已删除 `web/simple.html` 及 `src/api/main.py` 中对应的旧调试静态入口。该页绕过生产 API 密码头，且没有业务入口或数据承载能力。
- 未删除 `src/crawler/ai_analyzer.py` 和 `src/crawler/enhanced_ai_analyzer.py`。它们仍被正式代码作为兼容壳调用；删除前必须先替换所有调用方并运行爬虫和分析回归。
- 未改动 `render.yaml`、生产 `DATABASE_URL` 或 Supabase 数据结构。

## 关键文件与责任边界

| 文件 | 责任 | 修改注意事项 |
| --- | --- | --- |
| `src/services/data_portability_service.py` | JSON 导入、导出、类型转换、事务、序列 | 改格式前先保留 `1.0` 兼容测试和真实备份验收 |
| `src/api/routes/config.py` | 配置、备份接口、危险维护入口 | 不要让普通导入调用 `/api/import-database` |
| `src/models/database.py` | ORM、SQLite/PostgreSQL 引擎选择 | 属于高风险文件；结构变更必须先确认 |
| `src/services/prediction_verify_service.py` | 预测评分和准确率 | 改前先读过程指标和批处理测试 |
| `src/services/sector_flow_service.py` | 资金流抓取、计算、幂等写入 | 需同时验证路由、任务和运行日志 |
| `src/analyzer/llm_analyzer.py` | LLM 分析、熔断、缓存、解析 | 改动前读调用方，避免改变预测入库契约 |
| `web/index.html` | Vue CDN 单页界面 | 无构建链；只做局部、可验证的整理 |

## 验证记录

本轮新增或调整的重点测试：

- `tests/unit/test_data_portability_service.py`
- `tests/unit/test_database_url_routing.py`
- `tests/unit/test_post_routes.py`
- `tests/unit/test_post_analysis_flow.py`
- `tests/unit/test_advice_service.py`
- `tests/unit/test_sector_fund_service.py`
- `tests/unit/test_frontend_data_import.py`
- `tests/unit/test_frontend_post_flow.py`
- `tests/unit/test_static_page_cleanup.py`

已通过的局部验证包括导入导出、生产安全、帖子分析、基金同步、投资建议、爬虫、前端导入结果、调度和板块映射。全量验收以本轮最终命令输出为准：

```powershell
pytest tests/ -v
$env:DATABASE_URL = "sqlite:///C:/Temp/fund-insight-init-check.db"
python -m src --init-db
codegraph sync .
codegraph status .
git diff --check
```

最终执行结果：`pytest tests/ -v` 为 `139 passed, 1 warning`；隔离 SQLite 的 `python -m src --init-db` 成功；`codegraph sync .` 后 `codegraph status .` 显示索引已最新（193 个文件、3,051 个节点、4,583 条边）；`git diff --check` 没有空白错误。

本机仅有 `requests` 与 `urllib3` 版本兼容警告；它不表示项目测试失败。不要把这个第三方环境警告当作业务回归。

## 后续维护流程

1. 先在隔离 SQLite 数据库运行目标测试，绝不将测试命令指向 Supabase。
2. 涉及备份时，先运行 `test_data_portability_service.py`，再用真实导出 JSON 做一次临时库导入和二次幂等导入。
3. 涉及删除、迁移、依赖、部署或公共 API 时，遵循 `AGENTS.md` 的确认规则。
4. 改动索引相关代码或文档后运行 `codegraph sync .`；不要手改 `.codegraph/`。
5. 只有本地恢复和相关回归通过，且用户明确要求时，才讨论生产恢复操作。
