from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from src.api.routes.predictions import PredictionUpdate
from src.models.database import Base, Blogger, FundInfo, Post, Prediction
from src.services.prediction_service import PredictionService


def _seed_prediction(db, *, verified=False):
    blogger = Blogger(
        name="预测安全测试博主",
        platform="wechat",
        total_predictions=1 if verified else 0,
        correct_predictions=1 if verified else 0,
        total_verify_score=80 if verified else 0,
        accuracy_rate=80 if verified else 0,
    )
    fund = FundInfo(
        fund_code="SAFE01",
        fund_name="预测安全测试基金",
        active_predictions=1,
        can_delete=False,
    )
    db.add_all([blogger, fund])
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="预测安全测试帖子",
        content="用于验证预测管理不会丢失资料。",
        post_date=date(2026, 7, 1),
        analyzed=True,
    )
    db.add(post)
    db.flush()
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund.fund_code,
        fund_name=fund.fund_name,
        sector="白酒",
        prediction_type="up",
        prediction_content="未来一周上涨",
        confidence=80,
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=post.post_date + timedelta(days=7),
        next_verify_date=post.post_date + timedelta(days=5),
        status="success" if verified else "pending",
        is_expired=verified,
        is_correct=True if verified else None,
        verify_count=1 if verified else 0,
        verify_score=80 if verified else 0,
        is_deleted=False,
    )
    db.add(prediction)
    db.commit()
    return blogger, fund, prediction


def test_delete_prediction_archives_record_and_updates_denormalized_stats(test_db):
    blogger, fund, prediction = _seed_prediction(test_db, verified=True)

    assert PredictionService(test_db).delete_prediction(prediction.id) is True

    test_db.expire_all()
    archived = test_db.get(Prediction, prediction.id)
    assert archived is not None
    assert archived.is_deleted is True
    assert archived.deleted_at is not None
    assert archived.restore_before is not None
    assert test_db.get(Blogger, blogger.id).total_predictions == 0
    saved_fund = test_db.query(FundInfo).filter(FundInfo.fund_code == fund.fund_code).one()
    assert saved_fund.active_predictions == 0
    assert saved_fund.can_delete is True


def test_restore_prediction_reactivates_record_and_recalculates_stats(test_db):
    blogger, fund, prediction = _seed_prediction(test_db, verified=True)
    service = PredictionService(test_db)
    service.delete_prediction(prediction.id)

    assert service.restore_prediction(prediction.id) is True

    test_db.expire_all()
    restored = test_db.get(Prediction, prediction.id)
    assert restored.is_deleted is False
    assert restored.deleted_at is None
    assert restored.delete_reason is None
    assert test_db.get(Blogger, blogger.id).total_predictions == 1
    saved_fund = test_db.query(FundInfo).filter(FundInfo.fund_code == fund.fund_code).one()
    assert saved_fund.active_predictions == 1
    assert saved_fund.can_delete is False


def test_pending_prediction_period_update_recalculates_target_and_verify_dates(test_db):
    _, _, prediction = _seed_prediction(test_db)

    updated = PredictionService(test_db).update_prediction_fields(
        prediction.id,
        {"prediction_period": "1个月"},
    )

    assert updated.prediction_period == "1个月"
    assert updated.target_date == date(2026, 7, 31)
    assert updated.next_verify_date <= updated.target_date
    assert updated.next_verify_date >= updated.prediction_date


def test_verified_prediction_rejects_changes_to_verification_inputs(test_db):
    _, _, prediction = _seed_prediction(test_db, verified=True)

    with pytest.raises(ValueError, match="已验证预测"):
        PredictionService(test_db).update_prediction_fields(
            prediction.id,
            {"prediction_type": "down"},
        )

    test_db.refresh(prediction)
    assert prediction.prediction_type == "up"


@pytest.mark.parametrize("field,value", [
    ("prediction_type", "sideways"),
    ("prediction_period", "两三天"),
    ("confidence", 101),
])
def test_prediction_update_schema_rejects_unsupported_values(field, value):
    with pytest.raises(ValidationError):
        PredictionUpdate(**{field: value})


def test_put_prediction_route_updates_pending_prediction(monkeypatch, test_db):
    """前端编辑表单走的 PUT /api/predictions/{id} 必须能更新未验证预测。"""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.api.deps import get_db
    from src.api.main import app

    monkeypatch.setenv("ACCESS_PASSWORD", "update_route_test_password")
    engine = _memory_engine()
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        _, _, prediction = _seed_prediction(db)

        client = TestClient(app)
        headers = {"X-Access-Password": "update_route_test_password"}
        resp = client.put(
            f"/api/predictions/{prediction.id}",
            headers=headers,
            json={"confidence": 66, "prediction_period": "1个月"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        db.refresh(prediction)
        assert prediction.confidence == 66
        assert prediction.prediction_period == "1个月"
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)


def test_put_prediction_rebind_asks_the_evidence_gate_and_clears_the_lock(monkeypatch, test_db):
    """人工改绑必须过**同一道**改标证据门，并且旧标的上的重问锁要一起清掉（第 53 轮 A-3）。

    上一版这条路直接 `prediction.fund_code = …`：不问"这只标的给不给得出这段窗口"，
    也不动 `next_verify_date` ⇒ 一把压在旧标的上的锁会让**新标的的第一问**被
    `was_locked_previously` 读成"第二次"，行当场进回收站还写着"已问过两次"。
    判据从路由打进去（PUT 一次），不是直接调私有方法 —— 后者只能证明"这函数会自检"。
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from src.api.deps import get_db
    from src.api.main import app
    from src.models.database import FundHistory

    monkeypatch.setenv("ACCESS_PASSWORD", "rebind_gate_password")
    engine = _memory_engine()
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        _, _, prediction = _seed_prediction(db)
        # 行上先压一把"锁在未来"的重问日期（只有 `apply_unverifiable_hold` 会写在这种形状上）
        prediction.next_verify_date = date(2026, 8, 1)
        # 一只"库里只有窗口之后净值"的标的 ⇒ 这段窗口它给不出证据
        db.add_all([
            FundInfo(fund_code='LATE99', fund_name='晚到的基金'),
            FundHistory(fund_code='LATE99', nav_date=date(2026, 9, 1), nav=1.0),
            FundHistory(fund_code='LATE99', nav_date=date(2026, 9, 2), nav=1.01),
            # 一只窗口两端都有行的标的 ⇒ 照常允许改过去
            FundInfo(fund_code='GOOD99', fund_name='对得上的基金'),
            FundHistory(fund_code='GOOD99', nav_date=date(2026, 7, 1), nav=0.98),
            FundHistory(fund_code='GOOD99', nav_date=date(2026, 7, 8), nav=1.05),
        ])
        db.commit()
        client = TestClient(app)
        headers = {"X-Access-Password": "rebind_gate_password"}

        resp = client.put(f"/api/predictions/{prediction.id}", headers=headers,
                          json={"fund_code": "LATE99", "fund_name": "晚到的基金"})
        assert resp.status_code == 400, '给不出这段窗口的标的，人工改绑也不能放行'
        assert '不能把这条预测改到' in resp.json()["detail"]
        db.refresh(prediction)
        assert prediction.fund_code == 'SAFE01', '被拒的改绑一个字都不许留下'
        assert prediction.next_verify_date == date(2026, 8, 1)

        resp = client.put(f"/api/predictions/{prediction.id}", headers=headers,
                          json={"fund_code": "GOOD99", "fund_name": "对得上的基金"})
        assert resp.status_code == 200, resp.text
        db.refresh(prediction)
        assert prediction.fund_code == 'GOOD99'
        assert prediction.next_verify_date <= prediction.target_date, (
            '旧标的上的锁必须退回目标日之前 ⇒ 否则新标的的第一问会被当成第二次直接关进回收站')
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)


def test_put_prediction_route_rejects_verified_and_flat(monkeypatch, test_db):
    """已验证预测改验证依据、以及保存观望类型，都必须被 400 拒绝。"""
    from fastapi.testclient import TestClient

    from src.api.deps import get_db
    from src.api.main import app

    monkeypatch.setenv("ACCESS_PASSWORD", "update_route_test_password")
    engine = _memory_engine()
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        _, _, verified_pred = _seed_prediction(db, verified=True)
        # 复用同一博主/帖子/基金，再补一条未验证预测
        pending_pred = Prediction(
            post_id=verified_pred.post_id,
            blogger_id=verified_pred.blogger_id,
            fund_code=verified_pred.fund_code,
            fund_name=verified_pred.fund_name,
            sector="白酒",
            prediction_type="up",
            prediction_content="未来一周上涨",
            confidence=80,
            prediction_date=date(2026, 7, 1),
            prediction_period="1周",
            target_date=date(2026, 7, 8),
            next_verify_date=date(2026, 7, 6),
            status="pending",
            is_deleted=False,
        )
        db.add(pending_pred)
        db.commit()

        client = TestClient(app)
        headers = {"X-Access-Password": "update_route_test_password"}

        resp = client.put(
            f"/api/predictions/{verified_pred.id}",
            headers=headers,
            json={"prediction_type": "down"},
        )
        assert resp.status_code == 400
        assert "已验证" in resp.json()["detail"]

        resp = client.put(
            f"/api/predictions/{pending_pred.id}",
            headers=headers,
            json={"prediction_type": "flat"},
        )
        assert resp.status_code == 400
        assert "观望" in resp.json()["detail"]
        db.refresh(pending_pred)
        assert pending_pred.prediction_type == "up"  # 原值未被污染
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)


def _memory_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine
