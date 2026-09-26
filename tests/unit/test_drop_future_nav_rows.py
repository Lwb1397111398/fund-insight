# -*- coding: utf-8 -*-
"""`scripts/drop_future_nav_rows.py` 的判据：默认不删、要确认词、要备份、只删晚于今天的。

配套的产品事实：入库侧的门在 `tests/unit/test_nav_future_row_gate.py`；
这里管的是"门补上之前已经躺在库里的那几行"该怎么清，以及**清错了能不能回滚**。
"""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TODAY = date(2026, 9, 25)


def _load():
    spec = importlib.util.spec_from_file_location(
        'drop_future_nav_rows', os.path.join(ROOT, 'scripts', 'drop_future_nav_rows.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    sys.modules['drop_future_nav_rows'] = mod
    spec.loader.exec_module(mod)
    return mod


def _db(tmp_path):
    from src.models.database import Base, FundHistory, FundInfo
    engine = create_engine('sqlite:///' + (tmp_path / 'copy.db').as_posix())
    Base.metadata.create_all(engine, tables=[FundInfo.__table__, FundHistory.__table__])
    session = sessionmaker(bind=engine)()
    session.add(FundInfo(fund_code='000725', fund_name='大成添利宝货币B',
                         nav_date=TODAY + timedelta(days=2), latest_nav=0.3442))
    for delta, nav in ((-1, 0.3440), (0, 0.3441), (1, 0.3442), (2, 0.3442)):
        session.add(FundHistory(fund_code='000725', fund_name='大成添利宝货币B',
                                nav_date=TODAY + timedelta(days=delta), nav=nav, day_growth=0.0))
    session.commit()
    return session, FundHistory


def test_dry_run_deletes_nothing_and_names_every_row(tmp_path, capsys):
    mod = _load()
    db, FundHistory = _db(tmp_path)
    try:
        rc, deleted = mod.drop(db, TODAY, False, None)
        assert rc == 2 and deleted == 0
        assert db.query(FundHistory).count() == 4, 'dry-run 删了行'
        out = capsys.readouterr().out
        assert '[计划] 要删的净值行 2 条' in out and '晚于 2026-09-25' in out, out
        assert out.count('[待删]') == 2, '逐行清单没印全（只印计数等于没清单）：%s' % out
        assert '[dry-run]' in out
    finally:
        db.close()


def test_real_delete_requires_a_backup_and_rewinds_the_archive_head(tmp_path, capsys):
    """`--apply` 少了 `--json` 就拒；给了就先落逐行备份，再删，再把档案头倒回真实末条。"""
    mod = _load()
    db, FundHistory = _db(tmp_path)
    try:
        rc, _ = mod.drop(db, TODAY, True, None)
        assert rc == 4, '没备份也肯删 ⇒ 不可回滚：%s' % capsys.readouterr().out[-300:]
        assert db.query(FundHistory).count() == 4

        backup = str(tmp_path / 'backup.json')
        rc, deleted = mod.drop(db, TODAY, True, backup)
        assert rc == 0 and deleted == 2
        assert sorted(str(r.nav_date) for r in db.query(FundHistory).all()) == \
            ['2026-09-24', '2026-09-25'], '该留的被删了，或该删的还在'
        from src.models.database import FundInfo
        info = db.query(FundInfo).first()
        assert info.nav_date == TODAY, '档案头还挂在未来日 %s' % info.nav_date
        assert abs(info.latest_nav - 0.3441) < 1e-9, '档案头最新值没跟着倒回：%s' % info.latest_nav
        payload = json.load(open(backup, encoding='utf-8'))
        assert len(payload['rows']) == 2 and payload['rows'][0]['fund_code'] == '000725'
        assert {r['nav_date'] for r in payload['rows']} == {'2026-09-26', '2026-09-27'}
    finally:
        db.close()


def test_the_confirm_gate_stands_before_the_connection():
    """确认词的检查必须排在连库之前（第 46 轮 B-M8 同条规矩）。"""
    import ast
    src = open(os.path.join(ROOT, 'scripts', 'drop_future_nav_rows.py'), encoding='utf-8').read()
    tree = ast.parse(src, filename='drop_future_nav_rows.py')
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'main')
    confirm = next((n.lineno for n in ast.walk(main) if isinstance(n, ast.Compare)
                    and 'CONFIRM_TOKEN' in ast.dump(n)), None)
    connect = next((n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == 'SessionLocal'), None)
    assert confirm and connect and confirm < connect, \
        '用法错也要先连一次库（confirm 行 %s / 连库行 %s）' % (confirm, connect)


def test_a_bad_flag_exits_before_touching_the_database(tmp_path):
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['LOCAL_DB_URL'] = str(tmp_path / 'never.db')
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, 'scripts', 'drop_future_nav_rows.py'), '--apply'],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=180)
    assert r.returncode == 4, '退码 %s：--apply 缺确认词必须拒跑（stdout=%s）' % (
        r.returncode, (r.stdout or '')[-300:])
    assert 'DROP-FUTURE-NAV' in (r.stdout or '')
    assert not (tmp_path / 'never.db').exists(), '拒跑之前就把库建出来了'


def test_a_named_date_joins_the_deletion_set_and_rewinds_the_head(tmp_path, capsys):
    """`--dates` 是点名删，不是扩大打击面：被点到的历史行一起进计划与备份。"""
    mod = _load()
    db, FundHistory = _db(tmp_path)
    try:
        ahead = (TODAY - timedelta(days=1),)      # 08-... 那种"日期已被追平的提前行"
        rc, _ = mod.drop(db, TODAY, False, None, extra_dates=ahead)
        out = capsys.readouterr().out
        assert rc == 2 and out.count('[待删]') == 3, '点名一条 + 未来两条都该进计划：%s' % out[-400:]
        backup = str(tmp_path / 'b2.json')
        rc, deleted = mod.drop(db, TODAY, True, backup, extra_dates=ahead)
        assert rc == 0 and deleted == 3
        assert [str(r.nav_date) for r in db.query(FundHistory).all()] == ['2026-09-25']
        from src.models.database import FundInfo
        assert db.query(FundInfo).first().nav_date == TODAY
    finally:
        db.close()


def test_naming_a_date_that_is_not_in_the_db_refuses(tmp_path):
    """点错了日期必须拒跑 —— 否则"名单写错"会静默变成"顺手删了别的"。"""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['LOCAL_DB_URL'] = str(tmp_path / 'empty_copy.db')
    from sqlalchemy import create_engine
    from src.models.database import Base, FundHistory, FundInfo
    engine = create_engine('sqlite:///' + (tmp_path / 'empty_copy.db').as_posix())
    Base.metadata.create_all(engine, tables=[FundInfo.__table__, FundHistory.__table__])
    engine.dispose()
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, 'scripts', 'drop_future_nav_rows.py'),
                        '--dates', '1999-01-01'],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=240)
    assert r.returncode == 4, '退码 %s / stdout=%s' % (r.returncode, (r.stdout or '')[-400:])
    assert '1999-01-01' in (r.stdout or ''), '没说清是哪一天点错了：%s' % (r.stdout or '')[-300:]
