# 模块记录 - Tasks

## 模块定位

`src/tasks/` 负责本地后台调度和数据清理。生产 Render Cron 使用 `scripts/run_scheduled_tasks.py` 一次性执行每日任务，不依赖 Web 进程内常驻线程。

## 当前职责

- 本地调度器：基金更新、预测验证、过期补救验证、清理。
- 清理任务：过期预测/观点、旧基金历史、旧板块资金流、旧抓取日志、空帖子、孤儿基金、旧建议等。
- 提供手动清理入口给 API 层调用。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `scheduler.py` | `TaskScheduler`，本地常驻调度器和单次任务方法 |
| `cleanup_tasks.py` | `CleanupManager`，实际清理逻辑 |
| `cleanup_enhanced.py` | 增强清理任务和任务状态 |

## 调度窗口

`TaskScheduler` 使用北京时间：

- 启动时：基金更新、预测验证、过期补救验证。
- 02:00-02:59：清理任务。
- 10:00-10:59：基金更新、预测验证、过期补救验证。
- 15:30-15:59：基金更新。

Render Cron：

```bash
python scripts/run_scheduled_tasks.py daily
```

执行：

1. `init_db()`。
2. `scheduler._run_sector_flow(trigger="render_cron")`。
3. `scheduler._run_fund_update()`。
4. `scheduler._run_prediction_verify()`。
5. `scheduler._run_expired_verify()`。

## 清理范围

- 过期预测和关联空帖子。
- 过期观点。
- 旧基金历史。
- 旧板块资金流和 `sector_flow_fetch_runs`。
- 空帖子。
- 旧投资建议。
- 最旧批次数据。
- 孤儿基金。

## 高风险点

- 清理会删数据，必须保留预览/确认路径。
- Render Cron 和 GitHub Actions 可能同时写板块资金流，`SectorFlowService` 必须保持幂等。
- 本地调度器是进程内线程，不是分布式调度。
- 基金更新失败应只影响单个基金。

## 推荐验证

```bash
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_sector_flow_service.py::test_cleanup_old_sector_flow_runs -v
python scripts/run_scheduled_tasks.py daily
```
