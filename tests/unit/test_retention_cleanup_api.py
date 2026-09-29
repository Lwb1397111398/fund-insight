import os
import stat
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.models.database import (
    Base,
    Blogger,
    CleanupTask,
    FundHistory,
    FundInfo,
    Post,
    Prediction,
    Viewpoint,
)


AUTH_HEADERS = {"X-Access-Password": "cleanup-api-test"}


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'cleanup-api.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _client(monkeypatch, session_factory):
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return app, TestClient(app)


def test_cleanup_preview_exposes_policy_fingerprint_and_protection(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    test_db = session_factory()
    blogger = Blogger(name="API 清理测试", platform="test")
    test_db.add(blogger)
    test_db.flush()
    post = Post(
        blogger_id=blogger.id,
        content="长期预测",
        post_date=date.today(),
        analyzed=True,
    )
    test_db.add(post)
    test_db.flush()
    test_db.add(Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="bullish",
        prediction_date=date.today(),
        target_date=date(date.today().year + 1, 12, 31),
        status="pending",
        is_deleted=False,
    ))
    test_db.commit()
    test_db.close()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get("/api/config/cleanup/preview", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rule_version"] == "retention-v2"
    assert len(data["preview_fingerprint"]) == 64
    assert data["protected_counts"]["pending_predictions"] == 1
    assert data["protected_counts"]["long_term_predictions"] == 1
    assert all(len(samples) <= 20 for samples in data["samples"].values())


def test_cleanup_execute_rejects_stale_fingerprint_before_creating_task(
    monkeypatch, tmp_path
):
    session_factory = _database(tmp_path)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        response = client.post(
            "/api/config/cleanup",
            headers=headers,
            json={"preview_fingerprint": "stale"},
        )
    finally:
        app.dependency_overrides.clear()

    # 旧执行器硬删已下线：直接 403，不再走到指纹校验
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail.get("hard_delete_disabled") is True or "hard_delete" in str(detail).lower()
    with session_factory() as db:
        assert db.query(CleanupTask).count() == 0


def test_cleanup_task_status_returns_404_for_unknown_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        response = client.get(
            "/api/config/cleanup/tasks/missing", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_cleanup_status_marks_stale_pending_task_failed(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(CleanupTask(
            task_id="stale-pending",
            status="pending",
            progress=0,
            created_at=datetime.now() - timedelta(minutes=6),
        ))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/tasks/stale-pending", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert "中断" in data["error"] or "超时" in data["error"]
    assert data["completed_at"] is not None


def test_cleanup_status_keeps_recent_pending_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(CleanupTask(
            task_id="recent-pending",
            status="pending",
            progress=0,
            created_at=datetime.now(),
        ))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/tasks/recent-pending", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.json()["data"]["status"] == "pending"


def test_cleanup_background_task_reaches_completed(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    from src.api.routes.config import _run_cleanup_background
    from src.services import retention_cleanup_service as rcs
    from src.services.retention_cleanup_service import RetentionCleanupService

    # 单测临时打开旧硬删，验证后台任务链路仍可用（生产默认关闭）
    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)

    with session_factory() as db:
        fingerprint = RetentionCleanupService(db).build_plan().fingerprint
        db.add(CleanupTask(
            task_id="background-test",
            status="pending",
            progress=0,
            current_item=0,
            total_items=0,
            cleanup_params={"preview_fingerprint": fingerprint},
        ))
        db.commit()

    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    monkeypatch.setattr(RetentionCleanupService, "_create_backup", lambda self: None)
    _run_cleanup_background("background-test", fingerprint)

    with session_factory() as db:
        task = db.query(CleanupTask).filter_by(task_id="background-test").one()
        assert task.status == "completed"
        assert task.progress == 100
        assert task.result["success"] is True


def _seed_three_bucket_rows(session_factory):
    """一条可删软删预测 + 一条已有结论的软删预测（护栏）+ 一条软删观点。"""
    with session_factory() as db:
        blogger = Blogger(name="三桶 API", platform="test")
        db.add(blogger)
        db.flush()
        post = Post(
            blogger_id=blogger.id,
            content="three bucket api",
            post_date=date.today() - timedelta(days=120),
            analyzed=True,
        )
        db.add(post)
        db.flush()
        deletable = Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="TBAPI1",
            prediction_type="up",
            prediction_date=date.today() - timedelta(days=120),
            target_date=date.today() - timedelta(days=100),
            status="pending",
            is_deleted=True,
            deleted_at=datetime.now() - timedelta(days=60),
            is_correct=None,
        )
        ledger = Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="TBAPI2",
            prediction_type="up",
            prediction_date=date.today() - timedelta(days=120),
            target_date=date.today() - timedelta(days=100),
            status="failed",
            is_deleted=True,
            deleted_at=datetime.now() - timedelta(days=60),
            is_correct=False,
            verify_count=1,
        )
        viewpoint = Viewpoint(
            blogger_id=blogger.id,
            content="soft deleted viewpoint",
            author="t",
            source="test",
            viewpoint_date=date.today() - timedelta(days=90),
            valid_until=date.today() + timedelta(days=200),
            is_deleted=True,
            deleted_at=datetime.now() - timedelta(days=45),
            is_summary=False,
        )
        db.add_all([deletable, ledger, viewpoint])
        db.commit()
        return {
            "deletable_id": deletable.id,
            "ledger_id": ledger.id,
            "viewpoint_id": viewpoint.id,
        }


def test_three_bucket_preview_lists_candidates_and_ledger_guard(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    ids = _seed_three_bucket_rows(session_factory)
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rule_version"] == "three-buckets-v2"
    assert len(data["preview_fingerprint"]) == 64
    assert data["counts"]["deleted_predictions"] == 1
    assert data["counts"]["deleted_viewpoints"] == 1
    assert data["protected_counts"]["verified_ledger_excluded"] == 1
    assert data["labels"]["deleted_predictions"]
    sample_ids = [row["id"] for row in data["samples"]["deleted_predictions"]]
    assert ids["deletable_id"] in sample_ids
    assert ids["ledger_id"] not in sample_ids


def test_three_bucket_execute_requires_confirmation_header(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_three_bucket_rows(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=AUTH_HEADERS,
            json={"preview_fingerprint": "whatever"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert "确认" in response.json()["detail"]
    with session_factory() as db:
        assert db.query(CleanupTask).count() == 0


def test_three_bucket_execute_rejects_stale_fingerprint(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_three_bucket_rows(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={"preview_fingerprint": "stale"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["current_fingerprint"]
    with session_factory() as db:
        assert db.query(CleanupTask).count() == 0


def test_three_bucket_execute_deletes_only_unverified_rows(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    ids = _seed_three_bucket_rows(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        preview = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        ).json()["data"]
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={"preview_fingerprint": preview["preview_fingerprint"]},
        )
        task_id = response.json()["data"]["task_id"]
        status = client.get(
            f"/api/config/cleanup/tasks/{task_id}", headers=AUTH_HEADERS
        ).json()["data"]
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert status["status"] == "completed", status.get("error")
    assert status["result"]["total_deleted"] == 2
    assert status["result"]["deleted_counts"]["deleted_predictions"] == 1
    assert status["result"]["deleted_counts"]["deleted_viewpoints"] == 1
    with session_factory() as db:
        assert db.get(Prediction, ids["deletable_id"]) is None
        assert db.get(Prediction, ids["ledger_id"]) is not None
        assert db.get(Viewpoint, ids["viewpoint_id"]) is None


def test_three_bucket_execute_returns_completed_when_nothing_to_delete(
    monkeypatch, tmp_path
):
    session_factory = _database(tmp_path)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        preview = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        ).json()["data"]
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={"preview_fingerprint": preview["preview_fingerprint"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"]["task_id"] is None
    assert response.json()["data"]["total_items"] == 0


def test_three_bucket_execute_rejects_unknown_bucket(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_three_bucket_rows(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        preview = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        ).json()["data"]
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={
                "preview_fingerprint": preview["preview_fingerprint"],
                "buckets": ["nope"],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_three_bucket_execute_is_disabled_when_cleanup_switch_off(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_three_bucket_rows(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "false")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={"preview_fingerprint": "x"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert "已禁用" in response.json()["detail"]


def _seed_orphan_fund(session_factory, *, code: str = "APIORPH", history_rows: int = 6):
    with session_factory() as db:
        db.add(FundInfo(
            fund_code=code,
            fund_name="接口孤儿基金",
            updated_at=datetime.now() - timedelta(days=200),
        ))
        for i in range(history_rows):
            db.add(FundHistory(
                fund_code=code,
                fund_name="接口孤儿基金",
                nav_date=date.today() - timedelta(days=i + 1),
                nav=1.0 + i / 100,
            ))
        db.commit()


def test_three_bucket_preview_reports_cascade_rows_and_table_sizes(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_orphan_fund(session_factory)
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    data = response.json()["data"]
    assert data["counts"]["orphan_funds"] == 1
    # 净值不计入 counts，但必须以 cascade 报出，否则用户看不出真实删除量
    assert data["cascade_counts"]["fund_history"] == 6
    assert data["total_rows_removed"] == data["total"] + 6
    assert data["table_sizes"]["fund_history"] == 6
    assert data["labels"]["stale_fund_history"]


def test_three_bucket_execute_removes_orphan_fund_and_reclaims_space(
    monkeypatch, tmp_path
):
    session_factory = _database(tmp_path)
    _seed_orphan_fund(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        preview = client.get(
            "/api/config/cleanup/three-buckets/preview", headers=AUTH_HEADERS
        ).json()["data"]
        response = client.post(
            "/api/config/cleanup/three-buckets",
            headers=headers,
            json={"preview_fingerprint": preview["preview_fingerprint"]},
        )
        task_id = response.json()["data"]["task_id"]
        status = client.get(
            f"/api/config/cleanup/tasks/{task_id}", headers=AUTH_HEADERS
        ).json()["data"]
    finally:
        app.dependency_overrides.clear()

    assert status["status"] == "completed", status.get("error")
    assert status["result"]["deleted_counts"]["orphan_funds"] == 1
    assert status["result"]["cascade_counts"]["fund_history"] == 6
    assert status["result"]["total_rows_removed"] >= 7
    # 落盘的 sqlite 能真正 VACUUM，应报出释放量
    reclaim = status["result"]["space_reclaim"]
    assert reclaim["dialect"] == "sqlite"
    assert reclaim["success"] is True
    with session_factory() as db:
        assert db.query(FundInfo).filter_by(fund_code="APIORPH").first() is None
        assert db.query(FundHistory).filter_by(fund_code="APIORPH").count() == 0


def test_reclaim_space_endpoint_requires_confirmation(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.post("/api/config/cleanup/reclaim-space", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert "确认" in response.json()["detail"]


def test_reclaim_space_endpoint_vacuums_without_deleting(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    _seed_orphan_fund(session_factory)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        response = client.post("/api/config/cleanup/reclaim-space", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"]["dialect"] == "sqlite"
    # 这一趟是真跑了 VACUUM 的（落盘的 sqlite 文件），所以"完成"这一档归它
    assert response.json()["success"] is True
    assert "没跑" not in response.json()["message"]
    # 只回收空间，不动数据
    with session_factory() as db:
        assert db.query(FundInfo).filter_by(fund_code="APIORPH").first() is not None
        assert db.query(FundHistory).filter_by(fund_code="APIORPH").count() == 6


RECLAIM_HEADERS = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}


def _post_reclaim(monkeypatch, session_factory):
    """走一次真实路由，把「回收磁盘空间」这一步的回执拿回来。"""
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    try:
        return client.post(
            "/api/config/cleanup/reclaim-space", headers=RECLAIM_HEADERS
        )
    finally:
        app.dependency_overrides.clear()


def _assert_never_ran(response, fragment):
    """一次"没跑"的回执：既不许买到 success，也不许把话说成完成。"""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["skipped"] is True, body
    assert fragment in body["message"], body
    assert "完成" not in body["message"], body
    return body


def test_a_reclaim_that_never_ran_is_never_reported_as_done(monkeypatch, tmp_path):
    """「回收磁盘空间」有四种没跑的形状，每一种都得自己说一句人话。

    路由原来写的是 `success = 真跑了 or 跳过了` —— 开关关着、没有表、内存库、
    方言不支持这四种都会让页面长出"空间回收完成"，而空间一个字节都没还。
    """
    from src.api.routes import config as config_routes
    from src.services.retention_three_buckets import ThreeBucketRetentionService

    # ① 总开关关着（真 sqlite 文件库，只是这一步不跑）
    monkeypatch.setenv("ENABLE_SPACE_RECLAIM", "false")
    _assert_never_ran(
        _post_reclaim(monkeypatch, _database(tmp_path)), "回收开关（ENABLE_SPACE_RECLAIM）"
    )
    monkeypatch.delenv("ENABLE_SPACE_RECLAIM", raising=False)

    # ② 这次压根没有要回收的表（表名来自三桶那张表，路由在调用时现取）
    monkeypatch.setattr(ThreeBucketRetentionService, "BUCKET_TABLES", {})
    _assert_never_ran(_post_reclaim(monkeypatch, _database(tmp_path)), "没有要回收的表")
    monkeypatch.undo()

    # ③ 内存数据库：没有磁盘文件可回收
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    body = _assert_never_ran(
        _post_reclaim(monkeypatch, sessionmaker(bind=engine)), "内存数据库"
    )
    # 服务层那一份自己也不许同时带 success（两个互相打脸的旗标）
    assert not body["data"].get("success"), body["data"]
    engine.dispose()

    # ④ 方言不支持：两个库（sqlite / PostgreSQL）都是支持的，路由级走不到这一档，
    #    所以这一臂只钉那句话本身——它必须带出是哪个方言，不许退化成一句"没跑"。
    #    那句话的家在服务层（src/services.db_space.skip_detail），不在路由：两个消费方
    #    问的是同一个函数（路由现调 skip_detail(result)，页面读服务层预先盖好的
    #    reason_text），所以"翻译"这个动作全仓只有一处实现 —— 谁也不许自己再拼一遍。
    from src.services.db_space import skip_detail

    sentence = skip_detail({"reason": "unsupported_dialect:mysql"})
    assert "mysql" in sentence and "不支持" in sentence
    # 认不出的原因也不许静默变成空话
    assert "没跑" in skip_detail({"reason": "因为某种别的原因"})
    # 搬家的另一半：路由里不许留第二份句子表，否则同一个 `no_tables` 又会有两种说法
    assert not hasattr(config_routes, "_reclaim_skip_sentence")
    with open(config_routes.__file__, encoding="utf-8") as fh:
        assert "空间回收没跑" not in fh.read()


def test_a_reclaim_the_database_refused_says_why(tmp_path, monkeypatch):
    """数据库自己拒了 VACUUM：这一档要说"没有完成"并把它的原话带出来。"""
    path = tmp_path / "readonly-space.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    base = date(2026, 1, 1)
    session.bulk_save_objects(
        [
            FundHistory(
                fund_code=f"F{i % 10:03d}",
                fund_name="回收失败测试",
                nav_date=base + timedelta(days=i // 10),
                nav=1.0 + i / 10000,
            )
            for i in range(400)
        ]
    )
    session.commit()
    session.close()
    engine.dispose()
    # 只读文件 ⇒ sqlite 连不上写 ⇒ VACUUM 由数据库自己拒
    path.chmod(stat.S_IREAD)

    readonly_engine = create_engine(
        f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False}
    )
    try:
        response = _post_reclaim(monkeypatch, sessionmaker(bind=readonly_engine))
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        readonly_engine.dispose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    # "没做成"与"没跑"是两件事：这一档不该带 skipped
    assert not body.get("skipped")
    assert body["message"].startswith("空间回收没有完成")
    # 原因必须是数据库给的那句话，不许退化成"数据库没给出原因"
    assert "数据库没给出原因" not in body["message"]
    assert body["data"].get("error")


def test_a_pg_reclaim_failure_reaches_the_route_from_the_same_slot_the_page_reads(
    tmp_path, monkeypatch
):
    """Postgres 是按表 VACUUM 的：原因在 `tables[表].error`，页面读的是顶层 `error`。

    这一格量的是"路由与页面读同一个槽位"，两腿各问一次：
    ⑴ 服务把那句话算到顶层（今天 `_vacuum_postgres` 就是这么做的）⇒ 路由必须**原样**带出来。
        这里顶层那句与"按表自己拼一遍"的写法**故意不同**，路由要是绕过共用尺子去抄 tables，
        屏幕上会出现另一句话（两处两个口径 = 第 71 轮 #158 那条同族）。
    ⑵ 顶层没有（万一将来又漏了）⇒ 路由退到按表点名，也比"数据库没给出原因"强。
    """
    import src.services.db_space as db_space

    per_table = {
        "fund_history": {"mode": "failed", "success": False, "error": "被别的会话占着"},
        "predictions": {"mode": "failed", "success": False, "error": "超时被池子掐了"},
    }
    payloads = {
        "with_top_level": {
            "dialect": "postgresql", "mode": "per-table", "success": False,
            "error": "两张表都没还成空间（服务替页面算好的那一句）",
            "tables": per_table,
        },
        "only_tables": {
            "dialect": "postgresql", "mode": "per-table", "success": False,
            "tables": per_table,
        },
    }
    session_factory = _database(tmp_path)
    seen = []

    def fake_reclaim(db, tables, **kwargs):
        seen.append(sorted(tables))
        return payloads["with_top_level" if len(seen) == 1 else "only_tables"]

    monkeypatch.setattr(db_space, "reclaim_space", fake_reclaim)

    first = _post_reclaim(monkeypatch, session_factory).json()
    second = _post_reclaim(monkeypatch, session_factory).json()

    assert first["success"] is False and not first.get("skipped"), first
    assert first["message"].startswith("空间回收没有完成"), first
    assert "服务替页面算好的那一句" in first["message"], first
    assert "fund_history：" not in first["message"], \
        "顶层已经有了这句话，路由不许绕过共用尺子再按表拼一遍"

    assert second["success"] is False and not second.get("skipped"), second
    assert "数据库没给出原因" not in second["message"], second
    assert "被别的会话占着" in second["message"] and "超时被池子掐了" in second["message"], second


def test_a_reclaim_that_freed_bytes_says_the_number(tmp_path, monkeypatch):
    """真还了空间 ⇒ 页面要拿到"释放 N"那一句，而不是含糊的"跑完了"。"""
    session_factory = _database(tmp_path)
    session = session_factory()
    base = date(2026, 1, 1)
    session.bulk_save_objects(
        [
            FundHistory(
                fund_code=f"G{i % 20:03d}",
                fund_name="释放量测试基金",
                nav_date=base + timedelta(days=i // 20),
                nav=1.0 + i / 10000,
            )
            for i in range(4000)
        ]
    )
    session.commit()
    # 删掉大半，腾出可回收的空间（这一步不动回收站逻辑，只是造"死页"）
    session.query(FundHistory).filter(
        FundHistory.nav_date < base + timedelta(days=100)
    ).delete(synchronize_session=False)
    session.commit()
    session.close()

    body = _post_reclaim(monkeypatch, session_factory).json()
    assert body["success"] is True, body
    assert "释放" in body["message"], body
    assert body["data"]["bytes_freed"] > 0

