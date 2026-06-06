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


def test_fund_service_exposes_recent_history_query_helper(test_db):
    service = FundService(test_db)

    assert hasattr(service, "_get_recent_history_map")
