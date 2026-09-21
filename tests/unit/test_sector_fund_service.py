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


def _reset_sector_cache():
    SectorFundService._cache = {}
    SectorFundService._cache_loaded = False
    SectorFundService._cache_at = 0


def test_cancelled_review_must_reach_the_matcher(test_db):
    """第 14 轮 MAJOR-1：取消审查必须能在 TTL 之后传到匹配链，否则新预测继续挂错标的。

    `get_fund_by_sector` 以前"命中 reviewed 就 return"且从不过期，重灌时那条
    "已审查条目不被未审查条目覆盖"的守卫又挡住了降级 —— 而本轮把这条路径提到了
    `match_fund_with_fallback` 的最前端。
    """
    from src.utils.fund_matching import match_fund_with_fallback

    _reset_sector_cache()
    row = SectorFundMapping(sector_name='算力', fund_code='510300', fund_name='沪深300ETF',
                            is_active=True, reviewed=True,
                            match_source='agent', verified_at=__import__('datetime').datetime.now())
    test_db.add(row)
    test_db.commit()
    svc = SectorFundService(test_db)
    assert svc.get_fund_by_sector('算力')['code'] == '510300'

    row.reviewed = False
    test_db.commit()
    SectorFundService._cache_at = 0          # 让 TTL 到期
    assert svc.get_fund_by_sector('算力') is None or not svc.get_fund_by_sector('算力').get('reviewed'), \
        '取消审查后缓存仍返回旧的 reviewed 条目'

    code, _name = match_fund_with_fallback(
        pred={}, sector='算力', fund_auto_manager=None, llm_analyzer=None, db=test_db)
    assert code != '510300', '已被取消审查的标的仍在驱动新预测挂错基金'


def test_batch_review_without_owner_confirm_grants_no_immunity(test_db):
    """第 14 轮 MAJOR-2：不确认就不署名 owner、不锁定 —— 否则一键就买到永久体检豁免。"""
    _reset_sector_cache()
    row = SectorFundMapping(sector_name='机器人', fund_code='562500', fund_name='机器人ETF',
                            is_active=True, reviewed=False,
                            match_source='agent', verified_at=__import__('datetime').datetime.now())
    test_db.add(row)
    test_db.commit()

    SectorFundService(test_db).batch_mark_reviewed([row.id], reviewed=True, owner_confirm=False)
    test_db.refresh(row)
    assert row.reviewed is True
    assert row.owner_locked is not True, '未确认的批量点击不该给行加上老板锁定'
    assert row.reviewed_by != 'owner', '未确认的批量点击不该冒充老板署名'

    row2 = SectorFundMapping(sector_name='储存', fund_code='512400', fund_name='有色ETF',
                             is_active=True, reviewed=False,
                             match_source='agent', verified_at=__import__('datetime').datetime.now())
    test_db.add(row2)
    test_db.commit()
    SectorFundService(test_db).batch_mark_reviewed([row2.id], reviewed=True, owner_confirm=True)
    test_db.refresh(row2)
    assert row2.owner_locked is True and row2.reviewed_by == 'owner'
