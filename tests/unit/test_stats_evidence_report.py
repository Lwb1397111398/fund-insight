# -*- coding: utf-8 -*-
"""第 24 批：体检报告必须有 API 与页面出口（连续四轮被评审点到的那条）。

以前只有脚本 stdout 与日志里有这几个数，页面上永远是一个精确到小数点的准确率。
"""
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.deps import get_db
from src.models.database import (Base, Blogger, FundHistory, Prediction, Post,
                                 SectorFundMapping)
from src.services.verdict_evidence import span_report

AUTH = 'evidence-report-test'
HEADERS = {'X-Access-Password': AUTH}


def _seed(db):
    """两条已判结论：512170 的证据还复现得出来（判对），512480 那天没有净值行（判错）。"""
    blogger = Blogger(name='报告博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='报告帖子', post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    db.add(FundHistory(fund_code='512170', nav_date=date(2026, 6, 8), nav=1.05))
    for code, correct in (('512170', True), ('512480', False)):
        db.add(Prediction(
            post_id=post.id, blogger_id=blogger.id, fund_code=code,
            fund_name='测试基金' + code, sector='测试', prediction_type='up',
            prediction_date=date(2026, 6, 1), prediction_period='1周',
            target_date=date(2026, 6, 8), end_nav=1.05, end_nav_date=date(2026, 6, 8),
            actual_change=5.0, verify_count=1,
            verify_score=100 if correct else 0,
            status='success' if correct else 'failed', is_correct=correct))
    db.add_all([
        SectorFundMapping(sector_name='甲板块', fund_code='512170', fund_name='医疗ETF',
                          is_active=True, reviewed=True, owner_locked=True,
                          reviewed_by='owner'),
        SectorFundMapping(sector_name='乙板块', fund_code='512480', fund_name='半导体ETF',
                          is_active=True, reviewed=True),
        SectorFundMapping(sector_name='丙板块', fund_code='159995', fund_name='芯片ETF',
                          is_active=True, reviewed=False),
    ])
    db.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """自建库 + TestClient。

    不能用共享的 `test_db`：TestClient 在工作线程里跑，SQLite 默认禁止跨线程复用连接
    （本仓库其它路由测试都是这个形状）。
    """
    from src.api.main import app
    import src.api.routes.stats as stats_routes

    engine = create_engine(
        'sqlite:///' + (tmp_path / 'evidence.db').as_posix(),
        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)

    monkeypatch.setenv('ACCESS_PASSWORD', AUTH)
    # 进程内 60 秒缓存必须每次清空，否则上一条用例的报告会被这一条读到
    stats_routes._evidence_cache.update({'at': 0.0, 'report': None})

    def override():
        yield session

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


def test_span_report_numbers_and_labels_the_database(env):
    """报告里的数要能对上，而且**必须带库名**（第 23 轮我把镜像数当系统数报了十几轮）。"""
    _client, db = env
    rep = span_report(db)
    assert rep['judged'] == 2 and rep['correct'] == 1
    assert rep['accuracy_pct'] == 50.0
    assert rep['stale_evidence'] == 1 and rep['stale_pct'] == 50.0
    assert rep['by_kind'] == {'nav_row_missing': 1}
    # 失效那批按"全判错 / 全判对"两端折算；这批里判对的有 0 条 ⇒ 下界就等于现在的 50%
    assert rep['span_low_pct'] == 50.0 and rep['span_high_pct'] == 100.0
    assert rep['database'].endswith('库') and rep['as_of']


def test_stats_evidence_endpoint_returns_the_same_report(env):
    """页面拿的是这个接口，所以它必须与服务层同源，不是第二份算式。"""
    client, db = env
    res = client.get('/api/stats/evidence', headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()['data'] == span_report(db)


def test_mapping_list_counts_the_third_state(env):
    """卡片要能分开"看过"与"老板确认过"（后者才免疫体检与 AI 覆盖）。"""
    client, _db = env
    res = client.get('/api/config/sector-mappings', headers=HEADERS)
    assert res.status_code == 200, res.text
    payload = res.json()['data']
    # 两态那行照旧含内置行（既有行为，不改）；第三态只数自定义行
    assert payload['reviewed_count'] > payload['custom_count']
    assert payload['custom_count'] == 3, payload
    assert payload['owner_confirmed_count'] == 1, '老板真正确认过的只有 1 条'
    assert payload['reviewed_unconfirmed_count'] == 1, '剩下 1 条只是"看过"，不免疫'
    # 内置行不参与第三态：它们没有"老板确认"这个动作可做，算进来卡片只会变成噪音
    assert payload['owner_confirmed_count'] + payload['reviewed_unconfirmed_count']         <= payload['custom_count']


def test_the_page_shows_the_numbers_as_text_not_only_a_title():
    """窄屏没有 hover：这几个数必须是**正文**，不能只活在 `title` 里。"""
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', '..', 'web', 'index.html'),
                encoding='utf-8').read()
    assert '/api/stats/evidence' in html, '页面根本没去取体检报告'
    for field in ('evidenceReport.judged', 'evidenceReport.stale_evidence',
                  'span_low_pct', 'span_high_pct', 'evidenceReport.database',
                  'evidenceReport.as_of'):
        assert field in html, '页面少了 %s：老板还是只能看到一个孤立的小数点' % field
    assert 'sectorMappings.owner_confirmed_count' in html
    assert 'sectorMappings.reviewed_unconfirmed_count' in html
