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

import pytest    # `_collected_counts_or_skip` 要用它把"尺子拿不到"变成 skip（第 46 轮 B-M7）

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
    now, delta, unbound = mod._claims()
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
    now, _delta, _unbound = mod._claims()
    assert len(now) == 1
    assert now[0]['test'] not in mod._collected_counts()


def _collected_counts_or_skip(mod):
    """拿条数尺子；**拿不到就 skip，不许 fail**（第 46 轮 B-M7）。

    前端变异体检持有 `.mutation-harness.lock` 时，脚本内部那次嵌套
    `pytest --collect-only` 会被 `tests/conftest.py` 挡下 ⇒ `_collected_counts` 直接
    `SystemExit('[abort] pytest 只收集到 0 条 ⇒ 尺子本身不可用')`。那是"这台机器此刻
    测不了"，不是"文档在说谎"。同文件另一条（真文档过账）本来就认了这一档，
    而 in-process 这条没认 ⇒ 同一状况一条 skip、一条 fail，
    下一轮就会有人去改代码而不是等锁放下来（B 席实测：`pytest tests/unit -q` 1 failed /
    单跑本文件 13 passed）。
    """
    try:
        return mod._collected_counts()
    except SystemExit as exc:
        pytest.skip('条数尺子此刻拿不到（多半是前端变异体检持有互斥锁）⇒ 不是文档的问题：%s'
                    % exc)


def test_the_collected_counts_ruler_is_the_real_pytest_one():
    """尺子本身要通：当场收集到的条数必须与 `pytest --collect-only -q` 的输出一致。

    这里只做下限检查（<500 条脚本会直接 abort），因为条数每天都在涨，
    写死一个数就又变成一条"没绑口径的当场账"。
    """
    mod = _load()
    counts = _collected_counts_or_skip(mod)
    assert len(counts) > 50, '只收到 %d 个测试文件' % len(counts)
    assert sum(counts.values()) > 900, '当场收集到的总数少得可疑：%d' % sum(counts.values())


def test_an_unavailable_ruler_skips_instead_of_going_red(monkeypatch, capsys):
    """控制断言：`_collected_counts_or_skip` 必须**真的**把"尺子不可用"变成 skip。

    没有这一条，上面那个 helper 可以写成"照样 raise"，而全套件只在体检并发的日子才红 ——
    那种红每次都要重新查一遍根因，正是 B 席这轮记下来的成本。
    """
    mod = _load()

    def _blind():
        raise SystemExit('[abort] pytest 只收集到 0 条 ⇒ 尺子本身不可用，不做对账')

    monkeypatch.setattr(mod, '_collected_counts', _blind)
    try:
        _collected_counts_or_skip(mod)
    except pytest.skip.Exception as exc:
        assert '尺子' in str(exc) or '互斥锁' in str(exc), exc
    else:
        assert False, '尺子不可用时没有 skip ⇒ 并发那天整条基线会被读成"代码坏了"'


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


def test_chinese_numerals_are_audited_too(tmp_path, monkeypatch):
    """中文数字必须同样对账（第 41 轮 B-MINOR-4：只认 ASCII ⇒ "三条钉着"这类看不见）。

    这一条同时是**尺子自己的**覆盖面判据：把 `_to_int` 退回 `int()`，中文那条就漏收 ⇒ 红。
    """
    mod = _load()
    doc = tmp_path / 'C.md'
    doc.write_text(u'判据：`tests/unit/test_a.py` 三条钉着。\n'
                   u'另一处：`tests/unit/test_b.py` 9 条钉着。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_a.py': 7, 'test_b.py': 9})
    now, _delta, _unbound = mod._claims()
    assert sorted(c['test'] for c in now) == ['test_a.py', 'test_b.py'], now
    assert {c['test']: c['stated'] for c in now} == {'test_a.py': 3, 'test_b.py': 9}, now


def test_the_two_shapes_it_cannot_judge_are_reported_but_never_failed(tmp_path, monkeypatch):
    """管不到的两种形状要**打出来**，但不许判红（误报的尺子会逼人把对的数改错）。"""
    mod = _load()
    doc = tmp_path / 'D.md'
    doc.write_text(u'当场照出 14 条漏桩用例（`tests/unit/test_far.py` 整个文件的补拉腿）。\n'
                   u'五条硬规矩：① 甲；② 乙；③ 丙；④ 丁；⑤ 戊。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_far.py': 3})
    now, _delta, unbound = mod._claims()
    assert now == [], '数字在名字之前的历史账被判成当场账了（会误报）：%s' % now
    assert len(unbound) == 2, unbound


def _sources_of(tmp_path, monkeypatch, text):
    mod = _load()
    doc = tmp_path / 'R.md'
    doc.write_text(text, encoding='utf-8')
    monkeypatch.setattr(mod, '_report_docs', lambda: [doc])
    return mod._source_lines()


def test_a_data_source_line_that_only_names_a_variable_is_red(tmp_path, monkeypatch):
    """`数据源：`DATABASE_URL`` 不是出处，是**没出处**（第 42 轮 B-MINOR-6）。

    这一族不是"文档不好看"：第 23 轮我把**镜像库**的准确率当"系统的数"报了十几轮，
    而当时那行字如果写的是机器名，第一眼就能看出来连的是哪个库。
    """
    bad, seen = _sources_of(tmp_path, monkeypatch,
                            u'# 报告\n\n- 日期：2026-07-29\n- 数据源：`DATABASE_URL`\n')
    assert seen == 1, '一行 `数据源：` 都没认出来 ⇒ 这条判据是空判'
    assert len(bad) == 1 and '变量名' in bad[0]['why'], bad


def test_a_data_source_line_must_name_a_target_or_admit_it_was_never_recorded(tmp_path, monkeypatch):
    """放行两种：具体目标；或明写"未记录"**并且**给复现命令。只说"未记录"不算。"""
    good, _ = _sources_of(tmp_path, monkeypatch,
                          u'- 数据源：`本地镜像库（data/fund_insight.db）`\n'
                          u'- 数据源：⚠ **未记录**（旧脚本只写变量名）。'
                          u'复现：`python scripts/backtest_l1_weighting.py`\n')
    assert good == [], good
    vague, seen = _sources_of(tmp_path, monkeypatch, u'- 数据源：未记录\n')
    assert seen == 1 and len(vague) == 1, '承认"未记录"却不给复现命令 ⇒ 不该放行'


def test_a_claim_wrapped_onto_the_next_line_is_still_a_claim(tmp_path, monkeypatch, capsys):
    """文件名落在行末、数落在行首的软换行排版，逐行扫的旧写法读不到（第 45 轮 A-m6）。

    现场：`docs/模块总览/前端与接口层.md` 那句"（16 条判据、35 处变异逐条打红）"就写在
    `test_frontend_cold_start.py` 的**下一行**开头，而当场其实是 42 条 ⇒ 没有任何判据读得到它。
    两头都要钉：① 认得出，且认出来之后 `--fix` 改写的是**数所在那一行**（段内偏移换算错了
    就会改到别的字节上）；② 反向对照：上一句已经用 `。` 收口、下一行另起一句 ⇒ 两句不许
    被拼成一条承诺（误报的尺子会逼人把对的数改错）。
    """
    mod = _load()
    doc = tmp_path / 'W.md'
    doc.write_text(u'判据：`tests/unit/test_wrap.py`\n（9 条判据逐条打红）。\n'
                   u'当场照出 14 条漏桩用例（`tests/unit/test_far.py` 整个文件的补拉腿）。\n'
                   u'五条硬规矩：① 甲；② 乙；③ 丙；④ 丁；⑤ 戊。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts',
                        lambda: {'test_wrap.py': 42, 'test_far.py': 3})
    now, _delta, _unbound = mod._claims()
    assert [(c['test'], c['stated'], c['line']) for c in now] == [('test_wrap.py', 9, 2)], now

    # 控制：把"数"搬回同一行，旧写法本来就该认得 —— 排除"新形状靠的是别的东西"
    same = tmp_path / 'S.md'
    same.write_text(u'判据：`tests/unit/test_wrap.py` 9 条判据。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [same])
    assert [(c['test'], c['stated'], c['line']) for c in mod._claims()[0]] == [
        ('test_wrap.py', 9, 1)], '同一行的形状反而不认得了 ⇒ 跨行改动把老形状弄坏了'

    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])       # 指回跨行那份，别改到对照组样品上
    monkeypatch.setattr(mod, '_source_lines', lambda: ([], 0))
    monkeypatch.setattr(sys, 'argv', ['audit_doc_claims.py', '--fix'])
    assert mod.main() == 0, '条数已经不符却退了"全部对上" ⇒ 跨行承诺没进判红的那一组'
    out = capsys.readouterr().out
    lines = doc.read_text(encoding='utf-8').splitlines()
    assert lines[0] == u'判据：`tests/unit/test_wrap.py`', '名字那一行被动了：%s / %s' % (lines, out)
    assert lines[1] == u'（42 条判据逐条打红）。', '该改的那一处没改对字节：%s / %s' % (lines, out)
    assert lines[2] == u'当场照出 14 条漏桩用例（`tests/unit/test_far.py` 整个文件的补拉腿）。', lines
    assert '已改写 1 处条数' in out, out


def test_a_script_name_that_happens_to_contain_test_is_not_a_claim(tmp_path, monkeypatch):
    """`backtest_l1_weighting.py` 里含着 `test_l1_weighting.py` ⇒ 不算一条测试承诺。

    第 42 轮实测的误报：模块总览写"三个只读分析脚本改走只读门"，尺子把
    `backtest_l1_weighting.py` 切成 `test_l1_weighting.py` + "3 条"，报成"收集不到这个文件"。
    误报的尺子比没有尺子更坏 —— 它会逼人为了变绿去改一句本来对的话。
    """
    mod = _load()
    doc = tmp_path / 'E.md'
    doc.write_text(u'三个只读分析脚本：`audit_l3_clear_labels.py` / `estimate_l3_vague_labels.py` / '
                   u'`backtest_l1_weighting.py` 三个都改走统一门（3 条路径）。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {})
    now, delta, _unbound = mod._claims()
    assert now == [] and delta == [], '脚本名被切成测试文件名了：%s / %s' % (now, delta)
    # 控制：真承诺必须仍然认得出来（否则上面那条只是正则坏了）
    doc2 = tmp_path / 'F.md'
    doc2.write_text(u'判据：`tests/unit/test_real_gate.py` 7 条钉着。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc2])
    now2, _d, _u = mod._claims()
    assert [c['test'] for c in now2] == ['test_real_gate.py'] and now2[0]['stated'] == 7, now2


def test_a_claim_that_cannot_be_resolved_makes_the_run_fail_not_just_print(tmp_path, monkeypatch):
    """"指向收不到的文件"这一类必须**算进退码**（第 43 轮 A-MINOR-5：这条分支零判据覆盖）。

    上一版我只测到 `_claims()` 认得出这条承诺，没测 `main()` 拿它怎么办 ——
    把 `main()` 里 `if bad or missing or sources:` 的 `missing` 删掉，不会有任何用例变红，
    于是"文档引用了一个已经不存在的测试文件"可以一边打印一边退 0。
    """
    mod = _load()
    claim = {'file': 'X.md', 'line': 1, 'test': 'test_gone_away.py', 'stated': 4,
             'span': (0, 1), 'text': 'test_gone_away.py 4 条'}
    monkeypatch.setattr(mod, '_claims', lambda: ([claim], [], []))
    monkeypatch.setattr(mod, '_source_lines', lambda: ([], 3))
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_other.py': 3})
    monkeypatch.setattr(sys, 'argv', ['audit_doc_claims.py'])
    assert mod.main() == 3, '指向收不到文件的承诺被打印了却没算进失败 ⇒ 这句"对上了"是假的'

    # 控制：同一形状，但文件收得到且条数相等 ⇒ 必须退 0（否则上面那条只是恒红）
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_gone_away.py': 4})
    assert mod.main() == 0


def test_the_repository_data_source_lines_are_auditable():
    """真文档过账：`docs/*.md` 里每一行 `数据源：` 都得认得出是哪个库（或承认未记录 + 复现命令）。

    控制断言：仓库里必须**真的**有这种行（≥1）。0 行的话上面两条判据永远为真，
    这条也会永远绿 —— 那正是第 40 轮 A 席 M2 数过的空判形状。
    """
    mod = _load()
    bad, seen = mod._source_lines()
    assert seen >= 1, '一行 `数据源：` 都没认出来 ⇒ 尺子与文档写法脱节了'
    assert bad == [], '这些报告没说自己出自哪个库：%s' % [
        '%s:%s %s' % (c['file'], c['line'], c['text']) for c in bad]


def test_claims_skipped_because_the_paragraph_is_baseline_flow_are_counted_aloud(
        tmp_path, monkeypatch, capsys):
    """流水段里"没判"这件事必须**可见**（第 46 轮 B-M2）。

    B 席要的按句判我实测驳回了：`AGENTS.md` 的"当前测试基线"是一整条
    `（上一基线 X → 本批 Y：+N 条，分布在 test_a.py 新增 3 条（…）` 的链，
    切句后"新增 3 条"脱离流水语境被当当场账 ⇒ 假红（文档写 3 条、当场 35 条），
    而"把对的数改成错的"是这把尺子最不该做的事。
    所以这里钉的是**能做的那一半**：混在流水段里的真承诺确实不判，但条数必须印出来；
    同一句改写成独立段落就必须重新被 judged（否则这条 print 只是装饰）。
    """
    mod = _load()
    doc = tmp_path / 'F.md'
    doc.write_text(u'- （上一基线 10/11 → 本批 12/13：+2 条，分布在 `tests/unit/test_a.py` 1 条）\n'
                   u'\n'
                   u'另一处独立成段：`tests/unit/test_b.py` 9 条钉着。\n', encoding='utf-8')
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_a.py': 4, 'test_b.py': 9})
    now, delta, _unbound = mod._claims()
    assert [c['test'] for c in now] == ['test_b.py'], \
        '独立成段的那句没被当当场账 ⇒ 流水豁免扩到了整篇：%s' % now
    assert [c['test'] for c in delta] == ['test_a.py'], delta
    assert delta[0].get('why'), '流水段里跳过的那条没带原因 ⇒ 印出来的数没人看得懂'
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['audit_doc_claims.py'])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert '1 条只因所在那一段是基线流水' in out, \
        '"没判几条"没印出来 ⇒ B-M2 说的那种空转还是不可见：%s' % out


def test_the_fix_flag_rewrites_counts_and_leaves_the_other_accounts_alone(tmp_path, monkeypatch,
                                                                          capsys):
    """`--fix` 改了**什么**、没改**什么**，得有第二条用例（第 44 轮 A-m2：这条分支零覆盖）。

    脚本自己写着"只代改条数这一种账"，但没人跑过这句话：把它改成"连数据源那一行一起替换"、
    或"算了却没写回文件"、或"每次跑都再腐蚀一遍"，全套件仍然全绿。所以四件事都要钉：
    ① 两处不符的条数被改成当场数；② 本来就对的那一处与"不代改"的数据源行**一个字节都不许动**；
    ③ 回执把两笔账各报各的数（`已改写 N 处` / `不代改：M 行数据源账`）；
    ④ 再跑一次必须什么都没改（幂等）。
    """
    mod = _load()
    doc = tmp_path / 'AGENTS.md'
    doc.write_text(u'用例：`tests/unit/test_x.py` 3 条钉着。\n'
                   u'另一处：`tests/unit/test_y.py` 12 条。\n'
                   u'数据源：`DATABASE_URL`（这一行认不出是哪个库）\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    monkeypatch.setattr(mod, '_doc_files', lambda: [doc])
    monkeypatch.setattr(mod, '_collected_counts', lambda: {'test_x.py': 9, 'test_y.py': 12})
    monkeypatch.setattr(mod, '_source_lines', lambda: ([{
        'file': 'AGENTS.md', 'line': 3, 'why': '只写了变量名', 'text': '数据源：`DATABASE_URL`'}], 1))
    monkeypatch.setattr(sys, 'argv', ['audit_doc_claims.py', '--fix'])

    rc = mod.main()
    out = capsys.readouterr().out
    text = doc.read_text(encoding='utf-8')
    assert '`tests/unit/test_x.py` 9 条' in text, '该改的那一处没改：%s / %s' % (text, out)
    assert '`tests/unit/test_y.py` 12 条' in text, '本来就对的一处被改了 ⇒ 它在动自己没核对过的字节'
    assert '数据源：`DATABASE_URL`' in text, '说了"不代改"却动手改了数据源那一行'
    assert '已改写 1 处条数' in out, out
    assert '不代改：1 行数据源账' in out, out
    assert rc == 3, '数据源账还没平却退 %s ⇒ `--fix` 把"没修完"报成了"修完了"' % rc

    before = text
    assert mod.main() == 3                       # 第二次跑：条数已经对上了
    again = capsys.readouterr().out
    assert '已改写 0 处条数' in again, again
    assert doc.read_text(encoding='utf-8') == before, '--fix 不幂等 ⇒ 它每跑一次就腐蚀一次文档'
