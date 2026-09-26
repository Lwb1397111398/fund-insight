# -*- coding: utf-8 -*-
"""第 51 轮两份复评各自点到的两条"页面上点了没反应 / 静默留脏"的活缺陷。

放在同一个文件里的理由：两条都不是"某个函数对不对"，而是**一条从前端按钮打到库里**的
完整链路 —— 帖子编辑（`PATCH` 路由自 `2c227c9` 起不存在，老板点"保存"两个月来收 405）与
映射改绑（空字符串代码曾被当成"改了标的"，而生产没有指向 `fund_info` 的外键 ⇒ 当场多一行
"板块没有标的"、回执还是成功）。

调用姿势：直接打路由函数（不起 TestClient，避开 `ACCESS_PASSWORD` 那层中间件），
但**注册面**用 `app.routes` 说话 —— 那把路径闸（`test_frontend_api_urls_resolve.py`）的边界
注释自己写着"只判路径不判方法"，所以"方法没了"这件事它结构上看不见；本文件那条
`test_..._is_registered_with_the_method` 只钉这一处已知的洞，通用那一版留给下一轮（任务 #98）。
"""
import pytest
from datetime import date

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Blogger, FundInfo, Post, Prediction, SectorFundMapping


@pytest.fixture
def db():
    """开外键的夹具 —— 与 `src/models/database._create_sqlite_engine` 同一句（第 50 轮的账）。"""
    engine = create_engine('sqlite:///:memory:')

    @event.listens_for(engine, "connect")
    def _fk_on(conn, _rec):
        conn.cursor().execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _post(db):
    db.add(Blogger(id=1, name='测试博主'))
    db.add(Post(id=7, blogger_id=1, title='旧标题', content='原文',
                post_date=date(2026, 9, 20), source_url='https://x/1'))
    db.commit()
    return db.get(Post, 7)


def _api_routes():
    from fastapi.routing import APIRoute

    from src.api.main import app
    return [(r.path, set(m.upper() for m in (r.methods or [])))
            for r in app.routes if isinstance(r, APIRoute)]


def test_the_edit_button_targets_a_patch_route_that_is_actually_registered(db):
    """`web/post-manager.js` 那句 `axios.patch('/api/posts/{id}')` 必须有对端的方法。

    路径 `/api/posts/{post_id}` 一直注册着（GET / DELETE）⇒ 只判路径的那把闸恒绿，
    而"保存"两个月来是 405。这里判的是**方法**这一维。
    """
    import io
    import os
    import re

    src = io.open(os.path.join('web', 'post-manager.js'), encoding='utf-8').read()
    calls = set(re.findall(r"axios\.(\w+)\(\s*[`'\"](/api/posts/[^`'\"]*)", src))
    patch_calls = {(v, p) for v, p in calls if v == 'patch'}
    assert patch_calls, '前端那句 axios.patch 不见了？那这条判据就空转了：%s' % sorted(calls)

    routes = _api_routes()
    for verb, path in patch_calls:
        segs = [s for s in path.split('/') if s]
        hit = [p for p, methods in routes
               if len([s for s in p.split('/') if s]) == len(segs)
               and all(b.startswith('{') or a == b for a, b in
                       zip(segs, [s for s in p.split('/') if s]))
               and verb.upper() in methods]
        assert hit, '页面上编辑帖子打的 %s %s 没有注册路由（405）：%s' % (verb.upper(), path, path)


def test_editing_a_post_saves_and_protected_fields_refuse_saying_why(db):
    """改标题要落库；带预测的帖子改正文必须**带着原因**拒绝（不许被吞成一句"保存失败"）。"""
    from src.api.routes import posts as posts_route
    from src.api.schemas.post import PostUpdate

    row = _post(db)
    res = posts_route.update_post(row.id, PostUpdate(title='新标题'), db=db)
    assert res['success'] is True, res
    db.expire_all()
    assert db.get(Post, row.id).title == '新标题'

    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.add(Prediction(blogger_id=1, post_id=row.id, sector='半导体', fund_code='512480',
                      prediction_type='up', prediction_date=date(2026, 9, 1),
                      target_date=date(2026, 9, 20)))
    db.commit()
    body = posts_route.update_post(row.id, PostUpdate(content='改掉正文'), db=db)
    assert body['success'] is False and '只能修改标题' in body['message'], \
        '拒绝被抛成 HTTP 错误码 ⇒ 屏幕上只剩"保存失败"四个字：%s' % body
    db.expire_all()
    assert db.get(Post, row.id).content == '原文'


def test_an_empty_fund_code_is_refused_instead_of_unlinking_the_sector(db, monkeypatch):
    """改绑成空字符串以前算"改了标的"：探针答"没意见"、档案不建，于是映射被写成空标的。

    生产 `sector_fund_mapping` 实测**没有**指向 `fund_info` 的外键（`pg_constraint` 查该表
    contype='f' ⇒ 0 行）⇒ 那种行会静默落库并回执"已更新映射"；镜像有外键 ⇒ 回一句
    `FOREIGN KEY constraint failed` 原文。两边都不接受 ⇒ 整笔拒、旧标的一个字不动。
    """
    import src.services.sector_fund_service as sfs

    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.add(SectorFundMapping(sector_name='半导体', fund_code='512480',
                             fund_name='半导体ETF', reviewed=True, is_fetchable=True))
    db.commit()
    row_id = db.query(SectorFundMapping).filter_by(sector_name='半导体').first().id
    # 必须走 monkeypatch：直接赋值会把桩**留在模块上**，同一次跑批里后面的用例就被
    # "探针永远没意见"替它作了保（我自己这一批当场复现过一次，两条 manual_proof 用例假绿）
    monkeypatch.setattr(sfs, '_manual_identity_verdict',
                        lambda code, name, sector='': (None, None))

    assert sfs.get_sector_fund_service(db).update_mapping(row_id, fund_code='   ') is None
    db.expire_all()
    row = db.get(SectorFundMapping, row_id)
    assert row.fund_code == '512480' and row.reviewed is True and row.is_fetchable is True, \
        '空代码还是把板块从它的基金上摘了下来：%r' % (row.fund_code,)
