# 2026-07-12 平台、数据安全与分析链路审查记录

## 范围

- 入口：`scripts/run_scheduled_tasks.py`、`src/tasks/scheduler.py`、`src/models/database.py`、`src/services/post_service.py`、`src/services/prediction_service.py`、`src/analyzer/llm_analyzer.py`。
- 数据边界：仅使用临时 SQLite 验证；未连接 Render 或 Supabase；未执行生产导入、清理、迁移或删除。
- 修复原则：不删除业务资料，不改公开接口删除策略，不修改部署配置，不改数据库结构。

## 已修复

1. 批量帖子分析不再硬删除低质量帖子。
   - 行为：低质量帖子保留，`analyzed=False`，批量结果返回 `skipped`，兼容保留 `deleted=0`。
   - 风险等级：高，原行为绕过清理开关并物理删除资料。

2. pytest 强制使用临时 SQLite。
   - 行为：测试启动前覆盖 `DATABASE_URL=sqlite:///...fund-insight-pytest-<pid>.db`。
   - 风险等级：高，避免本机环境变量误指向 Supabase。

3. 预测统计准确率分母包含 `failed` 验证结果。
   - 行为：`success` 和 `failed` 都计入已验证预测，准确率不再被失败预测虚高。
   - 风险等级：中，会影响统计展示但不改历史记录。

4. Render Cron 子任务失败会显式失败。
   - 行为：`daily` 聚合 `sector_flow`、`fund_update`、`prediction_verify`、`expired_verify`，任一返回 `success=False` 或抛异常都会让结果失败，主进程返回非 0。
   - 风险等级：高，避免定时任务静默失败。

5. 显式 PostgreSQL 配置 fail-closed。
   - 行为：`postgresql://...` 缺少 `psycopg2` 时拒绝启动；未知 `DATABASE_URL` scheme 也拒绝启动。
   - 风险等级：高，避免生产配置错误时写入本地 SQLite。

6. LLM 帖子分析缓存键使用完整内容哈希。
   - 行为：缓存键从 `content[:200]` 改为完整内容 SHA-256，避免前 200 字相同的不同帖子复用错误分析。
   - 风险等级：中，影响重复分析命中率但提升正确性。

7. 自动板块-基金映射不再重复新增。
   - 行为：保存前检查同板块活跃映射；存在则跳过新增；已有人工审核映射时自动匹配不覆盖。
   - 风险等级：中，只阻止后续重复写入，不清理历史重复资料。

## 待确认风险

- 旧 `/api/import-database` 仍默认关闭；永久删除或进一步隔离需确认。
- `DELETE /api/posts/{id}`、`DELETE /api/predictions/{id}` 仍是物理删除；是否改为默认禁用或软删除需确认。
- `POST /api/predictions/merge-similar` 可能跨博主合并；加入 `blogger_id` 或调整产品规则需确认。
- `sync-sector-mapping` 重绑基金时会影响历史验证状态；修复前需备份和确认重算策略。
- `ACCESS_PASSWORD` 缺失时的认证策略、`/api/health/detail` 是否鉴权、Render Cron 时区和唯一主抓取器都属于行为或部署变更，需单独确认。
- 资金流表缺少数据库级唯一约束；加约束前需只读检查历史重复、备份和迁移确认。

## 验证

- `pytest tests/unit/test_post_analysis_flow.py tests/unit/test_database_url_routing.py tests/unit/test_services/test_services.py tests/unit/test_deployment_optimization.py tests/unit/test_scheduler_fixes.py tests/unit/test_llm_analyzer_cache.py -q`
  - 结果：`41 passed, 1 warning`
  - 警告：本机 `requests/urllib3` 版本兼容提示，非业务失败。
