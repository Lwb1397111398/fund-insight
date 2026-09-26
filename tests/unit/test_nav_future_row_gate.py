# -*- coding: utf-8 -*-
"""入库侧的"防未来函数"门：上游把净值日期签成明天，也不许落进 `fund_history`。

起因（2026-09-25 那次「更新基金净值」的实测）：`000725`（大成添利宝货币B）的东财 lsjz
把 `FSRQ` 直接给成 09-26 / 09-27（那天是 09-25），于是生产 `fund_history` 多了 2 条晚于当天的行，
`fund_info.nav_date` 也被档案头写成 09-27。验证侧从第 13 轮起就有"不取目标日之后的行情"这道门，
**入库侧一直没有** —— 脏数据是在写入那一刻进来的，不是判定那一刻。
"""
import ast
import importlib
import logging
import os
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fa = importlib.import_module('src.fund.fund_api')      # 注意：`from src.fund import fund_api`
from src.models.database import Base, FundHistory, FundInfo   # 拿到的是**实例**（包属性被重绑）

TODAY = date.today()


class _Resp:
    def __init__(self, codes_rows):
        self._rows = codes_rows
        self.encoding = None

    def json(self):
        return {'Data': {'LSJZList': [
            {'FSRQ': d.strftime('%Y-%m-%d'), 'DWJZ': '0.3442', 'LJJZ': '1.27', 'JZZZL': '0.00'}
            for d in self._rows]}}


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def get(self, url, params=None, headers=None, timeout=None):
        return _Resp(self.rows)


def _api(rows):
    api = fa.FundAPI()
    api.session = _Session(rows)
    return api


@pytest.fixture
def db():
    engine = create_engine('sqlite://', poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[FundInfo.__table__, FundHistory.__table__])
    session = sessionmaker(bind=engine)()
    session.add(FundInfo(fund_code='000725', fund_name='大成添利宝货币B'))
    session.commit()
    yield session
    session.close()


def test_a_future_nav_row_never_reaches_the_database(db):
    """上游给"后天"的行 ⇒ 取数入口就要挡掉，落到库里必须是 0 条未来行。

    这条是行为判据（真 sqlite、真写入函数），不是文本匹配：把 `usable_history_rows`
    从 `get_fund_history` 的 return 上摘掉，它立刻红。
    同时它也钉"别把闸门建成墙"：今天与昨天的行必须照旧入库。
    """
    rows = [TODAY + timedelta(days=2), TODAY + timedelta(days=1), TODAY,
            TODAY - timedelta(days=1)]
    mgr = fa.FundDataManager()
    mgr.api = _api(rows)

    written = mgr.update_fund_history('000725', days=30, db=db)
    db.commit()

    stored = sorted(r.nav_date for r in db.query(FundHistory).all())
    assert written == 2, '入库条数不是"今天+昨天"两条：%s' % written
    assert stored == [TODAY - timedelta(days=1), TODAY], '未来行还是进了库：%s' % stored
    assert all(d <= TODAY for d in stored), '库里出现晚于今天的净值行：%s' % stored


def test_the_gate_says_aloud_how_many_rows_it_dropped(caplog):
    """丢掉几条必须说出来 —— 静默丢弃与不丢弃同样难查。"""
    api = _api([TODAY + timedelta(days=3), TODAY + timedelta(days=2), TODAY])
    with caplog.at_level(logging.WARNING):
        kept = api.get_fund_history('000725', days=30)
    assert [r['date'] for r in kept] == [TODAY], '该留的没留下：%s' % kept
    text = '\n'.join(rec.message for rec in caplog.records)
    assert '2' in text and '000725' in text, '门没说出丢了谁、丢了几条：%s' % text


def _func(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_every_history_entry_point_passes_through_the_gate(tmp_path):
    """三个 `FundHistory(...)` 写入点都在两个取数入口下游 ⇒ 门必须挂在这两个入口本身，
    而不是"某个函数里有这个名字"（名字出现在 docstring 里不算，第 43 轮那条规矩）。
    """
    src = open(os.path.join(ROOT, 'src', 'fund', 'fund_api.py'), encoding='utf-8').read()
    tree = ast.parse(src, filename='fund_api.py')
    for fname in ('get_fund_history', 'get_fund_history_range'):
        body = _func(tree, fname)
        calls = {n.func.id for n in ast.walk(body)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert 'usable_history_rows' in calls, '%s 没过净值门' % fname

    sync = open(os.path.join(ROOT, 'src', 'fund', 'fund_sync_manager.py'), encoding='utf-8').read()
    tree2 = ast.parse(sync, filename='fund_sync_manager.py')
    body = _func(tree2, 'update_all_funds_info')
    calls = {n.func.id for n in ast.walk(body) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'is_future_nav' in calls, \
        '档案头 `fund.nav_date` 又无条件照抄上游日期了（000725 那次它被写成 09-27）'
