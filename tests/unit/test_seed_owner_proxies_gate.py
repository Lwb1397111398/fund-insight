"""`scripts/seed_owner_proxies.py` 的确认闸判据 —— 第 36 轮 B-MAJOR-3。

为什么这条值得钉：它写的是 `reviewed_by='owner' + owner_locked=True`（＝身份体检豁免），
而那是**全仓唯一一条不经页面 `owner_confirm` 的豁免来源**（豁免名单是代码字面量
`sector_fund_agent.DELIBERATE_PROXIES`）。第 20 轮 MAJOR-1 给它加了 `--owner-confirm SEED-PROXY`，
但到今天 `grep -rn seed_owner_proxies tests/` = **0** —— 加了参数不等于加了护栏，
护栏也没有样。这里钉三件事：
1. 不给口令就拒跑（退码 4），而且**在碰库之前**就拒；
2. `--dry-run` 一行都不写；
3. 只有 `--owner-confirm SEED-PROXY` 才写，且写的形态就是"老板署名 + 锁定"（所以它必须显式）。
"""
import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundInfo, SectorFundMapping

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'scripts'))


@pytest.fixture
def mod():
    return importlib.import_module('seed_owner_proxies')


def _db(tmp_path):
    engine = create_engine('sqlite:///%s' % (tmp_path / 'proxy.db').as_posix(),
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_it_refuses_to_run_without_the_token(mod, monkeypatch, capsys):
    """没 `--dry-run` 也没口令 ⇒ 退码 4；这一条跑在**任何库操作之前**（没有 SessionLocal 也不需要）。"""
    monkeypatch.setattr(sys, 'argv', ['seed_owner_proxies.py'])
    assert mod.main() == 4
    out = capsys.readouterr().out
    assert '[abort]' in out and 'SEED-PROXY' in out, out


def test_a_wrong_token_is_not_a_token(mod, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['seed_owner_proxies.py', '--owner-confirm', 'YES-I-AM-THE-BOSS'])
    assert mod.main() == 4
    assert '[abort]' in capsys.readouterr().out


def test_dry_run_writes_nothing(mod, monkeypatch, tmp_path, capsys):
    factory = _db(tmp_path)
    import src.models.database as dbmod
    import src.services.sector_fund_agent as agent
    from src.fund.fund_api import fund_api as instance

    monkeypatch.setattr(dbmod, 'SessionLocal', factory)
    monkeypatch.setattr(agent, 'DELIBERATE_PROXIES', {'测试代理板块': ('512480', '桩：无对口基金')})
    monkeypatch.setattr(instance, 'verify_fund_fetchable',
                        lambda code, name=None, **kw: {'api_name': '半导体ETF国泰', 'status': 'ok'})
    monkeypatch.setattr(sys, 'argv', ['seed_owner_proxies.py', '--dry-run'])
    assert mod.main() == 0
    assert '测试代理板块' in capsys.readouterr().out, 'dry-run 连计划都没报'
    db = factory()
    try:
        assert db.query(SectorFundMapping).count() == 0 and db.query(FundInfo).count() == 0
    finally:
        db.close()


def test_the_owner_stamp_only_lands_with_the_explicit_token(mod, monkeypatch, tmp_path, capsys):
    """给对口令 ⇒ 才写；写出来的就是"老板署名 + 体检锁定"，所以这条路径必须显式才可走。"""
    factory = _db(tmp_path)
    import src.models.database as dbmod
    import src.services.sector_fund_agent as agent
    from src.fund.fund_api import fund_api as instance

    monkeypatch.setattr(dbmod, 'SessionLocal', factory)
    monkeypatch.setattr(agent, 'DELIBERATE_PROXIES', {'测试代理板块': ('512480', '桩：无对口基金')})
    monkeypatch.setattr(instance, 'verify_fund_fetchable',
                        lambda code, name=None, **kw: {'api_name': '半导体ETF国泰', 'status': 'ok'})
    monkeypatch.setattr(sys, 'argv', ['seed_owner_proxies.py', '--owner-confirm', 'SEED-PROXY'])
    assert mod.main() == 0
    capsys.readouterr()

    db = factory()
    try:
        row = db.query(SectorFundMapping).filter_by(sector_name='测试代理板块').one()
        assert row.reviewed is True and row.reviewed_by == 'owner' and row.owner_locked is True
        assert row.match_source == 'manual' and row.match_kind == 'proxy'
        assert '老板确认的有意代理' in (row.verify_message or '')
        assert row.fund_name == '半导体ETF国泰', '官方名要现取，不许抄字面量'
        assert db.query(FundInfo).filter_by(fund_code='512480').first() is not None
    finally:
        db.close()


def test_the_gate_is_checked_before_the_database(mod):
    """顺序也是判据：先拒跑、再钉库。反过来的话，一次误碰就会先连上库再说不写。"""
    import inspect
    body = inspect.getsource(mod.main)
    abort_at = body.find("'[abort] 这个脚本会给行盖")
    pin_at = body.find('pin_local_sqlite')
    assert 0 <= abort_at < pin_at, '确认闸必须排在钉库之前（abort@%s pin@%s）' % (abort_at, pin_at)
