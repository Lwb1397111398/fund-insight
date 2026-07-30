"""基金 API 与服务回归测试。"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.routes.funds import get_funds, router
from src.models.database import Base, Blogger, FundInfo, Post, Prediction
from src.services.fund_service import FundService


def test_fund_list_uses_real_prediction_count(tmp_path):
    db = _session(tmp_path)
    try:
        blogger = Blogger(name="计数博主", platform="test")
        db.add(blogger)
        db.flush()
        post = Post(blogger_id=blogger.id, content="预测", post_date=date.today())
        db.add(post)
        db.flush()
        db.add(FundInfo(fund_code="000002", fund_name="计数基金", active_predictions=0, can_delete=True))
        db.add(Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="000002",
            prediction_type="up",
            prediction_date=date.today(),
            is_deleted=False,
        ))
        db.commit()

        funds = FundService(db).get_funds_with_grouping(group_by_sector=False)

        assert funds[0]["active_predictions"] == 1
        assert funds[0]["can_delete"] is False
    finally:
        db.close()


def test_required_route_imports_exist():
    from src.api.routes import funds

    assert hasattr(funds, "FundInfo")
    assert hasattr(funds, "FundHistory")


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'fund-routes.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_fund_detail_service_returns_existing_fund(tmp_path):
    db = _session(tmp_path)
    try:
        db.add(FundInfo(fund_code="000001", fund_name="测试基金", latest_nav=1.25))
        db.commit()

        result = FundService(db).get_fund_detail("000001")

        assert result["fund_name"] == "测试基金"
        assert result["history"] == []
    finally:
        db.close()


def test_delete_fund_checks_real_active_predictions(tmp_path):
    db = _session(tmp_path)
    try:
        blogger = Blogger(name="测试博主", platform="test")
        db.add(blogger)
        db.flush()
        post = Post(blogger_id=blogger.id, content="测试", post_date=date.today())
        db.add(post)
        db.flush()
        db.add(FundInfo(
            fund_code="000001",
            fund_name="测试基金",
            active_predictions=0,
            can_delete=True,
        ))
        db.add(Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            fund_code="000001",
            prediction_type="up",
            prediction_date=date.today(),
            is_deleted=False,
        ))
        db.commit()

        result = FundService(db).delete_fund("000001")

        assert result["success"] is False
        assert db.query(FundInfo).filter_by(fund_code="000001").one()
    finally:
        db.close()


def test_fund_router_exposes_detail_and_delete_routes():
    routes = {(route.path, method) for route in router.routes for method in route.methods}

    assert ("/funds/{fund_code}", "GET") in routes
    assert ("/funds/{fund_code}", "DELETE") in routes


def test_fund_list_response_includes_pagination_metadata(tmp_path):
    db = _session(tmp_path)
    try:
        db.add_all([
            FundInfo(fund_code=f"{index:06d}", fund_name=f"基金 {index}")
            for index in range(101)
        ])
        db.commit()

        response = get_funds(skip=100, limit=100, sector_type=None, group_by_sector=False, db=db)

        assert len(response["data"]) == 1
        assert response["meta"] == {"total": 101, "page": 2, "page_size": 100, "pages": 2}
    finally:
        db.close()
