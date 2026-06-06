# Fund Insight Performance Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏 Render + Supabase 生产数据的前提下，分阶段降低首屏、健康检查、基金同步、基金列表、统计接口和数据库查询的性能压力。

**Architecture:** 已完成的低风险优化保留为基线：首屏懒加载和轻量健康检查。后续优化按独立模块推进：基金列表查询先局部优化，基金全量更新改为后台任务，列表接口再逐步分页，最后处理同步扫描、市场情绪聚合和安全索引迁移。每个阶段先写失败测试，再做最小实现，再跑完整测试。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0、PostgreSQL/Supabase、SQLite 测试库、原生 HTML/Vue 3、pytest。

---

## 当前已完成基线

以下改动已经完成并通过 `pytest "tests" -v` 验证：

- `web/index.html`：登录成功后只加载 `fetchStats()` 和 `fetchBloggers()`；投资建议历史、配置和测试数据改为按需加载。
- `src/api/main.py`：`/api/health` 改为只执行 `SELECT 1`，不再统计多张表。
- `tests/unit/test_frontend_loading.py`：新增首屏和按需加载行为测试。
- `tests/integration/test_api.py`：健康检查断言不再返回 `counts`。

当前工作区还有一个非本计划产生的既有修改：`src/api/deps.py`。执行本计划时不要覆盖或回滚它，除非用户明确要求。

---

## 文件结构与责任边界

### 后端服务层

- `src/services/fund_service.py`
  - 负责基金列表、基金净值展示、基金更新入口。
  - 后续只在这里新增“每只基金最近 N 条历史净值”的查询 helper。

- `src/services/fund_update_task.py`
  - 新建文件，负责基金全量更新的后台任务状态、互斥启动、线程执行。
  - 不把后台任务状态塞进路由或 `FundService`，避免职责混杂。

- `src/fund/fund_sync_manager.py`
  - 负责预测和基金同步。
  - 后续只优化 `sync_missing_funds()` 的扫描范围和循环内查库。

### API 路由层

- `src/api/routes/funds.py`
  - 负责基金列表、基金更新启动、基金更新状态查询。
  - 后续 `POST /api/funds/update-all` 只启动任务，不同步阻塞。
  - 新增 `GET /api/funds/update-status`。

- `src/api/routes/posts.py`
- `src/api/routes/predictions.py`
- `src/api/routes/funds.py`
- `src/api/routes/viewpoints.py`
  - 后续逐步收紧默认 `limit`，并让前端显式控制分页。

- `src/api/main.py`
  - 当前仍包含 `/api/market-sentiment`。
  - 后续只优化该函数的聚合查询，不做大规模路由拆分。

### 前端

- `web/index.html`
  - 现阶段不做大拆分，只补“加载更多”和基金更新轮询。
  - 大文件拆分单独放到最后阶段，避免和性能逻辑混在一起。

### 测试

- `tests/unit/test_fund_recent_history.py`
  - 新建，测试每只基金只返回最近 5 条历史净值。

- `tests/unit/test_fund_update_task.py`
  - 新建，测试后台基金更新任务状态机。

- `tests/unit/test_frontend_fund_update.py`
  - 新建，测试前端使用后台状态接口而不是等待 300 秒同步请求。

- `tests/integration/test_fund_update_api.py`
  - 新建，测试基金更新启动和状态接口。

- `tests/integration/test_pagination_limits.py`
  - 新建，测试列表接口默认 limit 和显式 limit。

- `tests/unit/test_market_sentiment.py`
  - 新建，测试市场情绪接口输出不变。

- `tests/unit/test_sync_missing_funds.py`
  - 新建或扩展现有 `tests/unit/test_fund_sync_manager.py`，测试同步时不再循环查询已存在基金。

- `scripts/add_performance_indexes.py`
  - 新建，只生成/执行安全索引语句。

- `tests/unit/test_performance_index_script.py`
  - 新建，测试索引脚本生成 PostgreSQL 和 SQLite 语句。

---

## Task 1: 优化基金列表历史净值查询

**目标：** `get_funds_with_grouping()` 不再把当前页基金的全部历史净值拉到内存，而是在 SQL 层限制每只基金最近 5 条。

**Files:**
- Modify: `src/services/fund_service.py:230-353`
- Create: `tests/unit/test_fund_recent_history.py`

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_fund_recent_history.py`:

```python
from datetime import date, timedelta

from src.models.database import FundInfo, FundHistory
from src.services.fund_service import FundService


def test_get_funds_with_grouping_returns_only_recent_five_history_items(test_db):
    fund = FundInfo(
        fund_code="000001",
        fund_name="测试基金",
        sector_type="测试板块",
        latest_nav=1.0,
        day_growth=0.1,
    )
    test_db.add(fund)

    base_date = date(2026, 6, 1)
    for i in range(10):
        test_db.add(FundHistory(
            fund_code="000001",
            fund_name="测试基金",
            nav_date=base_date - timedelta(days=i),
            nav=1.0 + i,
            day_growth=0.1,
        ))
    test_db.commit()

    result = FundService(test_db).get_funds_with_grouping(group_by_sector=False)

    assert len(result) == 1
    assert [item["date"] for item in result[0]["recent_history"]] == [
        "2026-06-01",
        "2026-05-31",
        "2026-05-30",
        "2026-05-29",
        "2026-05-28",
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/unit/test_fund_recent_history.py -v
```

Expected: FAIL。原因应是 helper 尚不存在或实现尚未限制 SQL 层最近 5 条。若测试直接通过，补充一个针对 helper 的测试：要求 `FundService` 存在 `_get_recent_history_map(fund_codes, per_fund=5)`。

- [ ] **Step 3: 最小实现 helper**

Modify `src/services/fund_service.py`，在 `get_funds_with_grouping()` 前新增：

```python
    def _get_recent_history_map(self, fund_codes: List[str], per_fund: int = 5) -> Dict[str, List[FundHistory]]:
        if not fund_codes:
            return {}

        from sqlalchemy import func

        ranked_history = self.db.query(
            FundHistory.id.label("id"),
            func.row_number().over(
                partition_by=FundHistory.fund_code,
                order_by=FundHistory.nav_date.desc()
            ).label("row_number")
        ).filter(
            FundHistory.fund_code.in_(fund_codes)
        ).subquery()

        rows = self.db.query(FundHistory).join(
            ranked_history,
            FundHistory.id == ranked_history.c.id
        ).filter(
            ranked_history.c.row_number <= per_fund
        ).order_by(
            FundHistory.fund_code,
            FundHistory.nav_date.desc()
        ).all()

        history_map = {}
        for item in rows:
            history_map.setdefault(item.fund_code, []).append(item)
        return history_map
```

Then replace lines equivalent to current all-history block:

```python
        all_history = self.db.query(FundHistory).filter(
            FundHistory.fund_code.in_(fund_codes)
        ).order_by(FundHistory.fund_code, FundHistory.nav_date.desc()).all()

        history_map = {}
        for h in all_history:
            if h.fund_code not in history_map:
                history_map[h.fund_code] = []
            if len(history_map[h.fund_code]) < 5:
                history_map[h.fund_code].append(h)
```

with:

```python
        history_map = self._get_recent_history_map(fund_codes, per_fund=5)
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
pytest tests/unit/test_fund_recent_history.py tests/unit/test_services/test_services.py::TestFundService -v
```

Expected: PASS。

- [ ] **Step 5: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 6: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add src/services/fund_service.py tests/unit/test_fund_recent_history.py
git commit -m "perf: 优化基金历史净值查询"
```

---

## Task 2: 基金全量更新后台任务化

**目标：** `POST /api/funds/update-all` 立即返回任务已启动，不再阻塞请求线程；新增状态查询接口。

**Files:**
- Create: `src/services/fund_update_task.py`
- Create: `tests/unit/test_fund_update_task.py`
- Modify: `src/api/routes/funds.py:99-105`
- Create: `tests/integration/test_fund_update_api.py`

- [ ] **Step 1: 写任务状态机失败测试**

Create `tests/unit/test_fund_update_task.py`:

```python
from src.services.fund_update_task import FundUpdateTask


def test_start_rejects_second_run_while_running():
    task = FundUpdateTask()

    first = task.start(lambda: {"success": True, "message": "ok"}, run_inline=True, keep_running=True)
    second = task.start(lambda: {"success": True}, run_inline=True)

    assert first["success"] is True
    assert first["data"]["in_progress"] is True
    assert second["success"] is False
    assert "正在进行" in second["message"]


def test_status_records_success_after_inline_run():
    task = FundUpdateTask()

    task.start(lambda: {"success": True, "message": "同步完成"}, run_inline=True)
    status = task.status()

    assert status["in_progress"] is False
    assert status["last_result"]["success"] is True
    assert status["last_result"]["message"] == "同步完成"
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/unit/test_fund_update_task.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.services.fund_update_task'`。

- [ ] **Step 3: 新增后台任务服务**

Create `src/services/fund_update_task.py`:

```python
from datetime import datetime
from threading import Lock, Thread
from typing import Callable, Dict


class FundUpdateTask:
    def __init__(self):
        self._lock = Lock()
        self._in_progress = False
        self._started_at = None
        self._finished_at = None
        self._last_result = None

    def start(self, runner: Callable[[], Dict], run_inline: bool = False, keep_running: bool = False) -> Dict:
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "基金更新正在进行中，请稍后再试", "data": self.status()}

        self._in_progress = True
        self._started_at = datetime.now()
        self._finished_at = None
        self._last_result = None

        def execute():
            try:
                self._last_result = runner()
            except Exception as exc:
                self._last_result = {"success": False, "message": f"更新失败: {exc}"}
            finally:
                if not keep_running:
                    self._in_progress = False
                    self._finished_at = datetime.now()
                    self._lock.release()

        if run_inline:
            execute()
        else:
            Thread(target=execute, daemon=True).start()

        return {"success": True, "message": "基金更新任务已启动", "data": self.status()}

    def status(self) -> Dict:
        return {
            "in_progress": self._in_progress,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "finished_at": self._finished_at.isoformat() if self._finished_at else None,
            "last_result": self._last_result,
        }


fund_update_task = FundUpdateTask()
```

- [ ] **Step 4: 运行任务状态机测试**

Run:

```bash
pytest tests/unit/test_fund_update_task.py -v
```

Expected: PASS。

- [ ] **Step 5: 写 API 失败测试**

Create `tests/integration/test_fund_update_api.py`:

```python
import os
from fastapi.testclient import TestClient

AUTH_HEADERS = {"X-Access-Password": os.getenv("ACCESS_PASSWORD", "test_password_123")}


def test_update_all_funds_starts_background_task(monkeypatch):
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app
    from src.api.routes import funds as funds_routes

    def fake_start(runner, run_inline=False):
        return {"success": True, "message": "基金更新任务已启动", "data": {"in_progress": True}}

    monkeypatch.setattr(funds_routes.fund_update_task, "start", fake_start)

    client = TestClient(app)
    response = client.post("/api/funds/update-all", headers=AUTH_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["in_progress"] is True


def test_get_update_status_returns_task_state(monkeypatch):
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app
    from src.api.routes import funds as funds_routes

    monkeypatch.setattr(
        funds_routes.fund_update_task,
        "status",
        lambda: {"in_progress": False, "started_at": None, "finished_at": None, "last_result": None},
    )

    client = TestClient(app)
    response = client.get("/api/funds/update-status", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["data"]["in_progress"] is False
```

- [ ] **Step 6: 运行 API 测试确认失败**

Run:

```bash
pytest tests/integration/test_fund_update_api.py -v
```

Expected: FAIL because route still calls `FundService.update_all_funds()` synchronously and no `/update-status` exists。

- [ ] **Step 7: 修改基金路由**

Modify `src/api/routes/funds.py` imports:

```python
from src.models.database import SessionLocal
from src.services.fund_update_task import fund_update_task
```

Replace route body at `update_all_funds` with:

```python
def update_all_funds(db: Session = Depends(get_db)):
    """启动基金数据后台更新"""
    def runner():
        worker_db = SessionLocal()
        try:
            return FundService(worker_db).update_all_funds()
        finally:
            worker_db.close()

    return fund_update_task.start(runner)
```

Add route below it:

```python
@router.get("/update-status")
def get_fund_update_status():
    """获取基金更新任务状态"""
    return {
        "success": True,
        "data": fund_update_task.status()
    }
```

Do not reuse request-scoped `db` inside the background thread.

- [ ] **Step 8: 运行 API 测试确认通过**

Run:

```bash
pytest tests/unit/test_fund_update_task.py tests/integration/test_fund_update_api.py -v
```

Expected: PASS。

- [ ] **Step 9: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 10: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add src/services/fund_update_task.py src/api/routes/funds.py tests/unit/test_fund_update_task.py tests/integration/test_fund_update_api.py
git commit -m "perf: 后台执行基金全量更新"
```

---

## Task 3: 前端基金更新改为状态轮询

**目标：** 前端点击“更新全部基金”后不再等待 300 秒请求完成，而是启动任务后轮询 `/api/funds/update-status`。

**Files:**
- Modify: `web/index.html`
- Create: `tests/unit/test_frontend_fund_update.py`

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_frontend_fund_update.py`:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_update_all_funds_uses_status_polling():
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "/api/funds/update-status" in content
    assert "pollFundUpdateStatus" in content
    assert "timeout: 300000" not in content
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/unit/test_frontend_fund_update.py -v
```

Expected: FAIL because current frontend still posts with `{ timeout: 300000 }` and has no polling helper。

- [ ] **Step 3: 修改前端基金更新函数**

In `web/index.html`, replace current `updateAllFunds` function with:

```javascript
                const pollFundUpdateStatus = async () => {
                    let checks = 0;
                    const maxChecks = 120;
                    const interval = setInterval(async () => {
                        checks++;
                        try {
                            const statusRes = await axios.get('/api/funds/update-status');
                            const status = statusRes.data.data;
                            if (!status.in_progress || checks >= maxChecks) {
                                clearInterval(interval);
                                analyzing.value = false;
                                await fetchFunds();
                                if (status.last_result?.message) {
                                    alert(status.last_result.message);
                                } else if (checks >= maxChecks) {
                                    alert('基金更新仍在进行中，请稍后刷新查看结果');
                                }
                            }
                        } catch (e) {
                            if (checks >= maxChecks) {
                                clearInterval(interval);
                                analyzing.value = false;
                                alert('获取更新状态失败，请稍后刷新查看结果');
                            }
                        }
                    }, 5000);
                };

                const updateAllFunds = async () => {
                    analyzing.value = true;
                    try {
                        const res = await axios.post('/api/funds/update-all');
                        if (res.data.success) {
                            alert(res.data.message || '基金更新任务已启动');
                            await pollFundUpdateStatus();
                        } else {
                            analyzing.value = false;
                            alert(res.data.message || '更新失败');
                        }
                    } catch (e) {
                        analyzing.value = false;
                        alert('更新失败: ' + (e.response?.data?.message || e.message));
                    }
                };
```

Add `pollFundUpdateStatus` to the returned object only if the template directly needs it. If it is only called internally by `updateAllFunds`，do not export it。

- [ ] **Step 4: 运行前端测试**

Run:

```bash
pytest tests/unit/test_frontend_fund_update.py tests/unit/test_frontend_loading.py -v
```

Expected: PASS。

- [ ] **Step 5: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 6: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add web/index.html tests/unit/test_frontend_fund_update.py
git commit -m "perf: 前端轮询基金更新状态"
```

---

## Task 4: 列表接口默认分页收紧

**目标：** 数据增长后，帖子、预测、基金、观点列表不默认返回 1000 条；前端显式按视图加载分页数据。

**Files:**
- Modify: `src/api/routes/posts.py`
- Modify: `src/api/routes/predictions.py`
- Modify: `src/api/routes/funds.py`
- Modify: `src/api/routes/viewpoints.py`
- Modify: `web/index.html`
- Create: `tests/integration/test_pagination_limits.py`
- Extend: `tests/unit/test_frontend_loading.py`

- [ ] **Step 1: 写后端分页失败测试**

Create `tests/integration/test_pagination_limits.py`:

```python
import os
from fastapi.testclient import TestClient

AUTH_HEADERS = {"X-Access-Password": os.getenv("ACCESS_PASSWORD", "test_password_123")}


def test_posts_default_limit_is_100():
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.routes import posts

    assert posts.get_posts.__signature__.parameters["limit"].default == 100


def test_predictions_default_limit_is_100():
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.routes import predictions

    assert predictions.get_predictions.__signature__.parameters["limit"].default == 100


def test_funds_default_limit_is_100():
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.routes import funds

    assert funds.get_funds.__signature__.parameters["limit"].default == 100
```

If FastAPI wraps signatures differently in this project, replace signature assertions with TestClient calls and monkeypatch service methods to capture passed `limit`。

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/integration/test_pagination_limits.py -v
```

Expected: FAIL because defaults are still 1000。

- [ ] **Step 3: 修改后端默认 limit**

Change defaults:

```python
# src/api/routes/posts.py
limit: int = 100

# src/api/routes/predictions.py
limit: int = 100

# src/api/routes/funds.py
limit: int = 100

# src/api/routes/funds.py by-sector route
limit: int = 100
```

For routes already using `Query`，use:

```python
limit: int = Query(100, ge=1, le=1000)
```

Do not remove explicit `skip` and `limit` support。

- [ ] **Step 4: 更新前端显式请求**

In `web/index.html`, update fetch functions:

```javascript
                const fetchPosts = async () => { const res = await axios.get('/api/posts?limit=100'); if (res.data.success) posts.value = res.data.data; };
                const fetchPredictions = async () => { const res = await axios.get('/api/predictions?limit=100'); if (res.data.success) predictions.value = res.data.data; };
                const fetchViewpoints = async () => { const res = await axios.get('/api/viewpoints?limit=100'); if (res.data.success) viewpoints.value = res.data.data; };
                const fetchFunds = async () => { const res = await axios.get('/api/funds?group_by_sector=false&limit=100'); if (res.data.success) funds.value = res.data.data.sort((a, b) => (b.day_growth || 0) - (a.day_growth || 0)); };
```

This task intentionally does not add a “加载更多” UI. That is a separate UX task if users need old full-list behavior。

- [ ] **Step 5: 运行分页和前端测试**

Run:

```bash
pytest tests/integration/test_pagination_limits.py tests/unit/test_frontend_loading.py -v
```

Expected: PASS。

- [ ] **Step 6: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 7: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add src/api/routes/posts.py src/api/routes/predictions.py src/api/routes/funds.py src/api/routes/viewpoints.py web/index.html tests/integration/test_pagination_limits.py
git commit -m "perf: 收紧列表接口默认分页"
```

---

## Task 5: 优化 sync_missing_funds 扫描和循环查询

**目标：** `sync_missing_funds()` 不再处理所有未删除预测，不再在循环里反复查询已存在基金。

**Files:**
- Modify: `src/fund/fund_sync_manager.py:108-286`
- Extend: `tests/unit/test_fund_sync_manager.py` or create `tests/unit/test_sync_missing_funds.py`

- [ ] **Step 1: 写失败测试**

Create `tests/unit/test_sync_missing_funds.py`:

```python
from datetime import date

from src.fund.fund_sync_manager import FundSyncManager
from src.models.database import Blogger, FundInfo, Post, Prediction


def test_sync_missing_funds_links_existing_fund_without_loop_query(test_db, monkeypatch):
    blogger = Blogger(name="测试博主")
    test_db.add(blogger)
    test_db.flush()

    post = Post(blogger_id=blogger.id, content="看好白酒", post_date=date.today())
    test_db.add(post)
    test_db.flush()

    fund = FundInfo(fund_code="161725", fund_name="白酒基金", sector_type="白酒")
    test_db.add(fund)
    pred = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        sector="白酒",
        prediction_type="up",
        prediction_date=date.today(),
        is_deleted=False,
    )
    test_db.add(pred)
    test_db.commit()

    def fail_external_call(*args, **kwargs):
        raise AssertionError("已有基金时不应调用外部基金 API")

    monkeypatch.setattr("src.fund.fund_sync_manager.fund_api.get_fund_info", fail_external_call)

    result = FundSyncManager().sync_missing_funds(test_db)

    assert result["linked"] == 1
    assert result["checked"] == 1
    test_db.refresh(pred)
    assert pred.fund_code == "161725"
```

- [ ] **Step 2: 运行测试确认当前行为**

Run:

```bash
pytest tests/unit/test_sync_missing_funds.py -v
```

Expected: 若当前代码通过，继续补第二个测试：构造一个已经有 `fund_code` 且对应基金存在的预测，断言不发生循环内查询。可通过 SQLAlchemy event 计数实现。

- [ ] **Step 3: 修改查询范围和预加载映射**

In `src/fund/fund_sync_manager.py`, replace the top of `sync_missing_funds()` with:

```python
        predictions = db.query(Prediction).filter(
            Prediction.is_deleted == False,
            Prediction.status == 'pending'
        ).all()

        existing_funds = db.query(FundInfo).all()
        existing_sectors = {f.sector_type: f for f in existing_funds if f.sector_type}
        existing_codes = {f.fund_code: f for f in existing_funds if f.fund_code}
```

Replace loop-internal query:

```python
                existing = db.query(FundInfo).filter(FundInfo.fund_code == pred.fund_code).first()
```

with:

```python
                existing = existing_codes.get(pred.fund_code)
```

When adding a new fund, update both maps:

```python
                        existing_sectors[sector] = new_fund
                        existing_codes[new_fund.fund_code] = new_fund
```

- [ ] **Step 4: 限制 details 返回规模**

Add helper inside method before loop:

```python
        def add_detail(detail):
            if len(result["details"]) < 100:
                result["details"].append(detail)
```

Replace every `result["details"].append({...})` in this method with `add_detail({...})`。

- [ ] **Step 5: 运行同步测试**

Run:

```bash
pytest tests/unit/test_sync_missing_funds.py tests/unit/test_fund_sync_manager.py -v
```

Expected: PASS。

- [ ] **Step 6: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 7: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add src/fund/fund_sync_manager.py tests/unit/test_sync_missing_funds.py
git commit -m "perf: 减少基金同步扫描和循环查询"
```

---

## Task 6: 优化市场情绪接口聚合查询

**目标：** `/api/market-sentiment` 不再拉取近 7 天所有观点对象来统计情绪数量。

**Files:**
- Modify: `src/api/main.py:286-348`
- Create: `tests/unit/test_market_sentiment.py`

- [ ] **Step 1: 写输出行为测试**

Create `tests/unit/test_market_sentiment.py`:

```python
from datetime import date
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.database import Viewpoint


def test_market_sentiment_returns_expected_summary(test_db, monkeypatch):
    from src.api import main as api_main

    class TestSessionLocal:
        def __call__(self):
            return test_db

    monkeypatch.setattr(api_main, "SessionLocal", TestSessionLocal(), raising=False)

    test_db.add(Viewpoint(
        content="看好白酒",
        market_direction="bullish",
        viewpoint_date=date.today(),
        sectors_bullish=["白酒"],
        is_deleted=False,
    ))
    test_db.add(Viewpoint(
        content="看空新能源",
        market_direction="bearish",
        viewpoint_date=date.today(),
        sectors_bearish=["新能源"],
        is_deleted=False,
    ))
    test_db.commit()

    client = TestClient(app)
    response = client.get("/api/market-sentiment", headers={"X-Access-Password": "test_password_123"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bullish_count"] == 1
    assert data["bearish_count"] == 1
    assert data["neutral_count"] == 0
```

If authentication environment differs, set `os.environ["ACCESS_PASSWORD"] = "test_password_123"` before importing app。

- [ ] **Step 2: 运行测试并确认当前行为**

Run:

```bash
pytest tests/unit/test_market_sentiment.py -v
```

Expected: PASS or authentication-related failure。If auth failure occurs, fix the test setup only，不改生产代码。

- [ ] **Step 3: 改为 SQL 聚合情绪数量**

In `src/api/main.py`, import inside function:

```python
from sqlalchemy import func
```

Replace full-object count section with:

```python
        cutoff = date.today() - timedelta(days=7)
        direction_rows = db.query(
            Viewpoint.market_direction,
            func.count(Viewpoint.id)
        ).filter(
            Viewpoint.viewpoint_date >= cutoff,
            Viewpoint.is_deleted == False
        ).group_by(Viewpoint.market_direction).all()

        direction_counts = {direction: count for direction, count in direction_rows}
        bullish_count = direction_counts.get('bullish', 0)
        bearish_count = direction_counts.get('bearish', 0)
        neutral_count = direction_counts.get('neutral', 0)
        total = bullish_count + bearish_count + neutral_count
```

Keep sector hot list as a limited field query:

```python
        sector_rows = db.query(
            Viewpoint.sectors_bullish,
            Viewpoint.sectors_bearish
        ).filter(
            Viewpoint.viewpoint_date >= cutoff,
            Viewpoint.is_deleted == False
        ).limit(500).all()
```

Then compute `sector_counts` from `sector_rows` instead of full `Viewpoint` objects。

- [ ] **Step 4: 运行市场情绪测试**

Run:

```bash
pytest tests/unit/test_market_sentiment.py -v
```

Expected: PASS。

- [ ] **Step 5: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 6: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add src/api/main.py tests/unit/test_market_sentiment.py
git commit -m "perf: 优化市场情绪聚合查询"
```

---

## Task 7: 安全添加 fund_info 性能索引

**目标：** 为 `fund_info.sector_type` 和 `fund_info.active_predictions` 提供安全、可重复执行的索引脚本，不在应用启动时做重 DDL。

**Files:**
- Create: `scripts/add_performance_indexes.py`
- Create: `tests/unit/test_performance_index_script.py`

- [ ] **Step 1: 写索引语句生成测试**

Create `tests/unit/test_performance_index_script.py`:

```python
from scripts.add_performance_indexes import build_index_statements


def test_build_postgresql_index_statements():
    statements = build_index_statements("postgresql")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fund_info_sector_type ON fund_info (sector_type)" in statements
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fund_info_active_predictions ON fund_info (active_predictions)" in statements


def test_build_sqlite_index_statements():
    statements = build_index_statements("sqlite")

    assert "CREATE INDEX IF NOT EXISTS ix_fund_info_sector_type ON fund_info (sector_type)" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_fund_info_active_predictions ON fund_info (active_predictions)" in statements
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
pytest tests/unit/test_performance_index_script.py -v
```

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 新增索引脚本**

Create `scripts/add_performance_indexes.py`:

```python
from sqlalchemy import text

from src.models.database import engine


INDEXES = [
    ("ix_fund_info_sector_type", "fund_info", "sector_type"),
    ("ix_fund_info_active_predictions", "fund_info", "active_predictions"),
]


def build_index_statements(db_type: str):
    concurrent = " CONCURRENTLY" if db_type.startswith("postgresql") else ""
    return [
        f"CREATE INDEX{concurrent} IF NOT EXISTS {name} ON {table} ({column})"
        for name, table, column in INDEXES
    ]


def main():
    db_type = str(engine.url).split("://")[0]
    statements = build_index_statements(db_type)

    if db_type.startswith("postgresql"):
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    else:
        connection = engine.connect()

    try:
        for statement in statements:
            print(statement)
            connection.execute(text(statement))
        if not db_type.startswith("postgresql"):
            connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行索引脚本测试**

Run:

```bash
pytest tests/unit/test_performance_index_script.py -v
```

Expected: PASS。

- [ ] **Step 5: 本地只打印或测试，不直接操作生产库**

Run only in local/test environment:

```bash
python scripts/add_performance_indexes.py
```

Expected: prints the two CREATE INDEX statements and succeeds locally。

Production Supabase execution must be a separate manual deployment step after user approval。

- [ ] **Step 6: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 7: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add scripts/add_performance_indexes.py tests/unit/test_performance_index_script.py
git commit -m "perf: 添加基金查询索引脚本"
```

---

## Task 8: 前端大文件渐进拆分评估

**目标：** 不在性能逻辑任务中重构 `web/index.html`；等上述功能稳定后，单独评估是否拆分 API helper 和通用 UI helper。

**Files:**
- Candidate create: `web/api.js`
- Candidate create: `web/ui-helpers.js`
- Candidate modify: `web/index.html`
- Candidate test: `tests/unit/test_frontend_static_assets.py`

- [ ] **Step 1: 先统计当前重复和文件大小**

Run:

```bash
python - <<'PY'
from pathlib import Path
p = Path('web/index.html')
print(p.stat().st_size)
print(len(p.read_text(encoding='utf-8').splitlines()))
PY
```

Expected: print byte size and line count。

- [ ] **Step 2: 只拆无业务逻辑 helper**

Only extract pure request wrappers if this reduces `index.html` meaningfully without changing behavior。Do not move Vue templates in this task。

- [ ] **Step 3: 添加静态引用测试**

Create `tests/unit/test_frontend_static_assets.py`:

```python
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[2] / "web" / "index.html"


def test_index_references_local_frontend_helpers_if_split():
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "/web/vue.global.prod.js" in content
    assert "/web/axios.min.js" in content
```

- [ ] **Step 4: 运行完整测试**

Run:

```bash
pytest tests -v
```

Expected: PASS。

- [ ] **Step 5: 提交检查点**

只有在用户明确允许提交时执行：

```bash
git add web/index.html web/api.js web/ui-helpers.js tests/unit/test_frontend_static_assets.py
git commit -m "refactor: 拆分前端公共 helper"
```

---

## 推荐执行顺序

1. Task 1：基金列表历史净值查询优化，收益高，风险低。
2. Task 2：基金全量更新后台任务化，收益最高，后端先做。
3. Task 3：前端基金更新轮询，配合 Task 2 完成用户体验闭环。
4. Task 4：列表接口默认分页收紧，可能影响“看全部”的使用习惯，执行前最好确认用户接受默认只看前 100 条。
5. Task 5：同步缺失基金扫描优化，属于后台任务性能优化。
6. Task 6：市场情绪聚合查询优化，风险低但收益取决于观点数据量。
7. Task 7：索引脚本，生产执行前必须单独确认。
8. Task 8：前端拆分，最后做，避免和性能修复混杂。

---

## 全局验证要求

每个 Task 完成后至少运行：

```bash
pytest tests -v
```

涉及前端交互的 Task，还应本地启动服务并手动验证：

```bash
python -m src --port 8002
```

浏览器验证路径：

1. 登录首页。
2. 进入对应视图。
3. 确认首屏不预加载重数据。
4. 确认配置、投资建议、基金更新仍可正常使用。

如果不能进行浏览器验证，必须在汇报中明确说明未做 UI 手动验证，不能声称前端体验完全验证。

---

## 自检结果

- Spec coverage：覆盖剩余优化建议中的基金历史查询、后台更新、前端轮询、分页、同步扫描、市场情绪聚合、索引脚本和前端拆分评估。
- Placeholder scan：无 TBD/TODO/implement later。Task 8 是评估型任务，明确了不默认做业务拆分。
- Type consistency：使用现有 `FundService`、`FundSyncManager`、`FundInfo`、`FundHistory`、`Viewpoint`、`SessionLocal`、`axios` 命名；新增 `FundUpdateTask` 和 `fund_update_task` 在 Task 2 中定义后再被路由引用。
- Safety：生产 Supabase 索引执行被明确标为手动审批步骤；计划不包含删除数据、重置数据库或回滚用户既有改动。
