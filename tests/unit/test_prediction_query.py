from datetime import date, timedelta

from src.models.database import Blogger, Post, Prediction


def _seed_context(db):
    blogger = Blogger(name="价值观察者", platform="eastmoney")
    other_blogger = Blogger(name="趋势研究员", platform="eastmoney")
    db.add_all([blogger, other_blogger])
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="人工智能与医药观察",
        content="测试内容",
        post_date=date(2026, 7, 1),
    )
    other_post = Post(
        blogger_id=other_blogger.id,
        title="消费板块",
        content="测试内容",
        post_date=date(2026, 7, 2),
    )
    db.add_all([post, other_post])
    db.flush()
    return blogger, other_blogger, post, other_post


def test_prediction_query_pages_beyond_one_thousand_rows(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    blogger, _, post, _ = _seed_context(test_db)
    base_date = date(2026, 7, 1)
    test_db.add_all([
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code=f"F{i:04d}",
            fund_name=f"测试基金{i}",
            prediction_type="up",
            prediction_date=base_date + timedelta(days=i % 30),
            target_date=base_date + timedelta(days=31),
            status="pending",
            is_deleted=False,
        )
        for i in range(1005)
    ])
    test_db.commit()

    first = PredictionQueryService(test_db).search(page=1, page_size=200)
    last = PredictionQueryService(test_db).search(page=6, page_size=200)

    assert len(first["data"]) == 200
    assert len(last["data"]) == 5
    assert first["meta"]["total"] == 1005
    assert first["meta"]["has_more"] is True
    assert last["meta"]["has_more"] is False


def test_prediction_query_combines_search_and_filters(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    blogger, other_blogger, post, other_post = _seed_context(test_db)
    predictions = [
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="AI001",
            fund_name="人工智能指数",
            sector="人工智能",
            prediction_type="up",
            prediction_content="看好算力",
            prediction_date=date(2026, 7, 1),
            target_date=date(2026, 7, 8),
            status="success",
            is_correct=True,
            is_deleted=False,
        ),
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="MED01",
            fund_name="医药基金",
            sector="医药",
            prediction_type="down",
            prediction_content="医药承压",
            prediction_date=date(2026, 7, 2),
            target_date=date(2026, 7, 9),
            status="failed",
            is_correct=False,
            is_deleted=False,
        ),
        Prediction(
            post_id=other_post.id,
            blogger_id=other_blogger.id,
            fund_code="AI002",
            fund_name="人工智能增强",
            sector="人工智能",
            prediction_type="up",
            prediction_content="短期上涨",
            prediction_date=date(2026, 7, 3),
            target_date=date(2026, 7, 10),
            status="pending",
            is_deleted=False,
        ),
    ]
    test_db.add_all(predictions)
    test_db.commit()

    result = PredictionQueryService(test_db).search(
        search="人工智能",
        blogger_id=blogger.id,
        fund_code="AI001",
        sector="人工智能",
        prediction_type="up",
        result="correct",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
    )

    assert [item["id"] for item in result["data"]] == [predictions[0].id]
    assert result["meta"]["total"] == 1


def test_prediction_query_keeps_flat_predictions_and_reports_facets(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    blogger, _, post, _ = _seed_context(test_db)
    test_db.add_all([
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="flat",
            prediction_content="维持观望",
            prediction_date=date(2026, 7, 1),
            status="pending",
            is_deleted=False,
        ),
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="up",
            prediction_date=date(2026, 7, 2),
            status="success",
            is_correct=True,
            is_deleted=False,
        ),
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="down",
            prediction_date=date(2026, 7, 3),
            status="failed",
            is_correct=False,
            is_deleted=True,
        ),
    ])
    test_db.commit()

    active = PredictionQueryService(test_db).search()
    archived = PredictionQueryService(test_db).search(archive="archived")

    assert {item["prediction_type"] for item in active["data"]} == {"flat", "up"}
    assert active["meta"]["facets"] == {
        "all": 2,
        "pending": 1,
        "verified": 1,
        "correct": 1,
        "wrong": 0,
        "flat": 1,
        "archived": 1,
        "due": 0,
        "upcoming": 0,
        "unverifiable": 0,
    }
    assert len(archived["data"]) == 1
    assert archived["data"][0]["is_deleted"] is True


def test_prediction_route_returns_data_array_and_pagination_meta(test_db):
    from src.api.routes.predictions import get_predictions

    blogger, _, post, _ = _seed_context(test_db)
    test_db.add(Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="flat",
        prediction_date=date(2026, 7, 1),
        status="pending",
        is_deleted=False,
    ))
    test_db.commit()

    response = get_predictions(page=1, page_size=20, db=test_db)

    assert response["success"] is True
    assert isinstance(response["data"], list)
    assert response["meta"]["page"] == 1
    assert response["meta"]["page_size"] == 20


def test_pending_prediction_detail_includes_source_and_lifecycle(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    blogger, _, post, _ = _seed_context(test_db)
    post.source_url = "https://example.com/post/1"
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="flat",
        prediction_content="暂时观望",
        prediction_date=date(2026, 7, 1),
        target_date=date(2026, 7, 8),
        next_verify_date=date(2026, 7, 8),
        status="pending",
        is_deleted=False,
    )
    test_db.add(prediction)
    test_db.commit()

    detail = PredictionQueryService(test_db).get_detail(prediction.id)

    assert detail["lifecycle_status"] == "pending"
    assert detail["verification_result"] is None
    assert detail["post_title"] == "人工智能与医药观察"
    assert detail["post_source_url"] == "https://example.com/post/1"
    assert detail["next_verify_date"] == "2026-07-08"


def _seed_ordering_predictions(db):
    """构造三类预测：到期待验证（新/旧）、未到期近/远、已验证。"""
    from src.services.prediction_lifecycle import current_as_of, max_end_nav_age_days

    blogger, _, post, _ = _seed_context(db)
    today = current_as_of()
    max_age = max_end_nav_age_days()
    rows = {
        # 到期待验证：目标日已过（含超过历史窗口下限的，仍可验证）
        "due_old": today - timedelta(days=max_age + 40),
        "due_today": today,
        # 未到期
        "upcoming_near": today + timedelta(days=3),
        "upcoming_far": today + timedelta(days=200),
    }
    created = {}
    for key, target in rows.items():
        prediction = Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code=key,
            prediction_type="up",
            prediction_date=target - timedelta(days=7),
            target_date=target,
            status="pending",
            is_deleted=False,
        )
        db.add(prediction)
        created[key] = prediction
    verified = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="verified",
        prediction_type="up",
        prediction_date=today - timedelta(days=20),
        target_date=today - timedelta(days=13),
        status="success",
        is_correct=True,
        is_deleted=False,
    )
    db.add(verified)
    created["verified"] = verified
    db.commit()
    return created


def test_default_sort_puts_due_predictions_first(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    created = _seed_ordering_predictions(test_db)
    result = PredictionQueryService(test_db).search()

    assert result["meta"]["sort"] == "due_first"
    codes = [row["fund_code"] for row in result["data"]]
    # 到期待验证在最前，且内部按目标日升序（最旧的先验）
    assert codes[:2] == ["due_old", "due_today"]
    # 未到期紧随其后，近的在前
    assert codes[2:4] == ["upcoming_near", "upcoming_far"]
    # 已验证排在最后
    assert codes[4:] == ["verified"]
    assert created["due_old"].fund_code == "due_old"


def test_default_sort_marks_lifecycle_and_days_to_target(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    _seed_ordering_predictions(test_db)
    rows = {
        row["fund_code"]: row
        for row in PredictionQueryService(test_db).search()["data"]
    }

    assert rows["due_today"]["lifecycle"] == "due_unverified"
    assert rows["due_today"]["days_to_target"] == 0
    assert rows["due_old"]["lifecycle"] == "due_unverified"  # 旧预测不再归入 unverifiable
    assert rows["upcoming_near"]["lifecycle"] == "active"
    assert rows["upcoming_near"]["days_to_target"] == 3
    assert rows["verified"]["lifecycle"] == "verified_correct"


def test_lifecycle_filter_and_facets_expose_due_queue(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    _seed_ordering_predictions(test_db)
    service = PredictionQueryService(test_db)

    due = service.search(lifecycle="due")
    upcoming = service.search(lifecycle="active")
    unknown = service.search(lifecycle="unverifiable")  # 不再支持，回退无过滤

    assert {row["fund_code"] for row in due["data"]} == {"due_old", "due_today"}
    assert {row["fund_code"] for row in upcoming["data"]} == {"upcoming_near", "upcoming_far"}
    assert unknown["data"]  # 未知 lifecycle 不再过滤，返回全部
    facets = due["meta"]["facets"]
    assert facets["due"] == 2
    assert facets["upcoming"] == 2
    assert facets["unverifiable"] == 0


def test_explicit_sort_options_override_due_first(test_db):
    from src.services.prediction_query_service import PredictionQueryService

    _seed_ordering_predictions(test_db)
    service = PredictionQueryService(test_db)

    target_asc = [row["target_date"] for row in service.search(sort="target_asc")["data"]]
    target_desc = [row["target_date"] for row in service.search(sort="target_desc")["data"]]
    latest = service.search(sort="latest")

    assert target_asc == sorted(target_asc)
    assert target_desc == sorted(target_desc, reverse=True)
    assert latest["meta"]["sort"] == "latest"
    dates = [row["prediction_date"] for row in latest["data"]]
    assert dates == sorted(dates, reverse=True)
    # 未知排序值回落默认
    assert service.search(sort="nonsense")["meta"]["sort"] == "due_first"


def test_route_passes_lifecycle_and_sort_through(test_db):
    from src.api.routes.predictions import get_predictions

    _seed_ordering_predictions(test_db)

    response = get_predictions(lifecycle="due", sort="target_asc", db=test_db)

    assert response["success"] is True
    assert {row["fund_code"] for row in response["data"]} == {"due_old", "due_today"}
    assert response["meta"]["sort"] == "target_asc"
