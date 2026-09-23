"""`POST /api/config/sector-mappings/seed` 与 `scripts/seed_sector_mappings.py` 的诚实性判据。

第 36 轮 B-MAJOR-1 抓到这条路的三件事，今天之前**全仓零用例**
（`grep -rn seed_sector_mappings tests/` = 0）：
1. 路由起子进程却不查退码就回 `success:true "预置数据导入完成"`；而且它把 cwd 少退一层
   （退到 `src/`），脚本在那儿**根本不存在** ⇒ 子进程必定以退码 2 失败 ⇒ 这条路由在旧代码上
   **每次都在说谎**（`test_the_route_actually_points_at_a_script_that_exists` 钉住这条）；
2. 没有总开关、没有确认头 —— 一条 POST 就能动映射表；
3. 脚本无条件 `reviewed=True` 并就地改已有行的 `fund_code`（绕开 `retag_prediction`）。
   镜像上量过一次：`SEED-RECEIPT: dry-run 新增=126 已存在跳过=35 其中码不一致=21 老板行=0 缺档案=2 内置计划=163`
   （2026-09-23 17:46，`python scripts/seed_sector_mappings.py --dry-run`）。
   现在语义是**只补缺、不覆盖**，且新行一律 `reviewed=False` + `match_source='seed_builtin'`。
"""
import importlib
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.routes.config import seed_sector_mappings
from src.models.database import Base, SectorFundMapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 仓库根
REPO_ROOT = ROOT
SCRIPT = os.path.join(REPO_ROOT, 'scripts', 'seed_sector_mappings.py')


class _Request:
    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.fixture
def _open_switch(monkeypatch):
    monkeypatch.setenv('ENABLE_SECTOR_SEED_IMPORT', 'true')


@pytest.fixture
def _no_subprocess(monkeypatch):
    """任何一条用例都不该真起子进程（真跑脚本的用例自己显式跑）。"""
    def boom(*a, **kw):
        raise AssertionError('这条用例不该真跑子进程：%r' % (a,))
    monkeypatch.setattr(subprocess, 'run', boom)


def test_the_seed_route_is_off_until_the_server_opens_it(monkeypatch, _no_subprocess):
    """默认关：与 `ENABLE_SECTOR_AUDIT_IMPORT` 同一套仓库惯例。"""
    monkeypatch.delenv('ENABLE_SECTOR_SEED_IMPORT', raising=False)
    res = seed_sector_mappings(request=_Request(), dry_run=True, db=None)
    assert res['success'] is False
    assert 'ENABLE_SECTOR_SEED_IMPORT' in res['message']


RECEIPT = ('SEED-RECEIPT: 真写 新增=7 已存在跳过=35 其中码不一致=21 老板行=4 缺档案=2 内置计划=163')


def _stub_run(monkeypatch, stdout=RECEIPT, returncode=0):
    """替掉子进程：这些用例测的是路由怎么**解释**结果，不是脚本本身（脚本另有两条用例真跑）。"""
    class Done:
        pass
    d = Done()
    d.returncode, d.stdout, d.stderr = returncode, stdout, ''
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return d

    monkeypatch.setattr(subprocess, 'run', fake)
    return calls


def test_a_real_write_needs_the_danger_header_but_a_dry_run_does_not(_open_switch, monkeypatch):
    """写要确认头、看计划不要 —— 顺序也不能反：先拒写，再谈 dry-run。"""
    _stub_run(monkeypatch)
    res = seed_sector_mappings(request=_Request(), db=None)
    assert res['success'] is False and 'X-Danger-Confirm' in res['message']

    dry = seed_sector_mappings(request=_Request(), dry_run=True, db=None)
    assert 'X-Danger-Confirm' not in dry.get('message', '')


def test_a_nonzero_exit_is_never_reported_as_a_success(_open_switch, monkeypatch):
    """旧版就是死在这条上：退码 2（文件不存在）也回"预置数据导入完成"。"""
    class Done:
        returncode = 2
        stdout = "python: can't open file 'scripts/seed_sector_mappings.py'"
        stderr = ''
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: Done())
    res = seed_sector_mappings(request=_Request(
        {'X-Danger-Confirm': 'seed-sector-mappings'}), db=None)
    assert res['success'] is False, '脚本失败了却报成功 ⇒ 又是一句假话'
    assert '退码 2' in res['message']
    assert res['data']['returncode'] == 2


def test_zero_exit_without_a_receipt_is_still_a_failure(_open_switch, monkeypatch):
    """退码 0 但没打 `SEED-RECEIPT` ⇒ 它写了什么我们无从证明，按失败处理。"""
    class Done:
        returncode = 0
        stdout = '完成: 新增 128, 更新 0, 跳过 35'      # 旧格式，没有回执行
        stderr = ''
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: Done())
    res = seed_sector_mappings(request=_Request(
        {'X-Danger-Confirm': 'seed-sector-mappings'}), db=None)
    assert res['success'] is False
    assert '回执' in res['message']


def test_the_route_actually_points_at_a_script_that_exists(_open_switch, monkeypatch):
    """cwd 少退一层 ⇒ 脚本永远找不到。这条用例盯的就是那个"一层"。"""
    seen = {}

    class Done:
        returncode = 0
        stdout = 'SEED-RECEIPT: 真写 新增=0 已存在跳过=35 其中码不一致=21 老板行=0 内置计划=163'
        stderr = ''

    def fake(cmd, **kw):
        seen['cmd'] = cmd
        seen['cwd'] = kw.get('cwd')
        return Done()

    monkeypatch.setattr(subprocess, 'run', fake)
    res = seed_sector_mappings(request=_Request(
        {'X-Danger-Confirm': 'seed-sector-mappings'}), db=None)
    assert res['success'] is True
    target = os.path.join(seen['cwd'], cmd_tail(seen['cmd']))
    assert os.path.exists(target), '路由要跑的文件不存在：%s' % target


def cmd_tail(cmd):
    """脚本参数里可能夹着 `--confirm SEED-MAP`，取相对路径那一段。"""
    return next(part for part in cmd[1:] if part.startswith('scripts' + os.sep)
                or part.replace('\\', '/').startswith('scripts/'))


def test_true_success_carries_the_receipt_and_refreshes_the_cache(_open_switch, monkeypatch):
    """成功后：回执数字要原样给出去，并且**真插了行就得刷缓存**（写完不刷＝自己读的映射自己看不到）。"""
    _stub_run(monkeypatch)
    refreshed = []

    class _Svc:
        def refresh_cache(self):
            refreshed.append(1)

    import src.services.sector_fund_service as svc
    monkeypatch.setattr(svc, 'get_sector_fund_service', lambda db: _Svc())

    res = seed_sector_mappings(request=_Request(
        {'X-Danger-Confirm': 'seed-sector-mappings'}), db=None)
    assert res['success'] is True
    assert res['data'] == {'returncode': 0, 'cache_refreshed': True, 'receipt': RECEIPT,
                           'added': 7, 'existing_skipped': 35, 'code_mismatch': 21,
                           'owner_rows': 4, 'no_fund_info': 2, 'planned_rows': 163}
    assert '另有 2 行因本库没有 fund_info 档案被跳过' in res['message'], \
        '缺档案的行数没进人话 message（第 38 轮：调用方只看 message 就会以为"补了 7 行、万事大吉"）'
    assert refreshed == [1]


def test_the_parsed_receipt_covers_every_column_the_script_prints():
    """两端必须咬在**同一份格式**上：拿真脚本真输出喂真解析器。

    第 38 轮两份复评共同抓到：`dd125cb` 给回执行加了 `缺档案=` / `内置计划=`，
    而 `_parse_seed_receipt` 只认四个老键 ⇒ 新列被静默丢掉、10 条用例全绿。
    手抄底本永远会漂，所以这条不抄字符串，直接跑脚本（dry-run，钉镜像、不写库）。
    """
    import re
    from src.api.routes.config import _parse_seed_receipt

    res = subprocess.run([sys.executable, SCRIPT, '--dry-run'], capture_output=True,
                         text=True, encoding='utf-8', errors='replace',
                         cwd=REPO_ROOT, env=_child_env())
    assert res.returncode == 0, res.stdout[-400:] + res.stderr[-400:]
    line = next((l for l in res.stdout.splitlines() if l.startswith('SEED-RECEIPT:')), '')
    assert line, '脚本这次没打回执，本用例无从判定（先修脚本的回执，再谈解析）'
    printed = dict(re.findall(r'(\w+)=(-?\d+)', line))
    parsed = _parse_seed_receipt(res.stdout)
    EN = {'新增': 'added', '已存在跳过': 'existing_skipped', '其中码不一致': 'code_mismatch',
          '老板行': 'owner_rows', '缺档案': 'no_fund_info', '内置计划': 'planned_rows'}
    missing = [c for c in printed if c not in parsed and EN.get(c, c) not in parsed]
    assert not missing, ('解析器丢了脚本打的列：%s（两端各测各的 ⇒ 契约已在漂移）' % sorted(missing))
    for col, val in printed.items():
        key = col if col in parsed else EN[col]
        assert parsed[key] == int(val), '%s 解析成 %s，脚本打的是 %s' % (key, parsed[key], val)
    # 真正的防漂移：脚本**以后**再加列，也必须原样带出去，而不是只剩 `data.receipt` 那串原文
    synth = _parse_seed_receipt('SEED-RECEIPT: 真写 新增=1 已存在跳过=0 将来新加的列=9')
    assert synth.get('将来新加的列') == 9, '未知列又被静默丢掉了（%s）' % synth


def test_a_dry_run_does_not_refresh_the_cache(_open_switch, monkeypatch):
    _stub_run(monkeypatch, stdout='SEED-RECEIPT: dry-run 新增=126 已存在跳过=35 其中码不一致=21 老板行=0 缺档案=2 内置计划=163')
    import src.services.sector_fund_service as svc

    def boom(db):
        raise AssertionError('dry-run 不该刷缓存')

    monkeypatch.setattr(svc, 'get_sector_fund_service', boom)
    res = seed_sector_mappings(request=_Request(), dry_run=True, db=None)
    assert res['success'] is True and res['data']['cache_refreshed'] is False
    assert 'dry-run' in res['message']


def _child_env(tmp_path=None):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    if tmp_path is not None:
        env['LOCAL_DB_URL'] = str(tmp_path / 'seed-probe.db')
    return env


def test_the_script_refuses_to_write_without_the_token():
    """`--confirm` 缺失 ⇒ 退码 4 并且一行都不写（工具自己说得清）。"""
    res = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', cwd=REPO_ROOT, env=_child_env())
    assert res.returncode == 4
    assert '[abort]' in res.stdout and 'SEED-MAP' in res.stdout


def _db(tmp_path):
    path = str((tmp_path / 'seed.db')).replace('\\', '/')
    engine = create_engine('sqlite:///%s' % path)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _seed_module():
    return importlib.import_module('seed_sector_mappings')


def test_the_script_creates_missing_rows_without_touching_existing_ones(tmp_path, capsys):
    """语义核心：已存在的行**一律不动**（尤其老板盖过章的），新行不带 `reviewed=True`。"""
    sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))
    mod = _seed_module()
    db = _db(tmp_path)
    try:
        # 脚本现在会先看"本库有没有这只基金的档案"（`fund_code` 有外键），
        # 没档案就跳过而不是让整批回滚 ⇒ 用例得把计划里的码先补成档案，否则一行都不会插。
        from src.constants.sector_fund_map import SECTOR_FUND_MAP
        from src.models.database import FundInfo
        codes = {i.get('code', ''): i.get('name', '') for i in SECTOR_FUND_MAP.values()}
        codes.update({c: n for _s, c, n in mod.EXTRA_MAPPINGS})
        for code, name in codes.items():          # 去重：同一个码在两张表里都出现
            if code:
                db.add(FundInfo(fund_code=code, fund_name=name or code))
        db.commit()
        db.add(SectorFundMapping(sector_name='半导体', fund_code='512480', fund_name='芯片ETF国泰',
                                 reviewed=True, reviewed_by='owner', owner_locked=True))
        db.add(SectorFundMapping(sector_name='医疗', fund_code='159877', fund_name='现有标的',
                                 reviewed=True, match_source='agent'))
        db.commit()

        assert mod.seed_mappings(dry_run=False, confirm='SEED-MAP', db=db) == 0
        capsys.readouterr()

        kept_owner = db.query(SectorFundMapping).filter_by(sector_name='半导体').one()
        assert kept_owner.fund_code == '512480' and kept_owner.owner_locked and kept_owner.reviewed_by == 'owner'
        kept_agent = db.query(SectorFundMapping).filter_by(sector_name='医疗').one()
        assert kept_agent.fund_code == '159877', '内置表把已有行的标的改回去了 ⇒ 绕开 retag_prediction'

        new = db.query(SectorFundMapping).filter_by(match_source='seed_builtin').all()
        assert new, '没补进任何缺失行 ⇒ 这个脚本就没意义了'
        assert all(not r.reviewed for r in new), '脚本不许自己给行盖"已审查"'
    finally:
        db.close()


def test_dry_run_writes_nothing_even_with_the_token(tmp_path, capsys):
    sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))
    mod = _seed_module()
    db = _db(tmp_path)
    try:
        rc = mod.seed_mappings(dry_run=True, db=db)
        out = capsys.readouterr().out
        assert rc == 0
        assert 'SEED-RECEIPT: dry-run' in out
        assert db.query(SectorFundMapping).count() == 0, 'dry-run 写了库'
    finally:
        db.close()
