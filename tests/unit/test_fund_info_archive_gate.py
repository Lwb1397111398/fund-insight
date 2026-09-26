# -*- coding: utf-8 -*-
"""`fund_info` 建档咽喉上的身份门（第 49 轮 A-1 / B-4 的返修）。

上一批这道门装在 `LLMAnalyzer._save_fund_mapping` 上，而那个函数自 `9c598cf`
（2026-09-21 板块→基金改 agent 闭环）起在 `src/` 里**零调用方** —— 判据直接
`object.__new__(LLMAnalyzer)` 调那个私有方法，于是"绿灯替死代码作保"，而真正活着的
建档点是 `SectorFundService.ensure_fund_info_exists`（`config.py` 三处路由 + agent +
`full_sync` 都调它）。页面上填一个股票代码时**档案先落库、身份门后判**，
老板那批"活预测 0、净值 0"的垃圾档案就是这么攒出来的。

所以这里的判据一律**从路由/服务层打进去**，不许直接调私有方法（A/B 两席同一句建议）。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundInfo, SectorFundMapping


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _门(monkeypatch, verdict):
    """把 `_manual_identity_verdict` 换成桩，返回"被问过哪几个 (代码, 名字, 板块)"。"""
    import src.services.sector_fund_service as sfs

    asked = []

    def fake(code, name, sector):
        asked.append((code, name, sector))
        return verdict

    monkeypatch.setattr(sfs, '_manual_identity_verdict', fake)
    return asked


STOCK = ('这只标的在基金域里查不到，实为股票「秦安股份」', {'verdict': 'not_a_fund'})


def test_the_create_route_refuses_to_mint_a_fund_archive_for_a_stock(db, monkeypatch):
    """**路由级**：`POST /api/config/sector-mappings` 填股票代码 ⇒ **基金档案不许落库**。

    这条就是 A-1/B-4 量到的那条活路：旧顺序是先 `ensure_fund_info_exists`（内部 commit）
    再判身份，于是映射行被标成"不可服务"了、垃圾档案却进门了，从此每天被同步任务拉净值。
    映射行本身按既有设计保留为"未审查 + 判不可服务"（`create_sector_mapping` 从第 7 轮起
    就是这个契约：不把人的输入吞掉，但不给它任何"已核对"的分量），所以这里钉的是三件事：
    不建档、不自带已审查、被标不可服务。
    """
    from src.api.routes import config as cfg

    asked = _门(monkeypatch, STOCK)
    res = cfg.create_sector_mapping(
        cfg.MappingCreate(sector_name='秦安股份', fund_code='603758', fund_name='秦安股份'),
        owner_confirm=False, db=db)

    assert asked, '身份门一次都没被问 ⇒ 建档仍然是无门的'
    assert db.query(FundInfo).filter_by(fund_code='603758').count() == 0, \
        '判"不是基金"却还是补了基金档案 ⇒ 垃圾码又回来了（这一族的原罪）'
    row = db.query(SectorFundMapping).filter_by(sector_name='秦安股份').first()
    assert row is not None and row.reviewed is not True, res
    assert row.is_fetchable is False and (row.verify_message or ''), \
        '留着这行可以，但必须带着"为什么不可服务"，否则读路径会把它当已核对过的标的：%s' % row.verify_message


def test_the_create_route_still_archives_a_real_fund(db, monkeypatch):
    """反向对照（闸不是墙）：门说"没意见"时建档照旧，否则一次网络抖动就建不了映射。"""
    from src.api.routes import config as cfg

    _门(monkeypatch, (None, None))
    cfg.create_sector_mapping(
        cfg.MappingCreate(sector_name='医药', fund_code='001001', fund_name='医药基金'),
        owner_confirm=False, db=db)
    assert db.query(FundInfo).filter_by(fund_code='001001').count() == 1
    assert db.query(SectorFundMapping).filter_by(sector_name='医药').count() == 1


def test_a_caller_that_already_checked_says_so_and_skips_the_probe(db, monkeypatch):
    """`identity_checked=True` 是显式旁路：已过门的调用方不该重复打接口，
    但**旁路必须是调用方写的**，不能由"代码看起来像基金"推出来。"""
    import src.services.sector_fund_service as sfs

    asked = _门(monkeypatch, STOCK)
    service = sfs.get_sector_fund_service(db)
    assert service.ensure_fund_info_exists('603758', '秦安股份', '秦安股份',
                                           identity_checked=True) is True
    assert asked == [], '旁路参数没起作用 ⇒ 同一次保存会重复打探针（生产 idle in transaction 那一族）'
    assert db.query(FundInfo).filter_by(fund_code='603758').count() == 1


def test_an_existing_archive_is_not_probed_again(db, monkeypatch):
    """档案已存在时不需要身份证明：这道门的职责是"别放新的垃圾进来"，不是复查旧的。"""
    import src.services.sector_fund_service as sfs

    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.commit()
    asked = _门(monkeypatch, STOCK)
    service = sfs.get_sector_fund_service(db)
    assert service.ensure_fund_info_exists('512480', '半导体ETF', '半导体') is False
    assert asked == [], '已存在的档案又被探一次 ⇒ 每次保存都白打一圈外网'
