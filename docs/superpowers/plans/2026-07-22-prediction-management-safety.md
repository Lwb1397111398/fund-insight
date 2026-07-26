# Prediction Management Safety Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 安全重构预测管理，使预测可追溯、验证一致、Render 任务可恢复，并保证 Supabase 资料不会因部署或维护操作丢失。

**Architecture:** 保留现有 FastAPI + SQLAlchemy + Vue CDN 技术栈。以兼容方式拆分预测查询、命令、验证和维护职责，前端沿用无构建链的 manager 工厂模式；数据库变化只通过显式迁移和备份演练进入生产。

**Tech Stack:** Python 3.10/3.12、FastAPI、SQLAlchemy 2.0、Pydantic 2、pytest、Vue 3 CDN、PostgreSQL/Supabase、SQLite。

---

### Task 1: 建立安全行为测试

**Files:**
- Create: `tests/unit/test_prediction_management_safety.py`
- Modify: `tests/conftest.py`（仅在需要共用工厂时）

- [ ] 为软删除、恢复、周期重算、已验证编辑拒绝分别编写最小失败测试。
- [ ] 运行 `pytest tests/unit/test_prediction_management_safety.py -v`，确认测试因现有行为缺失而失败。
- [ ] 保留失败输出作为 RED 证据。

### Task 2: 实现安全编辑与恢复

**Files:**
- Modify: `src/services/prediction_service.py`
- Modify: `src/api/routes/predictions.py`
- Modify: `src/api/schemas/prediction.py`

- [ ] 将删除实现改为写入 `is_deleted/deleted_at/delete_reason/restore_before`，不调用 `db.delete()`。
- [ ] 增加恢复命令，恢复时重新计算博主统计和基金活跃预测数。
- [ ] 为更新请求添加方向、置信度和周期枚举校验。
- [ ] 待验证预测周期变化时通过统一周期计算器更新目标日期；已验证预测关键字段变化返回 409。
- [ ] 运行 Task 1 测试并确认通过，再运行现有预测服务测试。

### Task 3: 安全化维护操作

**Files:**
- Create: `src/services/prediction_maintenance_service.py`
- Modify: `src/api/routes/predictions.py`
- Test: `tests/unit/test_prediction_maintenance.py`

- [ ] 先写跨博主、空基金代码和合法同博主重复组的失败测试。
- [ ] 将相似预测改为只读扫描；默认不修改预测，旧执行端点返回预览结果。
- [ ] 为板块映射同步和无效验证回溯增加 `dry_run=true` 默认值；执行要求 `X-Danger-Confirm`。
- [ ] 修正映射同步对 `success/failed` 的识别，并保留验证前快照。
- [ ] 每个维护任务使用单一事务，异常时整体回滚。

### Task 4: 持久化批量验证

**Files:**
- Modify: `src/services/prediction_verify_task.py`
- Modify: `src/services/prediction_verify_service.py`
- Modify: `src/api/routes/predictions.py`
- Test: `tests/unit/test_prediction_verify_batch_task.py`

- [ ] 先写任务跨实例读取、陈旧任务恢复和重复触发拒绝测试。
- [ ] 使用现有 `BatchAnalysisTask(task_type='predictions')` 保存进度和结果。
- [ ] PostgreSQL 使用数据库锁保证 Cron/Web 单执行者；SQLite 保留进程锁用于本地。
- [ ] 单条验证行加锁，预测结果与博主统计同事务提交。
- [ ] 合并普通验证和过期补救扫描，旧入口保留兼容代理。

### Task 5: 分页查询与兼容 API

**Files:**
- Create: `src/services/prediction_query_service.py`
- Modify: `src/api/routes/predictions.py`
- Test: `tests/unit/test_prediction_query.py`

- [ ] 先写超过 1000 条、组合筛选、观望预测和状态计数测试。
- [ ] 实现服务端分页、排序、关键词、博主、基金、板块、方向、日期和结果筛选。
- [ ] 响应继续返回 `data` 数组，并新增 `meta={page,page_size,total,has_more,facets}`。
- [ ] 详情查询对待验证预测开放，返回来源帖子与派生生命周期字段。
- [ ] 将未使用端点标记为 deprecated，首版不删除。

### Task 6: 预测管理前端

**Files:**
- Create: `web/prediction-manager.js`
- Modify: `web/index.html`
- Modify: `web/common.css`
- Test: `tests/unit/test_frontend_prediction_flow.py`

- [ ] 先写静态契约测试，覆盖脚本加载、分页参数、恢复操作和维护按钮隔离。
- [ ] 抽取状态、请求、筛选、分页、详情、编辑、归档、恢复和任务轮询逻辑。
- [ ] 主视图展示全部/待验证/到期/正确/错误/观望，计数来自服务端 `facets`。
- [ ] 所有预测都可看详情；来源帖子可追踪；`0%` 按数值显示。
- [ ] 将重复扫描、映射同步和验证回溯放入维护区，先预览再执行。

### Task 7: 数据迁移与备份演练

**Files:**
- Create: `alembic.ini`、`alembic/`
- Create: `scripts/backup_database.py`
- Modify: `DEPLOYMENT.md`
- Test: `tests/unit/test_database_backup_manifest.py`

- [ ] 建立当前模型的 Alembic 基线和只新增结构的首个迁移。
- [ ] 增加预测变更日志表并加入应用 JSON 导出。
- [ ] 备份脚本只写本地忽略目录，生成行数、时间和 SHA-256 清单，不记录连接串。
- [ ] 提供 `pg_dump`、隔离恢复、结构比对、迁移预检和回滚步骤。
- [ ] 不在测试或本次执行中连接生产 Supabase。

### Task 8: 验证与收尾

- [ ] 运行预测专项测试与新增前端契约测试。
- [ ] 运行 `pytest tests/ -v`。
- [ ] 运行 `python -m src --init-db`。
- [ ] 运行 `codegraph sync .` 和 `codegraph status .`。
- [ ] 检查 `git diff --check`、工作树状态和生产数据安全清单。
- [ ] 仅提交本分支改动，不推送、不迁移 Supabase、不执行生产数据修复。
