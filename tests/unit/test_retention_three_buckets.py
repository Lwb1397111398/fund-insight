"""三桶 v2：verified 护栏、观点锚点、全局上限、dry-run 默认、净值两桶。"""
from datetime import date, datetime, timedelta

import pytest

from src.models.database import (
    Blogger,
    CleanupItemLog,
    CleanupLog,
    FundHistory,
    FundInfo,
    FundSyncRetry,
    Post,
    Prediction,
    SectorFundMapping,
    Viewpoint,
)
from src.services.retention_three_buckets import (
    CONFIRM_TOKEN,
    CleanupInterrupted,
    ThreeBucketPolicy,
    ThreeBucketRetentionService,
)


def _seed_blogger_post(db):
    b = Blogger(name="三桶测试博主", platform="test")
    db.add(b)
    db.flush()
    p = Post(
        blogger_id=b.id,
        content="three-bucket",
        post_date=date(2026, 1, 1),
        analyzed=True,
    )
    db.add(p)
    db.flush()
    return b, p


def test_verified_ledger_never_in_deleted_bucket(test_db):
    blogger, post = _seed_blogger_post(test_db)
    bare = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB001",
        prediction_type="bullish",
        prediction_content="no conclusion",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=None,
    )
    verified = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB002",
        prediction_type="bullish",
        prediction_content="has conclusion",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=False,
        verify_count=1,
    )
    test_db.add_all([bare, verified])
    test_db.commit()

    plan = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29)).build_plan()
    assert bare.id in plan.candidate_ids["deleted_predictions"]
    assert verified.id not in plan.candidate_ids["deleted_predictions"]
    assert plan.protected_counts["verified_ledger_excluded"] >= 1


def test_execute_blocks_if_verified_slips_into_plan(test_db):
    blogger, post = _seed_blogger_post(test_db)
    verified = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB003",
        prediction_type="bullish",
        prediction_content="ledger",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=True,
        verify_count=1,
    )
    test_db.add(verified)
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    plan = svc.build_plan()
    # 人为污染 plan
    plan.candidate_ids["deleted_predictions"] = [verified.id]
    try:
        svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN, plan=plan)
        assert False, "should block"
    except RuntimeError as exc:
        assert "verified ledger" in str(exc)


def test_global_cap_truncates_across_buckets(test_db):
    blogger, post = _seed_blogger_post(test_db)
    for i in range(5):
        test_db.add(
            Prediction(
                post_id=post.id,
                blogger_id=blogger.id,
                fund_code=f"CAP{i}",
                prediction_type="bullish",
                prediction_content="x",
                prediction_date=date(2026, 1, 1),
                target_date=date(2026, 2, 1),
                status="pending",
                is_deleted=True,
                deleted_at=datetime(2026, 1, 10),
                is_correct=None,
            )
        )
    test_db.commit()
    policy = ThreeBucketPolicy(max_total_per_run=2, max_per_bucket=10)
    plan = ThreeBucketRetentionService(
        test_db, today=date(2026, 7, 29), policy=policy
    ).build_plan()
    assert plan.total == 2
    assert plan.truncated is True


def test_soft_deleted_viewpoint_uses_deleted_at_not_valid_until(test_db):
    blogger, post = _seed_blogger_post(test_db)
    # valid_until 很远，但 deleted_at 已满 30 天 → 应进候选
    vp = Viewpoint(
        blogger_id=blogger.id,
        post_id=post.id,
        content="soft deleted vp",
        author="t",
        source="test",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 31),
        is_deleted=True,
        deleted_at=datetime(2026, 6, 1),
        is_summary=False,
    )
    test_db.add(vp)
    test_db.commit()
    plan = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29)).build_plan()
    assert vp.id in plan.candidate_ids["deleted_viewpoints"]


def test_summary_uses_viewpoint_date_window(test_db):
    blogger, post = _seed_blogger_post(test_db)
    old = Viewpoint(
        blogger_id=blogger.id,
        content="old summary",
        author="系统",
        source="daily_summary",
        viewpoint_date=date(2026, 3, 1),
        valid_until=date(2026, 12, 31),  # 故意拉长
        is_summary=True,
        is_deleted=False,
    )
    recent = Viewpoint(
        blogger_id=blogger.id,
        content="recent summary",
        author="系统",
        source="daily_summary",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 31),
        is_summary=True,
        is_deleted=False,
    )
    test_db.add_all([old, recent])
    test_db.commit()
    plan = ThreeBucketRetentionService(
        test_db,
        today=date(2026, 7, 29),
        policy=ThreeBucketPolicy(summary_viewpoint_days=90),
    ).build_plan()
    assert old.id in plan.candidate_ids["summary_viewpoints"]
    assert recent.id not in plan.candidate_ids["summary_viewpoints"]


def test_dry_run_default_and_token(test_db):
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    r = svc.execute(dry_run=True)
    assert r["mode"] == "dry-run"
    try:
        svc.execute(dry_run=False, confirm_token="nope")
        assert False
    except PermissionError:
        pass


def test_execute_deletes_unverified_soft_delete_only(test_db):
    blogger, post = _seed_blogger_post(test_db)
    bare = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB020",
        prediction_type="bullish",
        prediction_content="old deleted",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=None,
    )
    test_db.add(bare)
    test_db.commit()
    bare_id = bare.id
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    result = svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN)
    assert result["deleted_counts"]["deleted_predictions"] >= 1
    assert test_db.query(Prediction).filter(Prediction.id == bare_id).first() is None


TODAY = date(2026, 7, 29)


def _add_history(db, fund_code: str, dates, *, fund_name: str = "净值测试基金"):
    rows = []
    for nav_date in dates:
        row = FundHistory(
            fund_code=fund_code,
            fund_name=fund_name,
            nav_date=nav_date,
            nav=1.0,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _add_fund(db, fund_code: str, *, updated_days_ago: int = 200, **kwargs):
    fund = FundInfo(
        fund_code=fund_code,
        fund_name=kwargs.pop("fund_name", f"基金{fund_code}"),
        updated_at=datetime.combine(TODAY, datetime.min.time())
        - timedelta(days=updated_days_ago),
        **kwargs,
    )
    db.add(fund)
    db.flush()
    return fund


def test_orphan_fund_bucket_takes_fund_and_its_history(test_db):
    """没有任何引用的基金整只删除，净值与同步重试记录跟着走。"""
    orphan = _add_fund(test_db, "ORPH1")
    _add_history(test_db, "ORPH1", [TODAY - timedelta(days=i) for i in range(1, 8)])
    test_db.add(FundSyncRetry(fund_code="ORPH1", status="success"))
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=TODAY)
    plan = svc.build_plan()
    assert orphan.id in plan.candidate_ids["orphan_funds"]
    # 净值不进 stale 桶（由 orphan 桶连带删除），但预览要报出连带行数
    assert svc.estimate_cascade_rows(plan)["fund_history"] == 7

    result = svc.execute(
        dry_run=False, confirm_token=CONFIRM_TOKEN, plan=plan, reclaim_space=False
    )
    assert result["deleted_counts"]["orphan_funds"] == 1
    assert result["cascade_counts"]["fund_history"] == 7
    assert result["total_rows_removed"] >= 8
    assert test_db.query(FundInfo).filter_by(fund_code="ORPH1").first() is None
    assert test_db.query(FundHistory).filter_by(fund_code="ORPH1").count() == 0
    assert test_db.query(FundSyncRetry).filter_by(fund_code="ORPH1").count() == 0


def test_fund_referenced_by_prediction_is_never_orphaned(test_db):
    blogger, post = _seed_blogger_post(test_db)
    kept = _add_fund(test_db, "KEEP1")
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="KEEP1",
            prediction_type="up",
            prediction_date=TODAY - timedelta(days=10),
            target_date=TODAY + timedelta(days=10),
            status="pending",
            is_deleted=False,
        )
    )
    test_db.commit()

    plan = ThreeBucketRetentionService(test_db, today=TODAY).build_plan()
    assert kept.id not in plan.candidate_ids["orphan_funds"]
    assert plan.protected_counts["referenced_funds_kept"] >= 1


def test_fund_referenced_only_by_sector_mapping_is_protected(test_db):
    """预测只写了板块没写代码时，映射到的基金也不能删。"""
    mapped = _add_fund(test_db, "SECT1")
    test_db.add(
        SectorFundMapping(sector_name="人工智能", fund_code="SECT1", is_active=True)
    )
    test_db.commit()

    plan = ThreeBucketRetentionService(test_db, today=TODAY).build_plan()
    assert mapped.id not in plan.candidate_ids["orphan_funds"]


def test_stale_history_keeps_prediction_window_and_recent_floor(test_db):
    """仍被引用的基金：只删早于「最早未结预测起点」且超出保底条数的净值。"""
    blogger, post = _seed_blogger_post(test_db)
    _add_fund(test_db, "HIST1", updated_days_ago=1)
    # 一年净值，每 3 天一条
    dates = [TODAY - timedelta(days=3 * i) for i in range(0, 120)]
    _add_history(test_db, "HIST1", dates)
    prediction_start = TODAY - timedelta(days=60)
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="HIST1",
            prediction_type="up",
            prediction_date=prediction_start,
            target_date=TODAY + timedelta(days=10),
            status="pending",
            is_deleted=False,
        )
    )
    test_db.commit()

    policy = ThreeBucketPolicy(
        fund_history_keep_recent=5, fund_history_grace_days=15
    )
    svc = ThreeBucketRetentionService(test_db, today=TODAY, policy=policy)
    plan = svc.build_plan()
    doomed = set(plan.candidate_ids["stale_fund_history"])
    assert doomed, "应有可删净值"

    floor = prediction_start - timedelta(days=15)
    survivors = (
        test_db.query(FundHistory)
        .filter(FundHistory.fund_code == "HIST1", ~FundHistory.id.in_(doomed))
        .all()
    )
    # 预测窗口内一条不少
    in_window = [
        r for r in survivors if prediction_start <= r.nav_date <= TODAY
    ]
    assert len(in_window) == len(
        [d for d in dates if prediction_start <= d <= TODAY]
    )
    # 起点前值（anchor）保留，供验证算起点净值
    assert any(r.nav_date <= floor for r in survivors)
    # 被删的都早于保护下界
    deleted_rows = (
        test_db.query(FundHistory).filter(FundHistory.id.in_(doomed)).all()
    )
    assert all(r.nav_date < floor for r in deleted_rows)


def test_stale_history_respects_keep_recent_floor_without_predictions(test_db):
    """基金被映射保护但没有预测时，仍保底留最近 N 条。"""
    _add_fund(test_db, "HIST2", updated_days_ago=1)
    test_db.add(
        SectorFundMapping(sector_name="医药", fund_code="HIST2", is_active=True)
    )
    dates = [TODAY - timedelta(days=i) for i in range(0, 30)]
    _add_history(test_db, "HIST2", dates)
    test_db.commit()

    policy = ThreeBucketPolicy(fund_history_keep_recent=10)
    svc = ThreeBucketRetentionService(test_db, today=TODAY, policy=policy)
    plan = svc.build_plan()
    doomed = plan.candidate_ids["stale_fund_history"]
    assert len(doomed) == 20
    survivors = (
        test_db.query(FundHistory)
        .filter(FundHistory.fund_code == "HIST2", ~FundHistory.id.in_(doomed))
        .order_by(FundHistory.nav_date.desc())
        .all()
    )
    assert len(survivors) == 10
    assert survivors[0].nav_date == TODAY


def test_recently_verified_prediction_window_is_protected(test_db):
    """已验证但目标日在回溯窗口内，其净值窗口仍保护，便于核对。"""
    blogger, post = _seed_blogger_post(test_db)
    _add_fund(test_db, "HIST3", updated_days_ago=1)
    dates = [TODAY - timedelta(days=i) for i in range(0, 200)]
    _add_history(test_db, "HIST3", dates)
    start = TODAY - timedelta(days=100)
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="HIST3",
            prediction_type="up",
            prediction_date=start,
            target_date=TODAY - timedelta(days=30),
            status="success",
            is_correct=True,
            verify_count=1,
            is_deleted=False,
        )
    )
    test_db.commit()

    policy = ThreeBucketPolicy(
        fund_history_keep_recent=5,
        fund_history_grace_days=15,
        verified_lookback_days=90,
    )
    plan = ThreeBucketRetentionService(
        test_db, today=TODAY, policy=policy
    ).build_plan()
    doomed = set(plan.candidate_ids["stale_fund_history"])
    survivors = (
        test_db.query(FundHistory)
        .filter(FundHistory.fund_code == "HIST3", ~FundHistory.id.in_(doomed))
        .all()
    )
    assert all(
        any(r.nav_date == d for r in survivors)
        for d in dates
        if start <= d <= TODAY
    )


def test_verified_prediction_outside_lookback_stops_protecting_history(test_db):
    """结论已久的预测不再压着净值，否则永远清不掉。"""
    blogger, post = _seed_blogger_post(test_db)
    _add_fund(test_db, "HIST4", updated_days_ago=1)
    dates = [TODAY - timedelta(days=i) for i in range(0, 200)]
    _add_history(test_db, "HIST4", dates)
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="HIST4",
            prediction_type="up",
            prediction_date=TODAY - timedelta(days=190),
            target_date=TODAY - timedelta(days=180),
            status="success",
            is_correct=True,
            verify_count=1,
            is_deleted=False,
        )
    )
    test_db.commit()

    policy = ThreeBucketPolicy(
        fund_history_keep_recent=10, verified_lookback_days=90
    )
    plan = ThreeBucketRetentionService(
        test_db, today=TODAY, policy=policy
    ).build_plan()
    assert len(plan.candidate_ids["stale_fund_history"]) == 190


def test_history_bucket_has_its_own_cap(test_db):
    """净值行不跟业务桶挤 max_total_per_run，否则上万行永远清不完。"""
    _add_fund(test_db, "HIST5", updated_days_ago=1)
    test_db.add(
        SectorFundMapping(sector_name="消费", fund_code="HIST5", is_active=True)
    )
    _add_history(test_db, "HIST5", [TODAY - timedelta(days=i) for i in range(0, 60)])
    test_db.commit()

    policy = ThreeBucketPolicy(
        fund_history_keep_recent=5,
        max_total_per_run=1,
        max_fund_history_per_run=40,
    )
    plan = ThreeBucketRetentionService(
        test_db, today=TODAY, policy=policy
    ).build_plan()
    # 业务桶额度只有 1，但净值桶用自己的 40
    assert len(plan.candidate_ids["stale_fund_history"]) == 40
    assert plan.truncated is True


def test_orphan_fund_execute_rechecks_active_predictions(test_db):
    """预览到执行之间新增了活跃预测，执行时必须放过这只基金。"""
    blogger, post = _seed_blogger_post(test_db)
    orphan = _add_fund(test_db, "RACE1")
    _add_history(test_db, "RACE1", [TODAY - timedelta(days=i) for i in range(0, 5)])
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=TODAY)
    plan = svc.build_plan()
    assert orphan.id in plan.candidate_ids["orphan_funds"]

    # 用户在确认对话框停留期间新增了预测
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="RACE1",
            prediction_type="up",
            prediction_date=TODAY,
            target_date=TODAY + timedelta(days=7),
            status="pending",
            is_deleted=False,
        )
    )
    test_db.commit()

    result = svc.execute(
        dry_run=False, confirm_token=CONFIRM_TOKEN, plan=plan, reclaim_space=False
    )
    assert result["deleted_counts"]["orphan_funds"] == 0
    assert test_db.query(FundInfo).filter_by(fund_code="RACE1").first() is not None
    assert test_db.query(FundHistory).filter_by(fund_code="RACE1").count() == 5


def test_plan_is_idempotent_after_execute(test_db):
    """执行一轮后再 build_plan 应收敛到 0，避免反复删同一批。"""
    _add_fund(test_db, "IDEM1")
    _add_history(test_db, "IDEM1", [TODAY - timedelta(days=i) for i in range(0, 5)])
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=TODAY)
    svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN, reclaim_space=False)
    assert svc.build_plan().total == 0


def test_a_reclaim_on_an_in_memory_database_never_claims_success(test_db):
    """内存库 VACUUM 不了：接口要给出结构化结果，且不许同时说"跑成功了"。"""
    svc = ThreeBucketRetentionService(test_db, today=TODAY)
    result = svc.reclaim_space(["fund_history"])
    assert result.get("skipped") is True
    assert result["reason"] == "in_memory_database"
    # 一句"没跑"不许买到 success —— 页面据此说"已回收空间"就是假话
    assert not result.get("success")


def test_a_reclaim_that_raised_is_reported_as_not_done(test_db, monkeypatch):
    """回收这一步自己炸了：删除已经成功，但那一步不许被说成做完了。"""
    import src.services.db_space as db_space

    def boom(*args, **kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(db_space, "reclaim_space", boom)
    svc = ThreeBucketRetentionService(test_db, today=TODAY)
    result = svc.reclaim_space(["fund_history"])
    assert result["success"] is False
    assert "磁盘满了" in result["error"]


def _seed_two_rows_for_abort(test_db):
    """两行回收站预测（第一桶，会先 commit）+ 一行观点（第二桶之后，删不到）。"""
    blogger, post = _seed_blogger_post(test_db)
    ids = []
    for code in ("ABORT1", "ABORT2"):
        pred = Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code=code,
            prediction_type="bullish",
            prediction_content="will be gone before the crash",
            prediction_date=date(2026, 1, 1),
            target_date=date(2026, 2, 1),
            status="pending",
            is_deleted=True,
            deleted_at=datetime(2026, 1, 10),
            is_correct=None,
        )
        test_db.add(pred)
        test_db.flush()
        ids.append(pred.id)
    vp = Viewpoint(
        blogger_id=blogger.id,
        post_id=post.id,
        content="still here after the crash",
        author="t",
        source="test",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 31),
        is_deleted=True,
        deleted_at=datetime(2026, 6, 1),
        is_summary=False,
    )
    test_db.add(vp)
    test_db.commit()
    return ids, vp.id


def test_an_abort_midway_reports_the_rows_already_gone(test_db, monkeypatch):
    """崩在半路时，"已经删掉几行"必须同时出现在异常、台账与那句话里。

    每个 _hard_delete_* 都是"一批 commit 之后才回调"，所以第一桶的两行
    在崩溃点之前就已经不可再生地消失了；而台账与页面此前只会说"失败"，
    读起来像一个字都没动。
    """
    gone_ids, vp_id = _seed_two_rows_for_abort(test_db)
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))

    def boom(ids, *, on_progress=None):
        raise RuntimeError("模拟：删清理日志这一批撞上外键")

    monkeypatch.setattr(svc, "_hard_delete_cleanup_logs", boom)

    with pytest.raises(CleanupInterrupted) as caught:
        svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN, reclaim_space=False)

    # ① 已提交的那一批真从库里消失（per-batch commit 的语义，不是"整笔回滚"）
    assert test_db.query(Prediction).filter(Prediction.id.in_(gone_ids)).count() == 0
    # ② 崩溃点之后的那一桶一个字没动
    assert test_db.query(Viewpoint).filter(Viewpoint.id == vp_id).first() is not None

    # ③ 台账：一条 failed，数的是"已提交"的行数，不是计划的行数
    ledgers = test_db.query(CleanupLog).all()
    assert len(ledgers) == 1
    ledger = ledgers[0]
    assert ledger.status == "failed"
    assert ledger.success_count == 2
    assert ledger.success_count < ledger.total_items
    assert ledger.details["deleted_counts"] == {"deleted_predictions": 2}
    assert "外键" in " ".join(ledger.errors or [])
    # 清理日志那一桶崩在半路，连带行数不许提前入账
    assert ledger.details["cascade_counts"] == {}

    # ④ 异常自己带出行数与台账 id：config.py 的 task.error = str(exc) 全靠这句话
    assert "2 行" in str(caught.value)
    assert "外键" in str(caught.value)
    assert caught.value.committed_counts == {"deleted_predictions": 2}
    assert caught.value.cleanup_log_id == ledger.id


def test_a_run_that_finishes_writes_no_failed_ledger(test_db):
    """反面对照：正常跑完只留一条 completed，不许把"失败台账"变成常驻。"""
    _seed_two_rows_for_abort(test_db)
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    result = svc.execute(
        dry_run=False, confirm_token=CONFIRM_TOKEN, reclaim_space=False
    )
    assert result["total_deleted"] == 3  # 两行预测 + 一行观点
    assert [row.status for row in test_db.query(CleanupLog).all()] == ["completed"]


def test_a_failed_ledger_that_cannot_be_written_still_reports_the_counts(test_db, monkeypatch):
    """台账写不进去（比如库本身坏了）时，"删了几行"这件事仍然要说出口。

    这一格的结构是：CleanupLog 这个对象一旦建不出来，except 里那次
    add/commit 就会再抛一次；旧写法让它顺着冒出去，行数一个字都不剩。
    """
    gone_ids, _ = _seed_two_rows_for_abort(test_db)
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))

    def boom(ids, *, on_progress=None):
        raise RuntimeError("模拟：删到一半数据库连接断了")

    monkeypatch.setattr(svc, "_hard_delete_cleanup_logs", boom)
    real_commit = svc.db.commit
    crashed = {"yes": False}

    def commit_router():
        # 崩溃之前的 commit 照常（那批行必须真消失）；崩了之后补写台账那一次才坏
        if not crashed["yes"]:
            return real_commit()
        raise RuntimeError("模拟：台账写不进去")

    def boom_and_mark(ids, *, on_progress=None):
        crashed["yes"] = True
        return boom(ids, on_progress=on_progress)

    monkeypatch.setattr(svc, "_hard_delete_cleanup_logs", boom_and_mark)
    monkeypatch.setattr(svc.db, "commit", commit_router)

    with pytest.raises(CleanupInterrupted) as caught:
        svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN, reclaim_space=False)

    assert caught.value.cleanup_log_id is None
    assert caught.value.committed_counts == {"deleted_predictions": 2}
    assert "2 行" in str(caught.value)
    assert test_db.query(Prediction).filter(Prediction.id.in_(gone_ids)).count() == 0
