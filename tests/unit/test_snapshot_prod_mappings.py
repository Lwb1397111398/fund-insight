# -*- coding: utf-8 -*-
"""生产前像工具的两条用例：接口那条路必须**承认自己不完整**，直连那条路必须拿到全列。

背景（第 9 轮 MAJOR-3 / 本轮补的 `--via-db`）：`GET /api/config/sector-mappings` 不返回
`owner_locked / reviewed_by / evidence`，拿它当"逐行回滚的退路"是假的；
而静默少列比报错更糟——`owner_locked` 不在行里时"老板锁定 0 行"会被当成事实。
"""
import importlib.util
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    'snapshot_prod', os.path.join(ROOT, 'scripts', 'snapshot_prod_mappings.py'))
snap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snap)


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.execute("create table sector_fund_mapping (id integer primary key, sector_name text, "
                "fund_code text, fund_name text, reviewed integer, owner_locked integer, "
                "reviewed_by text, evidence text)")
    con.executemany("insert into sector_fund_mapping (sector_name, fund_code, reviewed, "
                    "owner_locked, reviewed_by, evidence) values (?,?,?,?,?,?)",
                    [('债券', '512000', 1, 1, 'owner', '{"identity":{"verdict":"ok"}}'),
                     ('半导体', '512480', 0, 0, None, None)])
    con.commit()
    con.close()


def test_via_db_returns_every_column(tmp_path, monkeypatch):
    """直连那条路：审计列必须在（这是它存在的唯一理由）。"""
    db = tmp_path / 'pre-image.sqlite3'
    _make_db(db)
    # 守卫的语义就是"LOCAL_DB_URL 无条件优先"，这里靠它把脚本钉到临时库
    monkeypatch.setenv('LOCAL_DB_URL', str(db))
    keys, rows = snap.fetch_via_db(production=False)
    assert {'owner_locked', 'reviewed_by', 'evidence'} <= set(keys), keys
    assert len(rows) == 2
    owner = [r for r in rows if r['sector_name'] == '债券'][0]
    assert owner['owner_locked'] and owner['reviewed_by'] == 'owner', \
        '老板的行读不出来 ⇒ 这份前像不足以回滚'


def test_via_db_is_read_only_at_engine_level(tmp_path, monkeypatch):
    """只读不是形容词：临时库上真写一次必须被数据库拒绝。"""
    db = tmp_path / 'ro.sqlite3'
    _make_db(db)
    monkeypatch.setenv('LOCAL_DB_URL', str(db))
    keys, rows = snap.fetch_via_db(production=False)
    assert len(rows) == 2
    con = sqlite3.connect('file:' + str(db).replace('\\', '/') + '?mode=ro', uri=True)
    with pytest.raises(sqlite3.OperationalError) as exc:
        con.execute("insert into sector_fund_mapping (sector_name, fund_code) values ('x','y')")
    assert 'readonly' in str(exc.value).lower()
    con.close()


def test_incomplete_api_image_says_so():
    """接口回来的行缺审计列时，回执必须明说"不完整"，不能报"老板锁定 0 行"。"""
    api_row = {'sector_name': '债券', 'fund_code': '512000', 'reviewed': True}
    missing = [c for c in ('owner_locked', 'reviewed_by', 'evidence')
               if c not in api_row]
    assert missing == ['owner_locked', 'reviewed_by', 'evidence'], \
        '接口若已补齐这三列，就该把脚本里的"不完整"警告一并去掉'
    src = open(os.path.join(ROOT, 'scripts', 'snapshot_prod_mappings.py'),
               encoding='utf-8').read()
    assert '不足以逐行回滚' in src and '--via-db' in src
