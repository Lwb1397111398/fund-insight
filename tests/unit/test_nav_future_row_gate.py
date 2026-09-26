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
import pathlib
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


# 净值日期的**落笔点**登记名单：`gated` = 这一处自己过了 `is_future_nav`。
# 为什么不是"要求全部有门"：很多处是**按请求窗口**回写档案头（`update_fund_info` 用请求的
# `nav_date` 参数、`add_fund_with_history` 用调用方给的日期），门在它们的**上游取数入口**
# （`usable_history_rows`）。这张表要钉住的是"有没有新增一处没人数的落笔"，
# 而不是假装 11 处都各自有门 —— 那句话说出去就成了第 49 轮 A 席量到的那种谎。
NAV_DATE_WRITE_SITES = {
    ('src/fund/fund_api.py', 'update_fund_info'): 'gated',   # 第 49 轮 A 席量到的第 2 处，09-26 补上门
    ('src/fund/fund_api.py', 'update_fund_history'): 'fed-by-gated-entry',
    ('src/fund/fund_api.py', 'backfill_history_range'): 'ungated-upstream',
    ('src/fund/fund_auto_manager.py', 'auto_add_fund_for_prediction'): 'ungated-upstream',
    ('src/fund/fund_sync_manager.py', 'sync_missing_funds'): 'ungated-upstream',
    ('src/fund/fund_sync_manager.py', 'update_all_funds_info'): 'gated',
    ('src/fund/fund_sync_manager.py', '_update_fund_history'): 'ungated-upstream',
    ('src/fund/fund_sync_manager.py', 'sync_predictions_by_sector_mapping'): 'ungated-upstream',
    ('src/services/fund_service.py', 'add_history'): 'dead-code',
    ('src/services/fund_service.py', 'add_fund_with_history'): 'ungated-upstream',
}


def _nav_date_write_sites():
    """AST 现量：构造 `FundHistory(...)` / `FundInfo(nav_date=…)` / 就地 `x.nav_date = …`。"""
    out = {}
    for path in sorted((pathlib.Path(ROOT) / 'src').rglob('*.py')):
        rel = 'src/' + path.as_posix().split('/src/', 1)[1]
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                hit = None
                if isinstance(n, ast.Call):
                    name = getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                    if name == 'FundHistory':
                        hit = '构造净值行'
                    elif name == 'FundInfo' and any(k.arg == 'nav_date' for k in n.keywords):
                        hit = '建档案即写档案头'
                elif isinstance(n, ast.Assign) and any(
                        getattr(t, 'attr', '') == 'nav_date' for t in n.targets):
                    hit = '就地改档案头'
                if hit:
                    out.setdefault((rel, fn.name), []).append(hit)
    return out


def test_the_nav_date_write_sites_are_the_registered_set(tmp_path):
    """第 49 轮 A 席(m-6)/B 席(m-3)：文档写"三个 `FundHistory` 写入点"，当场量到 **11 处 / 10 个函数**
    （漏的第 4 处是 `src/services/fund_service.py:238 add_history()`）。
    "只有 N 处"这句话没登记名单，就会多一处 —— 这一族本仓已经吃过好几次。
    """
    found = _nav_date_write_sites()
    assert set(found) == set(NAV_DATE_WRITE_SITES), (
        '净值日期的落笔点与登记名单不一致 ⇒ 要么新增了没人数的写点，'
        '要么删掉了没更新名单（多：%s／少：%s）'
        % (sorted(set(found) - set(NAV_DATE_WRITE_SITES)),
           sorted(set(NAV_DATE_WRITE_SITES) - set(found))))

    def self_gated(key):
        rel, fname = key
        tree = ast.parse((pathlib.Path(ROOT) / rel).read_text(encoding='utf-8'), filename=rel)
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == 'is_future_nav' for n in ast.walk(_func(tree, fname)))

    gated = {k for k in found if self_gated(k)}
    registered = {k for k, v in NAV_DATE_WRITE_SITES.items() if v == 'gated'}
    assert gated == registered, \
        '"哪几处自己过了门"与登记不符（今天量到 %d 处）：%s' % (len(gated), sorted(gated))


def test_a_new_unregistered_nav_date_write_is_caught(tmp_path):
    """空判对照：临时现造一个写档案头的函数 ⇒ 必须被 `_nav_date_write_sites` 收进来。

    上面那条"逐字相等"的判据如果只会读仓库现状，那它永远追不上新增 —— 这一格证明它有牙。
    """
    pkg = tmp_path / 'src' / 'probe_pkg'
    pkg.mkdir(parents=True)
    (tmp_path / 'src' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'probe.py').write_text(
        'class FundInfo:\n'
        '    def __init__(self, nav_date=None):\n        self.nav_date = nav_date\n\n\n'
        'def make(row):\n'
        '    return FundInfo(nav_date=row["nav_date"])\n', encoding='utf-8')
    found = {}
    for path in sorted((tmp_path / 'src').rglob('*.py')):
        rel = 'src/' + path.as_posix().split('/src/', 1)[1]
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'FundInfo' \
                        and any(k.arg == 'nav_date' for k in n.keywords):
                    found[(rel, fn.name)] = '建档案即写档案头'
    assert ('src/probe_pkg/probe.py', 'make') in found, \
        '现造一处"建档案就写档案头"却没被量到 ⇒ 那把尺子是空的'
