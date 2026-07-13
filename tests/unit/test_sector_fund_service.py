"""板块映射数据保护测试。"""
from src.models.database import FundInfo, SectorFundMapping
from src.services.sector_fund_service import SectorFundService


def test_cascade_conflict_resolution_preserves_existing_fund_data(test_db):
    """修改映射时只能停用冲突映射，不能删除基金资料。"""
    preferred = FundInfo(fund_code="000001", fund_name="首选基金", sector_type="人工智能")
    historical = FundInfo(fund_code="000002", fund_name="历史基金", sector_type="人工智能")
    test_db.add_all([preferred, historical])
    test_db.flush()

    conflict = SectorFundMapping(
        sector_name="人工智能",
        fund_code="000002",
        fund_name="历史基金",
        is_active=True,
    )
    test_db.add(conflict)
    test_db.commit()

    result = SectorFundService(test_db).cascade_cleanup_conflicts(
        "人工智能", "000001", "首选基金"
    )

    test_db.refresh(conflict)
    assert result == {"sector_fund_mapping": 1, "fund_info": 0}
    assert conflict.is_active is False
    assert test_db.query(FundInfo).filter(FundInfo.fund_code == "000002").one().fund_name == "历史基金"
