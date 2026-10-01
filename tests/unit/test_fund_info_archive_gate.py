# -*- coding: utf-8 -*-
"""`fund_info` 建档咽喉上的身份门（第 49 轮 A-1 / B-4 的返修 + 第 50 轮 A-MINOR-2）。

上一批这道门装在 `LLMAnalyzer._save_fund_mapping` 上，而那个函数自 `9c598cf`
（2026-09-21 板块→基金改 agent 闭环）起在 `src/` 里**零调用方** —— 判据直接
`object.__new__(LLMAnalyzer)` 调那个私有方法，于是"绿灯替死代码作保"，而真正活着的
建档点是 `SectorFundService.ensure_fund_info_exists`。页面上填一个股票代码时
**档案先落库、身份门后判**，老板那批"活预测 0、净值 0"的垃圾档案就是这么攒出来的。

所以这里的判据一律**从路由/服务层打进去**，不许直接调私有方法（A/B 两席同一句建议）。

**夹具必须开外键**（第 50 轮我自己量出来的那一层）：`sector_fund_mapping.fund_code`
有 FK 指向 `fund_info`，而应用侧的 SQLite 引擎由 `src/models/database.py`
统一 `PRAGMA foreign_keys = ON`。上一版夹具用裸 `create_engine('sqlite:///:memory:')`
⇒ 外键默认不生效 ⇒ 它一边放着"门拒建档案、映射行却改到那个码上"这种**真库会当场报错**
的形状过绿灯，一边也看不见生产（实测**没有**这条约束，`pg_constraint` 0 行）会静默留下
一行指向查无此码的映射。两个库形状不同 ≠ 可以只测其中一种，所以夹具照应用的样子开 FK。
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundInfo, SectorFundMapping


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')

    @event.listens_for(engine, "connect")
    def _fk_on(conn, _rec):
        # 与 `src/models/database._create_sqlite_engine` 同一句：外键在应用侧是真生效的
        conn.cursor().execute("PRAGMA foreign_keys = ON")

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


def test_the_fixture_enforces_the_foreign_key_the_way_the_app_does(db):
    """控制断言（没有它，上面那段"夹具必须开 FK"就只是一句说明文）：
    往映射行写一个**没有档案**的代码，夹具必须当场拒。
    """
    with pytest.raises(IntegrityError):
        db.add(SectorFundMapping(sector_name='半导体', fund_code='999999'))
        db.commit()
    db.rollback()


def test_the_create_route_refuses_to_mint_a_fund_archive_for_a_stock(db, monkeypatch):
    """**路由级**：`POST /api/config/sector-mappings` 填股票代码 ⇒ 档案不建、映射行也不建。

    第 7 轮的旧契约是"行保留、标成不可服务、带上理由"，但那一半在这里做不到：
    档案刚被同一道门拒建 ⇒ 行的 `fund_code` 就是个悬空引用（镜像 FK 拒、生产没 FK
    于是静默留脏）。所以当场拒到底，理由回给调用方，并留在日志里。
    """
    from src.api.routes import config as cfg

    asked = _门(monkeypatch, STOCK)
    res = cfg.create_sector_mapping(
        cfg.MappingCreate(sector_name='秦安股份', fund_code='603758', fund_name='秦安股份'),
        owner_confirm=False, db=db)

    assert asked, '身份门一次都没被问 ⇒ 建档仍然是无门的'
    assert db.query(FundInfo).filter_by(fund_code='603758').count() == 0, \
        '判"不是基金"却还是补了基金档案 ⇒ 垃圾码又回来了（这一族的原罪）'
    assert db.query(SectorFundMapping).filter_by(sector_name='秦安股份').count() == 0, \
        '档案没建成还把映射行写进去：镜像是外键报错、生产是一行悬空标的，两个都不能要'
    assert res['success'] is False and '603758' in res['message'] and '秦安股份' in res['message'], \
        '拒建必须把"拒的是哪个码、为什么"回给老板：%s' % res['message']


def test_the_create_route_still_archives_a_real_fund(db, monkeypatch):
    """反向对照（闸不是墙）：门说"没意见"时建档照旧，否则一次网络抖动就建不了映射。"""
    from src.api.routes import config as cfg

    _门(monkeypatch, (None, None))
    res = cfg.create_sector_mapping(
        cfg.MappingCreate(sector_name='医药', fund_code='001001', fund_name='医药基金'),
        owner_confirm=False, db=db)
    assert res['success'] is True, res
    assert db.query(FundInfo).filter_by(fund_code='001001').count() == 1
    assert db.query(SectorFundMapping).filter_by(sector_name='医药').count() == 1


def test_saving_a_row_onto_a_stock_code_leaves_the_row_exactly_as_it_was(db, monkeypatch):
    """PUT 改绑到股票代码：整笔拒改 —— 旧的正常标的不许因为一次打错而失去服务。

    旧写法会先 `mapping.fund_code = 新码` 再标"不可服务"：档案被门拒建之后那一写
    在镜像上撞 FK（回给老板一句 `IntegrityError` 原文），在生产（无 FK）留下一行
    指向查无此码的映射；而"把旧行标成不可服务"又是另一处冤枉 —— 那一行指的是上一只基金。
    """
    from src.api.routes import config as cfg

    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.add(SectorFundMapping(sector_name='半导体', fund_code='512480',
                             fund_name='半导体ETF', reviewed=True, is_fetchable=True))
    db.commit()
    row_id = db.query(SectorFundMapping).filter_by(sector_name='半导体').first().id
    _门(monkeypatch, STOCK)

    res = cfg.update_sector_mapping(row_id, cfg.MappingUpdate(fund_code='603758',
                                                             fund_name='秦安股份'),
                                   owner_confirm=False, db=db)
    db.expire_all()
    row = db.get(SectorFundMapping, row_id)
    assert res['success'] is False and '保存被拒' in res['message'], res
    assert row.fund_code == '512480' and row.fund_name == '半导体ETF', \
        '拒改还改了标的：%s/%s' % (row.fund_code, row.fund_name)
    assert row.reviewed is True and row.is_fetchable is True, \
        '一次打错的编辑不许把原来的正常标的打成"不可服务/未审查"（读路径会立刻失去这个板块）'
    assert db.query(FundInfo).filter_by(fund_code='603758').count() == 0


def test_one_save_asks_the_identity_gate_once_not_twice(db, monkeypatch):
    """第 50 轮 A-MINOR-2：补档案以前由路由先做（那道门自己再探一次）⇒ 一次保存两圈外网。

    现在门在 `update_mapping` 里问过就顺手补档案（`identity_checked=True`），
    所以"探过几次"是一个可数的数 —— 不数就会悄悄退回两次（多一个 caller 就多一圈）。
    """
    from src.api.routes import config as cfg

    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.add(SectorFundMapping(sector_name='半导体', fund_code='512480',
                             fund_name='半导体ETF'))
    db.commit()
    row_id = db.query(SectorFundMapping).filter_by(sector_name='半导体').first().id
    asked = _门(monkeypatch, (None, None))

    res = cfg.update_sector_mapping(row_id, cfg.MappingUpdate(fund_code='001001',
                                                             fund_name='医药基金'),
                                   owner_confirm=False, db=db)
    assert res['success'] is True, res
    assert [a[0] for a in asked] == ['001001'], '同一次保存问了 %d 次身份：%s' % (len(asked), asked)
    assert db.query(FundInfo).filter_by(fund_code='001001').count() == 1, \
        '省掉路由那一次调用之后，补档案必须仍在同一次保存里发生（否则新码写进行就撞 FK）'


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


# ---------------------------------------------------------------------------
# 第四处活路：整库导入的"补齐依赖"占位建档（第 88 批）
# ---------------------------------------------------------------------------
# `_create_mapping_fund_dependencies` 以前照单全收：导入的映射行里只要写着一个股票代码，
# 它就在 `fund_info` 里补一条"有档案、净值永远取不到"的占位记录 —— 老板那批
# `603758 / 600189 / 152788` 的形状有一个来源就是这里。它必须问**同一道门**，
# 不在导入这一路立第二把尺子。

def _mapping_spec():
    from src.services.data_portability_service import TABLE_SPECS
    return next(s for s in TABLE_SPECS if s.export_key == "sector_fund_mapping")


def _import_placeholders(db, monkeypatch, rows, verdict):
    """真走那条补齐路，把身份门的答复换成交进来的那一种。"""
    from src.services import data_portability_service as dps
    monkeypatch.setattr(dps, '_manual_identity_verdict', lambda *a, **k: verdict,
                        raising=False)
    # 门是在函数体里 import 的 ⇒ 打桩必须打在**来源模块**上
    from src.services import sector_fund_service as sfs
    monkeypatch.setattr(sfs, '_manual_identity_verdict', lambda *a, **k: verdict)
    svc = dps.DataPortabilityService(db)
    warnings = []
    created = svc._create_mapping_fund_dependencies(_mapping_spec(), rows, warnings)
    return created, warnings, svc


def _rows(codes):
    return [{"sector_name": "测试板块", "fund_code": c, "fund_name": "占位名"} for c in codes]


def test_bulk_import_refuses_to_create_an_archive_for_a_stock_code(db, monkeypatch):
    """门说"这不是基金" ⇒ 一行档案都不许多，而且必须把拒了谁说出来（不许静默）。"""
    created, warnings, svc = _import_placeholders(
        db, monkeypatch, _rows(["603758"]), ("这只代码在基金域查无此码", {}))
    assert created == 0
    assert db.query(FundInfo).count() == 0
    assert any("603758" in w and "不是基金" in w for w in warnings), warnings
    assert svc.placeholders_refused and "603758" in svc.placeholders_refused[0]


def test_bulk_import_still_creates_the_placeholder_for_a_real_fund(db, monkeypatch):
    """反面对照：门给出"没意见"（探针坏了 / 查不到）时**照建** ⇒ 这道门不许建成墙。"""
    created, warnings, svc = _import_placeholders(
        db, monkeypatch, _rows(["510300"]), (None, {"ok": True, "name": "沪深300ETF"}))
    assert created == 1
    assert db.query(FundInfo).filter(FundInfo.fund_code == "510300").count() == 1
    assert svc.placeholders_refused == []
    assert not any("不是基金" in w for w in warnings), warnings


def test_the_import_gate_asks_the_same_ruler_and_says_the_count(db, monkeypatch):
    """两件事一起钉：① 这一路问的是页面那道门（同一个函数），不是自己新搓一把；
    ② 拒了几条就报几条，回执里的数必须数得出（不许"报了拒 2 条"而实际拦了 5 条）。"""
    import ast
    import io as _io
    import os
    src = _io.open(os.path.join('src', 'services', 'data_portability_service.py'),
                   encoding='utf-8').read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_create_mapping_fund_dependencies")
    asked = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == '_manual_identity_verdict']
    assert len(asked) == 1, '这一路要么不问身份门，要么搓了第二把尺子'
    accused = [c for c in ('900001', '900002', '900003')]
    created, warnings, svc = _import_placeholders(
        db, monkeypatch, _rows(accused), ("基金域查无此码", {}))
    assert created == 0 and len(svc.placeholders_refused) == 3
    counted = [w for w in warnings if "不是基金" in w]
    assert len(counted) == 1 and counted[0].startswith("3 条"), counted
