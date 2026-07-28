"""基金数据管理器回归测试。"""
from datetime import date
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.fund.fund_api import FundDataManager
from src.models.database import Base, FundInfo


def test_update_fund_info_preserves_existing_name_and_type_when_api_omits_them(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'fund-data.db').as_posix()}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(FundInfo(
            fund_code="000001",
            fund_name="原基金名",
            fund_type="指数型",
            nav_date=date(2026, 7, 1),
        ))
        db.commit()
        manager = FundDataManager()
        manager.api = Mock()
        manager.api.get_fund_info.return_value = {
            "fund_name": None,
            "fund_type": "",
            "nav": 1.2,
            "nav_date": "2026-07-02",
            "day_growth": 0.1,
        }
        manager.api.get_fund_history.return_value = []

        manager.update_fund_info("000001", db=db)

        fund = db.query(FundInfo).filter_by(fund_code="000001").one()
        assert fund.fund_name == "原基金名"
        assert fund.fund_type == "指数型"
        assert fund.latest_nav == 1.2
        assert fund.nav_date == date(2026, 7, 2)
    finally:
        db.close()
