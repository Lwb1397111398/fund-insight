from datetime import date, datetime, timedelta

from src.models.database import (
    AdviceReasoning,
    AnalysisLog,
    BatchAnalysisTask,
    Blogger,
    CleanupItemLog,
    CleanupLog,
    CrawlerArticleRecord,
    FundHistory,
    FundHolding,
    FundInfo,
    FundSyncRetry,
    InvestmentAdvice,
    Post,
    Prediction,
    PredictionChangeLog,
    PredictionGroup,
    SectorFundMapping,
    UserFundBinding,
    VerificationTask,
    Viewpoint,
)


TODAY = date(2026, 7, 27)


def _prediction(
    db,
    *,
    blogger,
    post,
    fund_code="000001",
    prediction_date=date(2026, 1, 10),
    target_date=date(2026, 12, 31),
    status="pending",
    verified_at=None,
    verify_score=0,
    is_correct=None,
):
    row = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund_code,
        fund_name="测试基金",
        sector="测试板块",
        prediction_type="bullish",
        prediction_content="长期看涨",
        prediction_date=prediction_date,
        prediction_period="6个月",
        target_date=target_date,
        status=status,
        verified_at=verified_at,
        verify_count=1 if status != "pending" else 0,
        verify_score=verify_score,
        is_correct=is_correct,
        is_deleted=False,
    )
    db.add(row)
    db.flush()
    return row


def _blogger_and_post(db, *, post_date=date(2026, 1, 10)):
    blogger = Blogger(name="清理测试博主", platform="test")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="长期预测帖子",
        content="这是一条长期基金预测。",
        post_date=post_date,
        analyzed=True,
    )
    db.add(post)
    db.flush()
    return blogger, post


def test_plan_preserves_long_pending_prediction_and_its_full_history(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger, post = _blogger_and_post(test_db)
    prediction = _prediction(test_db, blogger=blogger, post=post)
    test_db.add(FundInfo(
        fund_code="000001",
        fund_name="测试基金",
        updated_at=datetime(2025, 1, 1),
    ))

    history = []
    current = date(2026, 1, 5)
    while current <= TODAY:
        row = FundHistory(
            fund_code="000001",
            fund_name="测试基金",
            nav_date=current,
            nav=1.0,
        )
        test_db.add(row)
        history.append(row)
        current += timedelta(days=7)
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()

    assert prediction.id not in plan.candidate_ids["predictions"]
    assert post.id not in plan.candidate_ids["posts"]
    assert plan.protected_counts["pending_predictions"] == 1
    assert plan.protected_counts["long_term_predictions"] == 1
    protected_window_ids = {
        row.id for row in history if row.nav_date >= prediction.prediction_date
    }
    assert protected_window_ids.isdisjoint(plan.candidate_ids["fund_history"])


def test_plan_keeps_one_weekly_history_record_between_30_and_90_days(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    test_db.add(FundInfo(
        fund_code="000002",
        fund_name="无预测基金",
        updated_at=datetime(2026, 7, 20),
    ))
    dates = [
        date(2026, 6, 10),
        date(2026, 6, 11),
        date(2026, 6, 17),
        date(2026, 6, 18),
    ]
    rows = []
    for nav_date in dates:
        row = FundHistory(
            fund_code="000002",
            fund_name="无预测基金",
            nav_date=nav_date,
            nav=1.0,
        )
        test_db.add(row)
        rows.append(row)
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()
    candidate_ids = set(plan.candidate_ids["fund_history"])

    assert rows[0].id in candidate_ids
    assert rows[1].id not in candidate_ids
    assert rows[2].id in candidate_ids
    assert rows[3].id not in candidate_ids


def test_plan_preserves_terminal_member_of_mixed_active_prediction_group(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger, post = _blogger_and_post(test_db)
    old_terminal = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="success",
        verified_at=datetime(2026, 2, 2),
        verify_score=90,
        is_correct=True,
    )
    active = _prediction(test_db, blogger=blogger, post=post)
    test_db.add(PredictionGroup(
        blogger_id=blogger.id,
        fund_code="000001",
        prediction_ids=[old_terminal.id, active.id],
        representative_id=old_terminal.id,
        prediction_count=2,
        is_active=True,
    ))
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()

    assert old_terminal.id not in plan.candidate_ids["predictions"]
    assert plan.protected_counts["mixed_group_predictions"] == 1


def test_plan_resolves_sector_funds_for_pending_prediction_history(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger, post = _blogger_and_post(test_db)
    prediction = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        fund_code=None,
    )
    prediction.fund_name = None
    prediction.sector = "人工智能"
    fund = FundInfo(
        fund_code="AI001",
        fund_name="人工智能主题基金",
        sector_type="人工智能",
        updated_at=datetime(2025, 1, 1),
    )
    test_db.add(fund)
    test_db.add(SectorFundMapping(
        sector_name="人工智能",
        fund_code="AI001",
        fund_name="人工智能主题基金",
        reviewed=True,
    ))
    rows = [
        FundHistory(fund_code="AI001", nav_date=date(2026, 1, 9), nav=1.0),
        FundHistory(fund_code="AI001", nav_date=date(2026, 3, 1), nav=1.1),
    ]
    test_db.add_all(rows)
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()

    assert {row.id for row in rows}.isdisjoint(plan.candidate_ids["fund_history"])
    assert fund.id not in plan.candidate_ids["funds"]


def test_plan_protects_all_business_fund_dependencies(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    funds = []
    for code in ["CORE", "LOCK", "VIEW", "BIND", "HOLD", "RETRY", "ORPHAN"]:
        fund = FundInfo(
            fund_code=code,
            fund_name=code,
            updated_at=datetime(2025, 1, 1),
            can_delete=True,
        )
        test_db.add(fund)
        funds.append(fund)
    test_db.flush()
    funds[0].is_core_fund = True
    funds[1].can_delete = False
    test_db.add(Viewpoint(
        fund_code="VIEW",
        content="仍然有效的观点",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 1),
        is_deleted=False,
    ))
    test_db.add(UserFundBinding(sector="绑定板块", fund_code="BIND"))
    test_db.add(FundHolding(
        fund_code="HOLD",
        stock_code="600000",
        stock_name="测试股票",
        report_date=date(2026, 6, 30),
    ))
    test_db.add(FundSyncRetry(
        fund_code="RETRY",
        retry_type="history",
        status="pending",
    ))
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()
    fund_candidates = set(plan.candidate_ids["funds"])

    assert funds[6].id in fund_candidates
    assert {fund.id for fund in funds[:6]}.isdisjoint(fund_candidates)


def test_plan_uses_effective_expiry_for_viewpoints_and_selects_old_advice(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    expired = Viewpoint(
        content="已失效观点",
        viewpoint_date=date(2026, 1, 1),
        valid_until=date(2026, 2, 1),
        is_deleted=False,
        is_summary=True,
    )
    still_valid = Viewpoint(
        content="长期有效观点",
        viewpoint_date=date(2026, 1, 1),
        valid_until=date(2026, 12, 1),
        is_deleted=False,
    )
    old_advice = InvestmentAdvice(
        advice_date=date(2026, 1, 1),
        advice_type="hold",
        advice_content="旧建议",
    )
    test_db.add_all([expired, still_valid, old_advice])
    test_db.flush()
    reasoning = AdviceReasoning(advice_id=old_advice.id, market_state="old")
    record = CrawlerArticleRecord(
        article_id="cleanup-old-viewpoint",
        source="sina_blog",
        is_adopted=True,
        viewpoint_id=expired.id,
        fetched_at=datetime(2026, 1, 1),
    )
    test_db.add_all([reasoning, record])
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()

    assert expired.id in plan.candidate_ids["viewpoints"]
    assert still_valid.id not in plan.candidate_ids["viewpoints"]
    assert old_advice.id in plan.candidate_ids["advice"]
    assert record.id in plan.candidate_ids["crawler_records"]


def test_execute_removes_dependencies_and_keeps_archived_blogger_score(test_db, monkeypatch):
    from src.services import retention_cleanup_service as rcs

    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger, post = _blogger_and_post(test_db, post_date=date(2026, 1, 1))
    blogger.total_predictions = 1
    blogger.correct_predictions = 1
    blogger.total_verify_score = 80
    blogger.accuracy_rate = 80
    prediction = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="success",
        verified_at=datetime(2026, 2, 2),
        verify_score=80,
        is_correct=True,
    )
    test_db.add_all([
        VerificationTask(prediction_id=prediction.id, task_date=date(2026, 2, 1)),
        PredictionChangeLog(
            prediction_id=prediction.id,
            action="verify",
            source="test",
            changed_fields=["status"],
            before_state={"status": "pending"},
            after_state={"status": "success"},
        ),
        PredictionGroup(
            blogger_id=blogger.id,
            fund_code="000001",
            prediction_ids=[prediction.id],
            representative_id=prediction.id,
            prediction_count=1,
        ),
    ])
    task = BatchAnalysisTask(task_type="posts", status="completed")
    test_db.add(task)
    test_db.flush()
    test_db.add(AnalysisLog(task_id=task.id, post_id=post.id, parse_success=True))
    test_db.commit()

    service = RetentionCleanupService(test_db, today=TODAY)
    plan = service.build_plan()
    result = service.execute(
        expected_fingerprint=plan.fingerprint,
        backup_before_cleanup=False,
    )
    test_db.expire_all()

    saved_blogger = test_db.get(Blogger, blogger.id)
    assert result["success"] is True
    assert test_db.get(Prediction, prediction.id) is None
    assert test_db.get(Post, post.id) is None
    assert test_db.query(VerificationTask).count() == 0
    assert test_db.query(PredictionChangeLog).count() == 0
    assert test_db.query(PredictionGroup).count() == 0
    assert test_db.query(AnalysisLog).count() == 0
    assert saved_blogger.archived_verified_count == 1
    assert saved_blogger.archived_correct_count == 1
    assert saved_blogger.archived_verify_score == 80
    assert saved_blogger.total_predictions == 1
    assert saved_blogger.accuracy_rate == 80
    guard = result["blogger_accuracy_guard"]
    assert guard["bloggers_touched"] == 1
    assert guard["stable"] is True
    assert abs(guard["max_abs_delta"]) < 0.01
    assert test_db.query(CleanupLog).count() == 1
    assert test_db.query(CleanupItemLog).filter(
        CleanupItemLog.data_type == "prediction"
    ).count() == 1


def test_plan_exposes_long_term_fund_window_protection_counts(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger, post = _blogger_and_post(test_db)
    prediction = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        prediction_date=date(2026, 1, 10),
        target_date=date(2026, 7, 10),
        status="pending",
    )
    test_db.add(FundInfo(
        fund_code="000001",
        fund_name="测试基金",
        updated_at=datetime(2026, 7, 1),
    ))
    history = []
    current = date(2026, 1, 5)
    while current <= TODAY:
        row = FundHistory(
            fund_code="000001",
            fund_name="测试基金",
            nav_date=current,
            nav=1.0,
        )
        test_db.add(row)
        history.append(row)
        current += timedelta(days=7)
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()
    assert plan.protected_counts["long_term_fund_windows"] >= 1
    assert plan.protected_counts["long_term_fund_history"] >= 1
    assert prediction.id not in plan.candidate_ids["predictions"]
    window_ids = {
        row.id for row in history
        if prediction.prediction_date <= row.nav_date <= prediction.target_date
    }
    assert window_ids.isdisjoint(plan.candidate_ids["fund_history"])


def test_execute_rejects_stale_preview_fingerprint(test_db, monkeypatch):
    from src.services import retention_cleanup_service as rcs
    from src.services.retention_cleanup_service import (
        CleanupPlanChanged,
        RetentionCleanupService,
    )

    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)

    blogger, post = _blogger_and_post(test_db)
    _prediction(
        test_db,
        blogger=blogger,
        post=post,
        target_date=date(2026, 2, 1),
        status="success",
        verified_at=datetime(2026, 2, 2),
    )
    test_db.commit()
    service = RetentionCleanupService(test_db, today=TODAY)

    try:
        service.execute(
            expected_fingerprint="stale-preview",
            backup_before_cleanup=False,
        )
    except CleanupPlanChanged as exc:
        assert exc.current_fingerprint == service.build_plan().fingerprint
    else:
        raise AssertionError("旧预览指纹必须阻止清理")


def test_plan_keeps_retry_posts_and_viewpoints_in_restore_window(test_db):
    from src.services.retention_cleanup_service import RetentionCleanupService

    blogger = Blogger(name="恢复保护测试", platform="test")
    test_db.add(blogger)
    test_db.flush()
    pending_post = Post(
        blogger_id=blogger.id,
        content="尚未分析",
        post_date=date(2026, 1, 1),
        analyzed=False,
    )
    failed_post = Post(
        blogger_id=blogger.id,
        content="等待重试",
        post_date=date(2026, 1, 1),
        analyzed=True,
    )
    removable_post = Post(
        blogger_id=blogger.id,
        content="已完成且无预测",
        post_date=date(2026, 1, 1),
        analyzed=True,
    )
    test_db.add_all([pending_post, failed_post, removable_post])
    test_db.flush()
    test_db.add(AnalysisLog(post_id=failed_post.id, parse_success=False))
    viewpoint = Viewpoint(
        content="恢复期观点",
        viewpoint_date=date(2026, 1, 1),
        is_deleted=True,
        restore_before=TODAY + timedelta(days=1),
    )
    test_db.add(viewpoint)
    test_db.commit()

    plan = RetentionCleanupService(test_db, today=TODAY).build_plan()

    assert pending_post.id not in plan.candidate_ids["posts"]
    assert failed_post.id not in plan.candidate_ids["posts"]
    assert removable_post.id in plan.candidate_ids["posts"]
    assert viewpoint.id not in plan.candidate_ids["viewpoints"]


def test_archiving_only_counts_predictions_used_by_blogger_stats(test_db, monkeypatch):
    from src.services import retention_cleanup_service as rcs
    from src.services.retention_cleanup_service import RetentionCleanupService

    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)

    blogger, post = _blogger_and_post(test_db, post_date=date(2026, 1, 1))
    blogger.total_predictions = 1
    blogger.total_verify_score = 70
    verified = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        target_date=date(2026, 2, 1),
        status="success",
        verified_at=datetime(2026, 2, 2),
        verify_score=70,
        is_correct=True,
    )
    flat = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        target_date=date(2026, 2, 1),
        status="success",
        verified_at=datetime(2026, 2, 2),
        verify_score=100,
        is_correct=True,
    )
    flat.prediction_type = "flat"
    unverified = _prediction(
        test_db,
        blogger=blogger,
        post=post,
        target_date=date(2026, 2, 1),
        status="failed",
        verified_at=datetime(2026, 2, 2),
    )
    unverified.verify_count = 0
    test_db.commit()

    service = RetentionCleanupService(test_db, today=TODAY)
    plan = service.build_plan()
    service.execute(expected_fingerprint=plan.fingerprint, backup_before_cleanup=False)
    test_db.expire_all()

    saved = test_db.get(Blogger, blogger.id)
    assert verified.id is not None
    assert saved.archived_verified_count == 1
    assert saved.archived_verify_score == 70
    assert saved.total_predictions == 1
    assert saved.accuracy_rate == 70


def test_category_limited_cleanup_does_not_delete_other_candidates(test_db, monkeypatch):
    from src.services import retention_cleanup_service as rcs
    from src.services.retention_cleanup_service import RetentionCleanupService

    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)

    fund = FundInfo(
        fund_code="ORPHAN-LIMITED",
        fund_name="仅清理基金",
        updated_at=datetime(2025, 1, 1),
    )
    viewpoint = Viewpoint(
        content="另一个分类的旧观点",
        viewpoint_date=date(2026, 1, 1),
        is_deleted=False,
    )
    test_db.add_all([fund, viewpoint])
    test_db.commit()

    service = RetentionCleanupService(test_db, today=TODAY)
    plan = service.build_plan()
    service.execute(
        expected_fingerprint=plan.fingerprint,
        backup_before_cleanup=False,
        categories={"funds"},
    )

    assert test_db.get(FundInfo, fund.id) is None
    assert test_db.get(Viewpoint, viewpoint.id) is not None
