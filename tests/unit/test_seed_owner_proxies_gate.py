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
import ast
import inspect
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundInfo, SectorFundMapping

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'scripts'))


def _real_probe_keys():
    """从**真函数**里读出 `verify_fund_fetchable` 返回的键名集合。

    第 38 轮 A 席 MAJOR-2 的同类病：我另一条用例的桩自己发明了 `is_fetchable` / `status`
    两个键（真接口返回的是 `ok` / `api_name` / `message` / `kind` / `history_count`…），
    而路由是纯 pass-through ⇒ "桩给什么、断言收到什么"＝同义反复，结构上不可能红。
    所以这里让**被告自己供证**：桩用的键必须是真返回值里有的键。
    """
    src = (REPO_ROOT / 'src' / 'fund' / 'fund_api.py').read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == 'verify_fund_fetchable':
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    return {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError('找不到 verify_fund_fetchable 的返回字典 ⇒ 它换了形状，桩必须先跟着换')


def _probe_stub(ok=True, code='512480', api_name='半导体ETF国泰'):
    payload = {
        'ok': ok, 'is_strict_ok': ok, 'kind': 'fund' if ok else 'unknown',
        'fund_type': None, 'official_name': api_name, 'code': code, 'input_name': None,
        'api_name': api_name, 'api_nav': 1.05 if ok else None,
        'nav_date': '2026-09-23' if ok else None, 'history_count': 30 if ok else 0,
        'message': '验证通过：%s' % api_name if ok else '验证失败：接口无有效数据，该基金可能已停牌/清盘或代码有误',
    }
    invented = sorted(set(payload) - _real_probe_keys())
    assert not invented, '桩发明了真接口没有的键：%s（⇒ 这条用例在测一个不存在的契约）' % invented
    return payload


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
                        lambda code, name=None, **kw: _probe_stub(code=code))
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
                        lambda code, name=None, **kw: _probe_stub(code=code))
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


def test_a_probe_that_says_not_fetchable_writes_nothing(mod, monkeypatch, tmp_path, capsys):
    """探针说"查无此码" ⇒ 不盖 owner 豁免、不写 `is_fetchable=True`、不造幻影 `fund_info` 行。

    第 38 轮 A 席 MAJOR-3：`main()` 调探针**只为取名字**，从不读 `ok`，于是一个被数据源
    下架的代理板块会带着"抓站验证过＝可服务"的章继续给新帖挑标的，还顺手在 `fund_info`
    里造出一行空名档案 —— 正是 S6 要清的垃圾码形状。
    """
    factory = _db(tmp_path)
    import src.models.database as dbmod
    import src.services.sector_fund_agent as agent
    from src.fund.fund_api import fund_api as instance

    monkeypatch.setattr(dbmod, 'SessionLocal', factory)
    monkeypatch.setattr(agent, 'DELIBERATE_PROXIES', {'测试代理板块': ('999999', '桩：码已下架')})
    monkeypatch.setattr(instance, 'verify_fund_fetchable',
                        lambda code, name=None, **kw: _probe_stub(ok=False, code=code, api_name=''))
    monkeypatch.setattr(sys, 'argv', ['seed_owner_proxies.py', '--owner-confirm', 'SEED-PROXY'])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 5, '探针说取不到却仍按成功收场（退码 %s）：%s' % (rc, out)
    assert '[跳过' in out and '[回执]' in out, out

    db = factory()
    try:
        assert db.query(SectorFundMapping).count() == 0, '没验证过的码拿到了 owner 豁免'
        assert db.query(FundInfo).count() == 0, '造出了空名幻影档案行（S6 要清的就是这种）'
    finally:
        db.close()


def test_the_stub_contract_is_the_real_probe_contract():
    """把"桩不许发明键"这件事本身钉住：真接口今天返回 `ok`，**没有** `is_fetchable` / `status`。

    这条同时是一份备忘：哪天真接口加了这两个键，它会红，逼着人来重写第 38 轮那条更正，
    而不是留下一个"看起来在测契约、其实在测桩"的用例。
    """
    keys = _real_probe_keys()
    assert {'ok', 'api_name', 'message', 'kind', 'history_count'} <= keys, sorted(keys)
    assert 'is_fetchable' not in keys and 'status' not in keys, \
        '真探针现在有 is_fetchable/status 了 ⇒ 页面读的 `d.ok` 与本文件的桩都要重核'
    for ok in (True, False):
        _probe_stub(ok=ok)      # 构造时就校验，发明键会当场 AssertionError
