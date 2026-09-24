# -*- coding: utf-8 -*-
"""`scripts/drop_probe_residue.py` 的判据（第 43 轮 B-MAJOR-6）。

B 席在真镜像里查出 `_db_guard_probe2(x int)` —— 第 41 轮 B-MAJOR-2 说"探针不再留残渣"
**之后**修的是新代码，旧残渣没人扫、也没人清。而 `sync_db_columns.py --stamp-head`
在"库里有、模型没声明的对象"非空时拒绝记版本号 ⇒ 一个空壳表能卡住一次结构对齐。

每条都跑**子进程 + 临时 sqlite**：这个工具会 `pin_local_sqlite()`（写 `os.environ`），
在测试进程里做那件事 = 第 33 轮 `tests/conftest.py` 那次生产误连的同款事故。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = str(ROOT / 'scripts' / 'drop_probe_residue.py')
FAKE_PROD = 'postgresql://someone:SECRETPASS@db.invalid.example/never'


def _make_db(path, tables):
    """tables: {表名: 行数}。"""
    conn = sqlite3.connect(str(path))
    for name, nrows in tables.items():
        conn.execute('CREATE TABLE "%s" (x int)' % name)
        for i in range(nrows):
            conn.execute('INSERT INTO "%s" VALUES (%d)' % (name, i))
    conn.execute('CREATE TABLE bloggers (id integer primary key, name text)')
    conn.commit()
    conn.close()


def _tables(path):
    conn = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'"))
    finally:
        conn.close()


def _run(path, *args):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['DATABASE_URL'] = FAKE_PROD          # 判的就是"它会不会被 .env/环境里的生产串带走"
    env['LOCAL_DB_URL'] = ''
    return subprocess.run([sys.executable, SCRIPT, '--db', str(path)] + list(args),
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=300)


def test_report_mode_finds_residue_and_deletes_nothing(tmp_path):
    db = tmp_path / 'mirror.db'
    _make_db(db, {'_db_guard_probe': 0, 'fund_info': 3})
    before = _tables(db)
    out = _run(db)
    assert out.returncode == 3, out.stdout + out.stderr       # 有残渣 ⇒ 非零，但不删
    assert '_db_guard_probe' in out.stdout, out.stdout
    assert all('fund_info' not in l for l in out.stdout.splitlines() if '可删' in l), \
        '正常表被点名成"可删残渣"：%s' % out.stdout
    assert _tables(db) == before, '报告模式改了库'
    assert 'SECRETPASS' not in out.stdout + out.stderr


def test_apply_without_the_confirm_token_deletes_nothing(tmp_path):
    db = tmp_path / 'mirror.db'
    _make_db(db, {'_db_guard_probe2': 0})
    before = _tables(db)
    out = _run(db, '--apply')
    assert out.returncode == 4, out.stdout
    assert 'DROP-PROBE' in out.stdout, out.stdout
    assert _tables(db) == before, '没给口令就删了'


def test_apply_removes_only_empty_unclaimed_probe_tables(tmp_path):
    db = tmp_path / 'mirror.db'
    _make_db(db, {'_db_guard_probe2': 0, '_db_guard_probe3': 4, 'system_config': 1})
    out = _run(db, '--apply', '--confirm', 'DROP-PROBE')
    assert out.returncode == 4, \
        '有一张探针名字的表里还有 4 行，整批必须不删（退码 %s）：%s' % (out.returncode, out.stdout)
    assert '拒绝' in out.stdout and '还有行' in out.stdout, out.stdout
    assert '_db_guard_probe3' in [t for t in _tables(db)], '该留的没留'
    assert '_db_guard_probe2' in _tables(db), '整批不删意味着残渣也还在'

    # 把那张有行的改名成正常表 ⇒ 剩下的残渣才允许被删，且 `system_config` 一个字都不动
    conn = sqlite3.connect(str(db))
    conn.execute('ALTER TABLE "_db_guard_probe3" RENAME TO real_data')
    conn.commit()
    conn.close()
    keep = 'system_config'
    out2 = _run(db, '--apply', '--confirm', 'DROP-PROBE')
    assert out2.returncode == 0, out2.stdout + out2.stderr
    left = _tables(db)
    assert '_db_guard_probe2' not in left, left
    assert keep in left and 'real_data' in left, left
    assert json.dumps(left).count('probe') == 0


def test_a_model_claimed_table_is_never_treated_as_residue(tmp_path, monkeypatch):
    """名字恰好像残渣、但模型声明了它 ⇒ 拒绝。这条判据不许被"名字匹配"绕过。"""
    scripts_dir = str(ROOT / 'scripts')
    sys.path.insert(0, scripts_dir)
    try:
        import importlib.util
        s = importlib.util.spec_from_file_location('dpr', SCRIPT)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
    finally:
        sys.path.remove(scripts_dir)
    db = tmp_path / 'one.db'
    _make_db(db, {'_db_guard_probe': 0})
    conn = sqlite3.connect(str(db))
    c = conn.cursor()
    rows = [(r,) for r in _tables(db)]
    conn.close()
    found = m.find_residue(_ConnStub(c, rows), {'_db_guard_probe'})
    assert found and found[0]['safe'] is False, found
    assert '模型里声明' in found[0]['why'], found


class _ConnStub:
    """给 `find_residue` 用的最小连接：只需要按顺序回答两条查询。"""

    def __init__(self, cursor, table_rows):
        self.cursor = cursor
        self.table_rows = table_rows
        self.calls = 0

    def execute(self, _stmt):
        self.calls += 1
        rows = self.table_rows if self.calls == 1 else [(0,)]

        class R:
            def __init__(self, rows):
                self._r = rows

            def fetchall(self):
                return self._r

            def scalar(self):
                return self._r[0][0]
        return R(rows)
