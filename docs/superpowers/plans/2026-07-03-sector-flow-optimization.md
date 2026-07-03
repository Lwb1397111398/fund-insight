# 抢筹板块抓取优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一抢筹板块抓取入口，实现双主幂等调度、运行日志、清理机制、前端状态展示，并完成自动化与短延迟真实验证。

**Architecture:** 将 GitHub Actions、Render Cron、手动接口全部收口到 `SectorFlowService.run_fetch()`。抓取失败只写运行日志，不删除旧数据；抓取成功按 `flow_date + sector_code + data_category` 幂等更新。新增 `SectorFlowFetchRun` 记录每次抓取状态，API 与前端读取该状态展示最近数据日期和失败原因。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0、pytest、SQLite 测试库、PostgreSQL/Supabase 生产库、GitHub Actions、Render Cron、原生 HTML/JS/Vue。

---

## File Structure

- Modify `src/models/database.py`
  - 新增 `SectorFlowFetchRun` ORM 模型。
  - 给 `SectorFundFlow` 增加唯一索引，优先使用 `flow_date + sector_code + data_category`。

- Modify `src/services/sector_flow_service.py`
  - 新增 `run_fetch(trigger, categories=None)` 统一入口。
  - 保留 `fetch_and_save()` 作为兼容包装。
  - 新增运行日志创建/完成方法。
  - 调整 upsert 逻辑，按日期、代码、分类幂等更新。
  - 新增 `get_fetch_status()`。

- Modify `src/api/routes/sector_flow.py`
  - 修复手动抓取参数名问题。
  - 手动抓取改为调用 `run_fetch(trigger="manual")`。
  - 新增 `GET /api/sector-flow/fetch-status`。

- Modify `scripts/fetch_sector_flow.py`
  - 删除独立抓取/计算/SQL 写库逻辑。
  - 改为 CLI 包装器，调用 `SectorFlowService.run_fetch(trigger=...)`。
  - 输出结构化 JSON 日志，失败返回非 0。

- Modify `scripts/run_scheduled_tasks.py`
  - `daily` 任务加入抢筹抓取。
  - 抢筹失败不阻断基金更新与预测验证，但最终结果中记录 sector flow 状态。

- Modify `src/tasks/scheduler.py`
  - 增加 `_run_sector_flow()`，供本地 scheduler 和 Render 脚本复用。

- Modify `src/tasks/cleanup_tasks.py`
  - 新增 `cleanup_old_sector_flow_runs(keep_days=180)`。
  - 接入 `run_cleanup_task()` 汇总和配置页摘要。

- Modify `src/api/routes/config.py`
  - 待清理统计增加过期运行日志数量。
  - 清理文案包含抢筹运行日志。

- Modify `web/index.html`
  - 抢筹板块展示最新数据日期、最近抓取状态、触发来源、失败原因。
  - 手动刷新按钮调用 `/api/sector-flow/fetch` 后刷新状态。

- Create `tests/unit/test_sector_flow_service.py`
  - 计算规则、幂等保存、失败保护、运行日志测试。

- Create `tests/unit/test_sector_flow_routes.py`
  - 手动抓取和状态 API 测试。

- Modify `tests/unit/test_scheduler_fixes.py`
  - 增加 `_run_sector_flow()` 和 `run_daily_tasks()` 调用测试。

- Create `scripts/test_sector_flow_timer.py`
  - 本地短延迟验证脚本：等待 N 秒后执行 `run_fetch(trigger="test_timer")` 并打印结果。

---

### Task 1: Add Fetch Run Model

**Files:**
- Modify: `src/models/database.py`
- Test: `tests/conftest.py` 自动创建所有 ORM 表，无需改动。

- [ ] **Step 1: Add `SectorFlowFetchRun` model**

Add below `SectorFundFlow` in `src/models/database.py`:

```python
class SectorFlowFetchRun(Base):
    """板块资金流向抓取运行日志"""
    __tablename__ = 'sector_flow_fetch_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False, default='running')
    flow_date = Column(Date, nullable=False)
    categories = Column(String(100))
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime)
    fetched_count = Column(Integer, default=0)
    saved_count = Column(Integer, default=0)
    error_message = Column(Text)
    data_source = Column(String(50), default='eastmoney')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_sector_flow_fetch_runs_date', 'flow_date'),
        Index('ix_sector_flow_fetch_runs_status', 'status'),
        Index('ix_sector_flow_fetch_runs_trigger', 'trigger'),
    )
```

- [ ] **Step 2: Add idempotency index to `SectorFundFlow`**

Extend `SectorFundFlow.__table_args__`:

```python
__table_args__ = (
    Index('ix_sector_fund_flow_date', 'flow_date'),
    Index('ix_sector_flow_date_behavior', 'flow_date', 'behavior'),
    Index('ix_sector_flow_intensity', 'main_intensity'),
    Index('ix_sector_flow_date_code_category', 'flow_date', 'sector_code', 'data_category', unique=True),
)
```

- [ ] **Step 3: Run import/schema smoke test**

Run:

```bash
pytest tests/test_imports.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/models/database.py
git commit -m "feat: 添加抢筹抓取运行日志模型"
```

---

### Task 2: Implement Unified Service Entry

**Files:**
- Modify: `src/services/sector_flow_service.py`
- Test: `tests/unit/test_sector_flow_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/unit/test_sector_flow_service.py`:

```python
from datetime import date, timedelta
from unittest.mock import Mock

from src.models.database import SectorFundFlow, SectorFlowFetchRun
from src.services.sector_flow_service import SectorFlowService


def sample_item(name="测试板块", code="BK0001", category="industry"):
    return {
        "sector_code": code,
        "sector_name": name,
        "change_pct": 1.2,
        "turnover": 100.0,
        "main_net_flow": 5.0,
        "retail_net_flow": -2.0,
        "data_category": category,
    }


def test_behavior_thresholds():
    assert SectorFlowService.judge_behavior(3.0) == "grab"
    assert SectorFlowService.judge_behavior(1.0) == "build"
    assert SectorFlowService.judge_behavior(0.0) == "wash"
    assert SectorFlowService.judge_behavior(-1.0) == "sell"


def test_enrich_calculates_dark_pool_and_intensity(test_db):
    service = SectorFlowService(test_db)
    enriched = service.enrich(sample_item())

    assert enriched["dark_pool"] == 7.0
    assert enriched["main_intensity"] == 7.0
    assert enriched["behavior"] == "grab"


def test_run_fetch_saves_run_log_and_records(test_db):
    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.side_effect = [
        [sample_item("行业A", "BK1001", "industry")],
        [sample_item("概念A", "BK2001", "concept")],
    ]

    result = service.run_fetch(trigger="manual")

    assert result["success"] is True
    assert result["saved_count"] == 2
    assert test_db.query(SectorFundFlow).count() == 2
    run = test_db.query(SectorFlowFetchRun).one()
    assert run.trigger == "manual"
    assert run.status == "success"
    assert run.fetched_count == 2
    assert run.saved_count == 2


def test_run_fetch_is_idempotent_for_same_day(test_db):
    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.return_value = [sample_item("行业A", "BK1001", "industry")]

    first = service.run_fetch(trigger="manual", categories=["industry"])
    second = service.run_fetch(trigger="manual", categories=["industry"])

    assert first["saved_count"] == 1
    assert second["saved_count"] == 1
    assert test_db.query(SectorFundFlow).count() == 1
    assert test_db.query(SectorFlowFetchRun).count() == 2


def test_run_fetch_failure_keeps_existing_data_and_logs_failure(test_db):
    existing = SectorFundFlow(
        flow_date=date.today(),
        sector_name="旧板块",
        sector_code="BKOLD",
        data_category="industry",
        main_net_flow=1.0,
        data_source="eastmoney",
    )
    test_db.add(existing)
    test_db.commit()

    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.side_effect = RuntimeError("上游失败")

    result = service.run_fetch(trigger="manual", categories=["industry"])

    assert result["success"] is False
    assert test_db.query(SectorFundFlow).count() == 1
    run = test_db.query(SectorFlowFetchRun).one()
    assert run.status == "failed"
    assert "上游失败" in run.error_message


def test_cleanup_old_sector_flow_runs(test_db):
    from src.tasks.cleanup_tasks import CleanupTasks

    old_run = SectorFlowFetchRun(
        trigger="manual",
        status="success",
        flow_date=date.today() - timedelta(days=200),
    )
    new_run = SectorFlowFetchRun(
        trigger="manual",
        status="success",
        flow_date=date.today(),
    )
    test_db.add_all([old_run, new_run])
    test_db.commit()

    cleanup = CleanupTasks()
    cleanup.SessionLocal = lambda: test_db
    result = cleanup.cleanup_old_sector_flow_runs(keep_days=180)

    assert result["success"] is True
    assert result["deleted_sector_flow_runs"] == 1
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/unit/test_sector_flow_service.py -v
```

Expected: FAIL because `run_fetch`, `SectorFlowFetchRun`, or cleanup method is not implemented yet.

- [ ] **Step 3: Implement service methods**

In `src/services/sector_flow_service.py`:

- Import `datetime` and `SectorFlowFetchRun`.
- Add `run_fetch()`.
- Add `_start_run()`, `_finish_run()`, `_fetch_categories()`.
- Update `_upsert()` to match by `flow_date + sector_code + data_category`, falling back to name/category when code is missing.
- Keep `fetch_and_save()` as compatibility wrapper:

```python
def fetch_and_save(self, turnover_limit: int = 100) -> int:
    result = self.run_fetch(trigger="service_compat")
    return result.get("saved_count", 0) if result.get("success") else 0
```

- [ ] **Step 4: Run service tests**

Run:

```bash
pytest tests/unit/test_sector_flow_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/sector_flow_service.py tests/unit/test_sector_flow_service.py
git commit -m "feat: 统一抢筹抓取服务入口"
```

---

### Task 3: Add API Status and Fix Manual Fetch

**Files:**
- Modify: `src/api/routes/sector_flow.py`
- Test: `tests/unit/test_sector_flow_routes.py`

- [ ] **Step 1: Write API tests**

Create `tests/unit/test_sector_flow_routes.py`:

```python
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.api.main import app


client = TestClient(app)


def test_manual_sector_flow_fetch_uses_run_fetch():
    with patch("src.api.routes.sector_flow.SectorFlowService") as MockService:
        service = MockService.return_value
        service.run_fetch.return_value = {
            "success": True,
            "status": "success",
            "saved_count": 2,
            "fetched_count": 2,
            "run_id": 1,
            "error_message": None,
        }

        response = client.post("/api/sector-flow/fetch")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    service.run_fetch.assert_called_once_with(trigger="manual")


def test_sector_flow_fetch_status_endpoint():
    with patch("src.api.routes.sector_flow.SectorFlowService") as MockService:
        service = MockService.return_value
        service.get_fetch_status.return_value = {
            "latest_run": None,
            "latest_data_date": None,
            "today_data_count": 0,
            "displaying_stale_data": False,
        }

        response = client.get("/api/sector-flow/fetch-status")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["today_data_count"] == 0
```

- [ ] **Step 2: Run failing API tests**

Run:

```bash
pytest tests/unit/test_sector_flow_routes.py -v
```

Expected: FAIL until route changes are implemented.

- [ ] **Step 3: Modify route**

Change `POST /fetch` to:

```python
result = service.run_fetch(trigger="manual")
if result.get("success"):
    return {
        "success": True,
        "data": result,
        "message": f"成功保存 {result.get('saved_count', 0)} 条板块资金流向数据",
    }
return {
    "success": False,
    "data": result,
    "message": result.get("error_message") or "数据抓取失败，请稍后重试。最近一次缓存数据仍可用于查看。",
    "error_code": "SECTOR_FLOW_FETCH_FAILED",
}
```

Add endpoint:

```python
@router.get("/fetch-status")
async def get_fetch_status(db: Session = Depends(get_db)):
    service = SectorFlowService(db)
    return {"success": True, "data": service.get_fetch_status()}
```

- [ ] **Step 4: Run API tests**

Run:

```bash
pytest tests/unit/test_sector_flow_routes.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/sector_flow.py tests/unit/test_sector_flow_routes.py
git commit -m "feat: 添加抢筹抓取状态接口"
```

---

### Task 4: Update CLI and Render Scheduler

**Files:**
- Modify: `scripts/fetch_sector_flow.py`
- Modify: `scripts/run_scheduled_tasks.py`
- Modify: `src/tasks/scheduler.py`
- Test: `tests/unit/test_scheduler_fixes.py`

- [ ] **Step 1: Add scheduler tests**

Append to `tests/unit/test_scheduler_fixes.py`:

```python
    def test_run_sector_flow_invokes_service(self):
        scheduler = TaskScheduler()
        mock_db = Mock()
        with patch('src.models.database.SessionLocal', return_value=mock_db):
            with patch('src.services.sector_flow_service.SectorFlowService') as MockService:
                service = MockService.return_value
                service.run_fetch.return_value = {"success": True, "saved_count": 1}
                result = scheduler._run_sector_flow(trigger="render_cron")

        service.run_fetch.assert_called_once_with(trigger="render_cron")
        assert result["success"] is True
        mock_db.close.assert_called()
```

- [ ] **Step 2: Run failing scheduler test**

Run:

```bash
pytest tests/unit/test_scheduler_fixes.py::TestSchedulerFixes::test_run_sector_flow_invokes_service -v
```

Expected: FAIL because `_run_sector_flow` does not exist.

- [ ] **Step 3: Implement `_run_sector_flow()`**

Add to `src/tasks/scheduler.py`:

```python
def _run_sector_flow(self, trigger: str = "scheduler"):
    """执行抢筹板块资金流向抓取"""
    from src.models.database import SessionLocal
    from src.services.sector_flow_service import SectorFlowService

    logger.info("开始执行抢筹板块资金流向抓取...")
    db = SessionLocal()
    try:
        service = SectorFlowService(db)
        result = service.run_fetch(trigger=trigger)
        if result.get("success"):
            logger.info(f"抢筹抓取完成: 保存 {result.get('saved_count', 0)} 条")
        else:
            logger.error(f"抢筹抓取失败: {result}")
        return result
    except Exception as e:
        logger.error(f"执行抢筹抓取失败: {e}", exc_info=True)
        return {"success": False, "error_message": str(e)}
    finally:
        db.close()
```

- [ ] **Step 4: Update `run_daily_tasks()`**

In `scripts/run_scheduled_tasks.py`, call sector flow first or after fund update and include result:

```python
sector_flow_result = scheduler._run_sector_flow(trigger="render_cron")
scheduler._run_fund_update()
scheduler._run_prediction_verify()
scheduler._run_expired_verify()
return {
    "success": True,
    "sector_flow": sector_flow_result,
    "started_at": started_at.isoformat(),
    "finished_at": datetime.now().isoformat(),
}
```

If sector flow fails, daily job should still continue other tasks, but the returned dict should include sector flow failure.

- [ ] **Step 5: Replace `scripts/fetch_sector_flow.py` with service wrapper**

Implement CLI:

```python
import argparse
import json
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.models.database import SessionLocal
from src.services.sector_flow_service import SectorFlowService


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取板块资金流向数据")
    parser.add_argument("--trigger", default="github_actions")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        service = SectorFlowService(db)
        result = service.run_fetch(trigger=args.trigger)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0 if result.get("success") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run scheduler tests**

Run:

```bash
pytest tests/unit/test_scheduler_fixes.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/fetch_sector_flow.py scripts/run_scheduled_tasks.py src/tasks/scheduler.py tests/unit/test_scheduler_fixes.py
git commit -m "feat: 统一抢筹定时抓取入口"
```

---

### Task 5: Add Cleanup Integration

**Files:**
- Modify: `src/tasks/cleanup_tasks.py`
- Modify: `src/api/routes/config.py`
- Test: `tests/unit/test_sector_flow_service.py`

- [ ] **Step 1: Implement cleanup method**

Add `cleanup_old_sector_flow_runs()` to `CleanupTasks` near `cleanup_old_sector_flow()`:

```python
def cleanup_old_sector_flow_runs(self, keep_days: int = 180) -> dict:
    """清理过期的抢筹抓取运行日志"""
    from src.models.database import SessionLocal, SectorFlowFetchRun
    from datetime import date, timedelta

    db = SessionLocal()
    try:
        cutoff_date = date.today() - timedelta(days=keep_days)
        old_runs = db.query(SectorFlowFetchRun).filter(
            SectorFlowFetchRun.flow_date < cutoff_date
        )
        deleted = old_runs.count()
        if deleted == 0:
            return {"success": True, "deleted_sector_flow_runs": 0, "message": "没有需要清理的抓取运行日志"}
        old_runs.delete(synchronize_session=False)
        db.commit()
        return {"success": True, "deleted_sector_flow_runs": deleted, "cutoff_date": cutoff_date.isoformat()}
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e), "deleted_sector_flow_runs": 0}
    finally:
        db.close()
```

- [ ] **Step 2: Add cleanup summary**

In `run_cleanup_task()` / `cleanup_all()` summary, call this method and include:

```python
sector_flow_runs_result = self.cleanup_old_sector_flow_runs(keep_days=180)
```

Add to `total_deleted` and response:

```python
"sector_flow_runs": {
    "deleted": sector_flow_runs_result.get("deleted_sector_flow_runs", 0)
}
```

- [ ] **Step 3: Update config cleanup preview**

In `src/api/routes/config.py`, include old `SectorFlowFetchRun` count in preview and text summary.

- [ ] **Step 4: Run cleanup-related tests**

Run:

```bash
pytest tests/unit/test_sector_flow_service.py::test_cleanup_old_sector_flow_runs -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tasks/cleanup_tasks.py src/api/routes/config.py tests/unit/test_sector_flow_service.py
git commit -m "feat: 清理抢筹抓取运行日志"
```

---

### Task 6: Frontend Status Display

**Files:**
- Modify: `web/index.html`
- Test: existing frontend smoke tests if available.

- [ ] **Step 1: Locate sector flow UI state**

Find current sector flow methods and state in `web/index.html`.

- [ ] **Step 2: Add state fields**

Add fields similar to:

```javascript
sectorFlowStatus: null,
sectorFlowStatusLoading: false,
sectorFlowFetchLoading: false,
```

- [ ] **Step 3: Add API method**

Add method:

```javascript
async loadSectorFlowStatus() {
  this.sectorFlowStatusLoading = true;
  try {
    const res = await axios.get('/api/sector-flow/fetch-status');
    if (res.data.success) {
      this.sectorFlowStatus = res.data.data;
    }
  } catch (error) {
    console.error('加载抢筹抓取状态失败:', error);
  } finally {
    this.sectorFlowStatusLoading = false;
  }
}
```

- [ ] **Step 4: Update manual fetch method**

After manual fetch success/failure, call:

```javascript
await this.loadSectorFlowStatus();
await this.loadSectorFlowRanking();
```

- [ ] **Step 5: Add UI block**

Near sector flow card title, render latest status:

```html
<div v-if="sectorFlowStatus" class="text-xs text-gray-500 mt-1">
  <span>最新数据：{{ sectorFlowStatus.latest_data_date || '暂无' }}</span>
  <span v-if="sectorFlowStatus.latest_run">
    ｜最近抓取：{{ sectorFlowStatus.latest_run.status }} / {{ sectorFlowStatus.latest_run.trigger }}
  </span>
  <span v-if="sectorFlowStatus.displaying_stale_data" class="text-orange-600">
    ｜当前展示最近可用数据
  </span>
  <div v-if="sectorFlowStatus.latest_run && sectorFlowStatus.latest_run.error_message" class="text-red-600">
    失败原因：{{ sectorFlowStatus.latest_run.error_message }}
  </div>
</div>
```

- [ ] **Step 6: Run existing frontend tests**

Run:

```bash
pytest tests/unit/test_frontend_visual_polish.py tests/unit/test_web_cleanup_buttons.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/index.html
git commit -m "feat: 展示抢筹抓取状态"
```

---

### Task 7: Add Short Delay Timer Verification Script

**Files:**
- Create: `scripts/test_sector_flow_timer.py`

- [ ] **Step 1: Create timer script**

```python
"""短延迟抢筹抓取验证脚本"""
import argparse
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.models.database import SessionLocal
from src.services.sector_flow_service import SectorFlowService


def main() -> int:
    parser = argparse.ArgumentParser(description="等待指定秒数后执行抢筹抓取验证")
    parser.add_argument("--delay", type=int, default=30)
    args = parser.parse_args()

    print(f"将在 {args.delay} 秒后执行抢筹抓取验证...")
    time.sleep(args.delay)

    db = SessionLocal()
    try:
        service = SectorFlowService(db)
        result = service.run_fetch(trigger="test_timer")
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0 if result.get("success") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run timer with short delay during implementation**

Run first with 1 second in local test:

```bash
python scripts/test_sector_flow_timer.py --delay 1
```

Expected: either success with `saved_count > 0`, or controlled failure with run log if upstream API is unavailable.

After all tests pass, run the user-requested 30 second verification:

```bash
python scripts/test_sector_flow_timer.py --delay 30
```

- [ ] **Step 3: Commit**

```bash
git add scripts/test_sector_flow_timer.py
git commit -m "test: 添加抢筹短延迟抓取验证脚本"
```

---

### Task 8: Full Verification

**Files:**
- No new files unless bug fixes are required.

- [ ] **Step 1: Run focused tests**

```bash
pytest tests/unit/test_sector_flow_service.py tests/unit/test_sector_flow_routes.py tests/unit/test_scheduler_fixes.py -v
```

Expected: PASS.

- [ ] **Step 2: Run broader test subset**

```bash
pytest tests/unit -v
```

Expected: PASS or document unrelated existing failures.

- [ ] **Step 3: Run import smoke**

```bash
pytest tests/test_imports.py -v
```

Expected: PASS.

- [ ] **Step 4: Run short delay verification**

```bash
python scripts/test_sector_flow_timer.py --delay 30
```

Expected:

- If Eastmoney API is reachable: command exits 0, output contains `"success": true`, and `saved_count` is greater than 0.
- If Eastmoney API is unreachable: command exits 1, output contains `"success": false`, `error_message` is populated, and no existing sector flow rows are deleted.

- [ ] **Step 5: Check git status**

```bash
git status --short
```

Expected: clean working tree after final commit, or only intentionally uncommitted user files.

---

## Self-Review

- Spec coverage: unified entry,双主幂等, run logs, cleanup, status API, frontend status, tests, 30 秒验证 are covered by Tasks 1-8.
- Placeholder scan: no TBD/TODO placeholders are intended in executable steps.
- Type consistency: `run_fetch(trigger, categories=None)`, `SectorFlowFetchRun`, `/fetch-status`, and `cleanup_old_sector_flow_runs()` names are consistent across tasks.
