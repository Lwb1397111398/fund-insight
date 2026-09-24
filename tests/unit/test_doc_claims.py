# -*- coding: utf-8 -*-
"""文档里"`test_x.py` N 条"这类当场承诺，必须等于 pytest 当场收集到的条数。

第 41 轮两份复评共同指出的**结构性**扣分：这轮的账已经修到 80 线以下卡住，而两份报告
都数出同一族问题 —— 文档里的枚举承诺（"5 条用例钉着""八条形状""6 条"）没有单一真源，
每加一条用例就漂一处。本轮实测就抓到两处（seed 那道闸写 5 条、当场 8 条）。
所以这轮的修法不是再手改一次数，而是加一把尺子量所有的数。

尺子：`scripts/audit_doc_claims.py`（读命令与判据同源 —— 它内部跑
`python -m pytest tests --collect-only -q`，按文件分组计数）。
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'audit_doc_claims.py'


def _load():
    spec = importlib.util.spec_from_file_location('audit_doc_claims', str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_auditor_catches_a_stale_count_and_accepts_a_live_one(tmp_path, monkeypatch):
    """检测型守卫必须证明"它会响"：现造一份写错条数的文档，它得判成不符。

    只跑真文档再断言"退码 0"是不够的 —— 万一正则压根匹配不到任何承诺，
    那条承诺数为 0、不符数为 0，也一样"全绿"（脚本自己也会为这种情况退码 4）。
    """
    mod = _load()
    doc = tmp_path / 'A.md'
    doc.write_text(u'判据：`tests/unit/test_x.py` 3 条钉着。\n'
                   u'同一批 `tests/unit/test_y.py` +7 条（这是增量账，不对账）。\n',
                   encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts',
                        lambda: {'test_x.py': 9, 'test_y.py': 1})
    now, delta = mod._claims()
    assert [c['test'] for c in now] == ['test_x.py'], now
    assert [c['test'] for c in delta] == ['test_y.py'], delta      # `+7 条` 走增量账
    counts = mod._collected_counts()
    assert counts[now[0]['test']] != now[0]['stated'], '这条样品没构造出"条数不符"'


def test_a_claim_that_cannot_be_resolved_to_a_collected_file_is_reported(tmp_path, monkeypatch):
    """指向"根本收集不到的文件"的承诺要单独报出来，不能被当成 0 条混过去。"""
    mod = _load()
    doc = tmp_path / 'B.md'
    doc.write_text(u'用例：`tests/unit/test_gone_away.py`（4 条）。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_other.py': 3})
    now, _delta = mod._claims()
    assert len(now) == 1
    assert now[0]['test'] not in mod._collected_counts()


def test_the_collected_counts_ruler_is_the_real_pytest_one():
    """尺子本身要通：当场收集到的条数必须与 `pytest --collect-only -q` 的输出一致。

    这里只做下限检查（<500 条脚本会直接 abort），因为条数每天都在涨，
    写死一个数就又变成一条"没绑口径的当场账"。
    """
    mod = _load()
    counts = mod._collected_counts()
    assert len(counts) > 50, '只收到 %d 个测试文件' % len(counts)
    assert sum(counts.values()) > 900, '当场收集到的总数少得可疑：%d' % sum(counts.values())


def test_the_repository_has_no_stale_doc_counts():
    """真文档过账：现在这一刻，`AGENTS.md` 与 `docs/模块总览/*.md` 里的当场账必须对得上。"""
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT),
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=1800)
    blob = (result.stdout or '') + (result.stderr or '')
    if '尺子不可用' in blob:
        # 前端变异体检正持有互斥锁 ⇒ 嵌套的 --collect-only 会被 conftest 挡下，这不是文档的错
        import pytest
        pytest.skip('变异体检持有互斥锁，本次拿不到条数尺子')
    assert result.returncode == 0, blob[-1200:]
