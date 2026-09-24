# -*- coding: utf-8 -*-
"""第 24 批：体检报告必须有 API 与页面出口（连续四轮被评审点到的那条）。

以前只有脚本 stdout 与日志里有这几个数，页面上永远是一个精确到小数点的准确率。
"""
import os
import re
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

    engine = create_engine(
        'sqlite:///' + (tmp_path / 'evidence.db').as_posix(),
        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)

    monkeypatch.setenv('ACCESS_PASSWORD', AUTH)
    # 这里**不再**手工清空进程内 60 秒缓存：每条用例的库都是不同的 tmp_path，
    # 缓存键带上了 bind url 就自然分得开。上一版靠手工清缓存绕过，等于把
    # "换库会不会串数"这件事排除在测试之外（第 24 轮两份复评同点）。

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
    # 库名必须认得出**是哪一个文件**（第 42 轮 B-c）：只写"本地镜像库"这种分类词，
    # 副本回放与临时夹具就会共用同一句标签，而"报数带库名"的全部前提是可分辨。
    assert '本地 sqlite' in rep['database'] and 'evidence.db' in rep['database'], rep['database']
    assert rep['as_of']


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
    assert payload['custom_count'] == 3, payload
    assert payload['owner_confirmed_count'] == 1, '老板真正确认过的只有 1 条'
    assert payload['reviewed_unconfirmed_count'] == 1, '剩下 1 条只是"看过"，不免疫'
    # 内置行不参与第三态：它们没有"老板确认"这个动作可做，算进来卡片只会变成噪音
    assert payload['owner_confirmed_count'] + payload['reviewed_unconfirmed_count']         <= payload['custom_count']


def test_reviewed_counts_do_not_absorb_the_builtin_rows(env):
    """第 30 轮两份复评共同抓到：镜像上一行同时写着
    "共 222 / 已审查 200 / 老板已确认 4 / 看过未确认 119"，而 4+119=123 ——
    那 77 条是静态表合成的内置行，`reviewed=True` 是硬编码占位、`audited:False`，
    既不会被任何体检旗标点到，也没有"确认"这个按钮。把它们算进分子就是假数。
    上一版这条文件里甚至写着 `reviewed_count > custom_count`（把缺陷钉成期望）。

    判据形态：四个数必须**互相闭合**，而不是各自数各自的。
    """
    client, _db = env
    payload = client.get('/api/config/sector-mappings', headers=HEADERS).json()['data']
    builtin = payload['builtin_count']
    assert builtin == payload['total'] - payload['custom_count'] > 0, '这份夹具就该带内置行'
    assert payload['reviewed_count'] + payload['unreviewed_count'] == payload['custom_count'], \
        '"已审查 + 待审查"必须等于在册行数；现在它 %d+%d 对 %d' % (
            payload['reviewed_count'], payload['unreviewed_count'], payload['custom_count'])
    assert payload['reviewed_count'] == payload['owner_confirmed_count'] + payload['reviewed_unconfirmed_count']
    # 内置行确实"没体检过"：不许有任何一档旗标能把它们算成已核对
    rows = [m for m in payload['mappings'] if m.get('source') == 'builtin']
    assert all(r['audited'] is False and r['relevance_state'] is None and r['realigned'] is None
               for r in rows), '内置行被算进了某个体检档'


def test_audit_script_consumes_the_single_source():
    """脚本必须消费 `span_report()`，不许自己再算一遍区间。

    第 24 轮两份复评同点：提交说明写着"审计脚本改成消费它（唯一出处）"，
    而那次改动**没进提交** —— HEAD 里的 `audit_verdict_evidence.py` 仍自带
    `low = 100.0 * (correct - stale_correct) / ...`。今天两边数值凑巧相等，
    但"迟早打架"正是这句话的承诺对象，所以把它钉成断言而不是留在提交说明里。
    """
    import io
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'scripts', 'audit_verdict_evidence.py')
    src = io.open(path, encoding='utf-8').read()
    assert 'span_report(' in src, '脚本没在用唯一出处'
    assert 'from src.services.verdict_evidence import' in src
    for forbidden in ('100.0 * (correct - stale_correct)', 'stale_correct + len(stale'):
        assert forbidden not in src, '脚本里还留着自算区间的式子：%s' % forbidden


def test_report_as_of_uses_the_shared_beijing_clock(env):
    """`as_of` 必须走 `current_as_of()`：Render 没设 TZ，`date.today()` 会每天早 8 小时说"昨天"。"""
    from src.services.prediction_lifecycle import current_as_of

    rep = span_report(env[1])
    assert rep['as_of'] == current_as_of().isoformat()


def test_evidence_cache_is_per_database(tmp_path, monkeypatch):
    """同一进程里换库，第二次请求必须给新库的数。

    为什么单独一条：接口自己写着"带 60 秒缓存"，而缓存原先只按时间分键。
    第 24 轮 A 用两个临时库复现：第一次 judged=1，换绑到 10 条的库后**仍是 1**，
    而 `database` 字段照样印"本地镜像库" —— 与第 23 轮那条错同一形态。
    """
    from src.api.main import app

    monkeypatch.setenv('ACCESS_PASSWORD', AUTH)
    engines, sessions = [], []
    for i, extra in enumerate((0, 9)):
        eng = create_engine('sqlite:///' + (tmp_path / f'c{i}.db').as_posix(),
                            connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng)()
        _seed(s)
        for k in range(extra):                        # 第二个库多 9 条已判结论
            s.add(Prediction(
                post_id=1, blogger_id=1, fund_code='512170', fund_name='x%d' % k,
                sector='测试', prediction_type='up', prediction_date=date(2026, 6, 1),
                prediction_period='1周', target_date=date(2026, 6, 8),
                end_nav=1.05, end_nav_date=date(2026, 6, 8), actual_change=5.0,
                verify_count=1, verify_score=100, status='success', is_correct=True))
        s.commit()
        engines.append(eng)
        sessions.append(s)

    def override_for(idx):
        def dep():
            yield sessions[idx]
        return dep

    client = TestClient(app)
    try:
        app.dependency_overrides[get_db] = override_for(0)
        first = client.get('/api/stats/evidence', headers=HEADERS).json()['data']
        app.dependency_overrides[get_db] = override_for(1)      # 不清缓存，故意踩 TTL
        second = client.get('/api/stats/evidence', headers=HEADERS).json()['data']
        assert first['judged'] == 2, first
        assert second['judged'] == 11, ('缓存把上一个库的数给了新库' % second)
    finally:
        app.dependency_overrides.pop(get_db, None)
        for s in sessions:
            s.close()
        for e in engines:
            e.dispose()


def _text_nodes(html: str) -> str:
    """只留标签之间的正文；属性（含 `:title`）里的内容一律不算"看得见"。"""
    return re.sub(r'<[^>]*>', '\n', html)


def _strip_inert_regions(html: str) -> str:
    """先去掉 `<script>/<style>/注释`，再用"最近的 > 还是 <"判断插值落在哪。

    第 25 轮 A 用三种畸形输入把上一版判据打穿：插值写进 JS 字符串、写进含 `>` 的注释、
    写进属性里，全都被认成正文。那三种位置人都看不见，所以先从文本里摘掉。
    """
    s = re.sub(r'<script\b.*?</script>', ' ', html, flags=re.S | re.I)
    s = re.sub(r'<style\b.*?</style>', ' ', s, flags=re.S | re.I)
    return re.sub(r'<!--.*?-->', ' ', s, flags=re.S)


def _text_interpolations(html: str):
    """返回所有**落在正文里**的 `{{ ... }}` 插值内容（标签属性里的不算）。

    不能用"插值前最近的是 `>` 还是 `<`"这一条：第 25 轮 A 举出属性里含 `>` 的写法
    （`:title="'x' + (1 > 0) + '{{ … }}'"`）会被它误判成正文。这里改成小词法扫描：
    进标签后按引号配对走，只有闭合 `>`（不在引号里的那个）才算标签结束。
    """
    s = _strip_inert_regions(html)
    texts, i, in_tag, quote = [], 0, False, ''
    while i < len(s):
        ch = s[i]
        if in_tag:
            if quote:
                if ch == quote:
                    quote = ''
            elif ch in ('"', "'"):
                quote = ch
            elif ch == '>':
                in_tag = False
            i += 1
            continue
        if ch == '<':
            in_tag = True
            i += 1
            continue
        j = s.find('<', i)
        if j < 0:
            j = len(s)
        texts.append(s[i:j])
        i = j
    body = '\n'.join(texts)
    return [m.group(1) for m in re.finditer(r'\{\{(.*?)\}\}', body, flags=re.S)]


def test_text_interpolation_rule_itself_is_not_foolable():
    """这条测的是**测试的判据**：三种畸形位置必须都不算正文（第 25 轮 A 的三条反例）。"""
    inside_script = '<script>var t = "{{ evidenceReport.judged }}";</script>'
    inside_comment = '<!-- {{ evidenceReport.judged }} -->'
    inside_attr = '<div :title="\'x\' + (1 > 0) + \'{{ evidenceReport.judged }}\'"></div>'
    for bad in (inside_script, inside_comment, inside_attr):
        assert not any('evidenceReport.judged' in x for x in _text_interpolations(bad)), \
            '畸形位置被判成正文 ⇒ 这条守护又是恒真的：%s' % bad[:48]
    good = '<div class="x">{{ evidenceReport.judged }}</div>'
    assert any('evidenceReport.judged' in x for x in _text_interpolations(good))


def test_current_as_of_fallback_leaves_a_trail(monkeypatch, caplog):
    """容器缺 tzdata 时 `current_as_of()` 会回退，但**必须留一行 WARNING**。

    第 25 轮 B：回退本身可以接受，静默不行 —— 这条修复针对的就是"生产上日期差一天"，
    如果它在生产永远走回退而没人知道，那修了等于没修。
    """
    import logging
    import sys
    import types

    from src.services.prediction_lifecycle import current_as_of

    fake = types.ModuleType('zoneinfo')

    def boom(*_a, **_k):
        raise RuntimeError('No time zone found with key Asia/Shanghai')
    fake.ZoneInfo = boom
    monkeypatch.setitem(sys.modules, 'zoneinfo', fake)
    with caplog.at_level(logging.WARNING):
        got = current_as_of()
    assert got                            # 确实回退了
    assert any('tzdata' in r.getMessage() for r in caplog.records), \
        '回退没留任何痕迹 ⇒ 生产上这条修复静默失效也不会被发现'


def test_the_page_shows_the_numbers_as_text_not_only_a_title():
    """窄屏没有 hover：这几个数必须是**正文插值**，不能只活在 `title` 里。

    原先这条只是 `field in html` 的子串检查 —— 把全部数字塞进 `:title="..."` 它照样绿，
    恰好挡不住它声称要挡的那次回归（第 24 轮两份复评共同点到，属于"恒真守护"）。
    """
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', '..', 'web', 'index.html'),
                encoding='utf-8').read()
    assert '/api/stats/evidence' in html, '页面根本没去取体检报告'
    text = '\n'.join(_text_interpolations(html))
    for field in ('evidenceReport.judged', 'evidenceReport.stale_evidence',
                  'evidenceReport.stale_pct', 'evidenceReport.database',
                  'evidenceReport.as_of', 'spanText()'):
        assert field in text, '%s 不在正文插值里：手机上就看不见' % field
    # 区间那两个数经 `spanText()` 中转，所以钉它定义里读的字段
    span_def = re.search(r'const spanText = \(\) =>(.*?);', html, flags=re.S)
    assert span_def and 'span_low_pct' in span_def.group(1) \
        and 'span_high_pct' in span_def.group(1), 'spanText 没在读区间两个数'
    # 一条已判结论都没有时不能显示 "0.0%~0.0%"（会被读成"准确率为零"）
    assert '还没有已判结论' in _text_nodes(html)
    # 第三态的两个数也必须走正文（`:title` 里那句补充说明可以有，但数不能只在那儿）
    assert 'sectorMappings.owner_confirmed_count' in text
    assert 'sectorMappings.reviewed_unconfirmed_count' in text

