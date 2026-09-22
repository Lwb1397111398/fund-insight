# -*- coding: utf-8 -*-
"""静态板块表 `SECTOR_FUND_MAP` 的永久守护用例（不打网、不碰库）。

为什么必须有：2026-09-22 实测，1616 条活预测里有 **916 条**所在板块只被这张写死的表
覆盖（映射表里没有已审查行）——老板抱怨的"识别出来的基金离板块差十万八千里"，
源头有一大块在这张表上，而它此前 26 轮从来没被体检过。

判据只有一条来源：`scripts/audit_static_sector_map.py` + 离线夹具
`tests/fixtures/sector_map_roster_snapshot.json`（表里每个代码的官方名 + 每个板块核心词
在全网名册里的最佳对口候选）。**用例不自己抄一份判据** —— 手抄清单漏一项就等于没测
（第 15/16 轮各付过一次账）。

改完表要跑：
    python scripts/audit_static_sector_map.py --fixture tests/fixtures/sector_map_roster_snapshot.json
    # 若新增了代码/板块，再刷新夹具（会打网）：
    python scripts/audit_static_sector_map.py --emit-fixture tests/fixtures/sector_map_roster_snapshot.json
"""
import ast
import copy
import importlib.util
import io
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'sector_map_roster_snapshot.json')
spec = importlib.util.spec_from_file_location(
    'audit_static_map', os.path.join(ROOT, 'scripts', 'audit_static_sector_map.py'))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

spec2 = importlib.util.spec_from_file_location(
    'sync_map_funds', os.path.join(ROOT, 'scripts', 'sync_sector_map_funds.py'))
sync = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(sync)

from src.constants.sector_fund_map import (          # noqa: E402
    SECTOR_FUND_MAP, SECTOR_PROXY_ALLOWED, SECTOR_NO_STATIC_FUND)


@pytest.fixture(scope='module')
def roster():
    assert os.path.exists(FIXTURE), (
        '缺少离线名册夹具 %s —— 用 --emit-fixture 生成，别改用打网' % FIXTURE)
    return audit.load_fixture(FIXTURE)


def _judge(entries, roster, proxy=None):
    by_code, d1_words = roster
    return audit.classify(entries, by_code, set(entries.keys()),
                          SECTOR_PROXY_ALLOWED if proxy is None else proxy, d1_words)


def _dirty(buckets):
    """需要处理的行：查无此码 / 另有更对口 / 主题冲突 / 说不清的代理。"""
    return {k: [r[0] for r in v] for k, v in buckets.items()
            if k.startswith(('A_', 'D')) and v}


# ---------- 现状必须干净 ----------

def test_static_table_has_no_unexplained_sector_fund_mismatch(roster):
    bad = _dirty(_judge(SECTOR_FUND_MAP, roster))
    assert not bad, (
        '静态表出现"板块↔基金对不上"，逐条如下（官方名来自名册，不是我的看法）：\n%s\n'
        '要么换成名册里字面对口的代码，要么把理由写进 SECTOR_PROXY_ALLOWED。'
        % '\n'.join('  %s: %s' % (k, v) for k, v in bad.items()))


def test_every_label_equals_the_official_name(roster):
    """表里的 `name` 必须逐字等于名册官方名：前端展示的就是它。"""
    bad = audit.label_problems(SECTOR_FUND_MAP, roster[0])
    assert not bad, '标签与官方名不符（改码忘改名）：\n%s' % '\n'.join(
        '  %s %s 标签=%s 官方=%s' % r[:4] for r in bad)


def test_a_code_outside_the_fund_domain_is_caught(roster):
    """老板的硬约束"只允许基金不允许股票"：名册是基金域名册，查无此码即旗标。

    拿 600189（股票「冰山冷热」）当反例 —— 它在基金域里根本不存在，必须落 A 桶。
    """
    entries = copy.deepcopy(SECTOR_FUND_MAP)
    entries['测试股票板块'] = {'code': '600189', 'name': '冰山冷热'}
    buckets = _judge(entries, roster)
    assert '测试股票板块' in _dirty(buckets).get('A_查无此码', [])


# ---------- 判据本身不能是摆设 ----------

def test_guard_catches_a_quietly_swapped_code(roster):
    """把 白酒 换成 512480（半导体ETF国联安）必须红 —— 这是本用例存在的唯一理由。"""
    entries = copy.deepcopy(SECTOR_FUND_MAP)
    entries['白酒'] = {'code': '512480', 'name': '半导体ETF国联安'}
    dirty = _dirty(_judge(entries, roster))
    assert '白酒' in (dirty.get('D1_另有更对口', []) + dirty.get('D2_主题冲突嫌疑', [])), dirty


def test_old_suffix_shortcut_would_have_let_that_through(roster):
    """钉住第 27 轮修掉的短路：旧版"官方名以标签开头就算 B_后缀差异"会放走同一行。

    旧判据只看手写标签与官方名的关系，标签是作者自己填的 ⇒ 改码时顺手改名就能全绿。
    这条用例断言"旧写法绿、新写法红"同时成立：如果哪天有人把短路加回去，它会红。
    """
    by_code, _d1 = roster
    official = by_code['512480']['name']                  # 半导体ETF国联安
    old_entry = {'白酒': {'code': '512480', 'name': official}}

    def old_rule_is_green(entry):
        code, label = entry['code'], entry['name']
        off = (by_code.get(code) or {}).get('name') or ''
        return bool(off and label and off.startswith(label))     # 旧版到这里就 continue

    assert old_rule_is_green(old_entry['白酒']), '旧判据已经不复现了，这条用例要改写'
    assert '白酒' in _dirty(_judge(old_entry, roster)).get('D1_另有更对口', [])


def test_row_regex_actually_matches_every_row_in_the_file(roster):
    """`--fix-labels` 的正则必须匹到表里每一行 —— 匹不到时它报告"0 行"，看着像"已经干净"。

    第 27 轮实测就是这个坑：正则里多打了一个引号 ⇒ 匹配 0 行，而体检同时报 92 条标签不符。
    """
    text = io.open(audit.MAP_SOURCE, encoding='utf-8').read()
    matched = [audit.ROW_RE.match(l) for l in text.split('\n')]
    hits = [m for m in matched if m]
    assert len(hits) == len(SECTOR_FUND_MAP), (
        '静态表 %d 条，正则只匹到 %d 行 ⇒ 改标签的工具在空转' % (len(SECTOR_FUND_MAP), len(hits)))
    assert {m.group('sector') for m in hits} == set(SECTOR_FUND_MAP)


def test_fix_labels_repairs_exactly_the_rows_it_flags(roster, tmp_path):
    """把 3 行标签故意写歪：dry-run 报 3、真改之后报 0，且只动 name 那一段。"""
    by_code, _d1 = roster
    src = tmp_path / 'sector_fund_map.py'
    text = io.open(audit.MAP_SOURCE, encoding='utf-8').read()
    victims = ['白酒', '芯片', '煤炭']
    for sector in victims:
        old = "'%s': {'code': '%s', 'name': '%s'}" % (
            sector, SECTOR_FUND_MAP[sector]['code'], SECTOR_FUND_MAP[sector]['name'])
        assert text.count(old) == 1, '定位不到这一行，用例本身的写法失效了：%s' % old
        text = text.replace(old, old.replace(SECTOR_FUND_MAP[sector]['name'], '随便写个名字'))
    src.write_text(text, encoding='utf-8')

    assert audit.fix_labels(by_code, apply=False, path=str(src)) == (3, 0)
    assert io.open(str(src), encoding='utf-8').read() == text, 'dry-run 把文件写了'
    assert audit.fix_labels(by_code, apply=True, path=str(src)) == (3, 0)
    fixed = io.open(str(src), encoding='utf-8').read()
    assert '随便写个名字' not in fixed
    for sector in victims:
        assert SECTOR_FUND_MAP[sector]['name'] in fixed
    assert audit.fix_labels(by_code, apply=False, path=str(src)) == (0, 0)
    # 只改标签不该动到代码：逐行对比，差异行数必须正好是那 3 行
    diff = [(a, b) for a, b in zip(text.split('\n'), fixed.split('\n')) if a != b]
    assert len(diff) == 3 and all(len(a.split("'")) == len(b.split("'")) for a, b in diff)


def test_fix_labels_counts_rows_it_refuses_to_touch(roster, tmp_path):
    """跳过的行要单独报数，不能被算进"改好了"里（第 27 轮 D-MINOR-9：退码 0 说谎）。"""
    by_code, _d1 = roster
    src = tmp_path / 'sector_fund_map.py'
    text = io.open(audit.MAP_SOURCE, encoding='utf-8').read()
    old = "'白酒': {'code': '%s', 'name': '%s'}" % (
        SECTOR_FUND_MAP['白酒']['code'], SECTOR_FUND_MAP['白酒']['name'])
    assert text.count(old) == 1
    src.write_text(text.replace(old, old.replace(SECTOR_FUND_MAP['白酒']['name'], '随便写个名字')),
                   encoding='utf-8')
    # 名册里查不到那只代码 ⇒ 不敢编名字，只能跳过：改 0 行、跳 1 行
    without = {c: n for c, n in by_code.items() if c != SECTOR_FUND_MAP['白酒']['code']}
    assert audit.fix_labels(without, apply=False, path=str(src)) == (0, 1)
    assert audit.fix_labels(by_code, apply=False, path=str(src)) == (1, 0)



def test_weak_literal_hits_are_labeled_as_weak(roster):
    """只共用一个汉字的"相关"必须被**说出来**，不许混在 R 桶里冒充"已核对"。

    第 27 轮 D-MINOR-3：`建材 → 基建ETF`、`家居 → 家电ETF` 是靠共一个字过关的，
    而我此前把 R 桶说成"板块↔官方名全部相关"——说重了。弱命中不等于错码
    （`恒科 → 恒生科技ETF` 就是别名，语义上对），但这一格必须是"字面这根轴说不出话"，
    而不是"已经核对过"。数量钉在这里，是为了让文档与汇报里引用这个数时被迫跟着改。
    """
    assert audit.relevance_kind('建材', '基建ETF国泰') == 'char'
    assert audit.relevance_kind('半导体', '半导体ETF国联安') == 'core'
    assert audit.relevance_kind('区块链', '疫苗ETF富国') is None
    buckets = _judge(SECTOR_FUND_MAP, roster)
    weak = [r for r in buckets['R_字面命中'] if r[4].startswith('只与板块共用')]
    assert all(audit.relevance_kind(r[0], r[3]) == 'char' for r in weak), weak
    assert len(weak) == 13, (
        '只靠共字过关的行数变了（现 %d）⇒ 同步改 AGENTS.md 与模块总览里引用这个数的句子'
        % len(weak))
    assert '建材' in {r[0] for r in weak}


# ---------- 代理登记表不能变成免检通道 ----------

def test_proxy_registry_only_contains_real_proxies(roster):
    """登记的每一条必须确实"字面不相关"；其实字面命中的属于死条目，要红。

    否则这张表会越写越宽，最后变成"什么都能登记成代理"的免检通道。
    """
    by_code = roster[0]
    dead = [s for s in SECTOR_PROXY_ALLOWED
            if s in SECTOR_FUND_MAP
            and audit.is_relevant(s, (by_code.get(SECTOR_FUND_MAP[s]['code']) or {}).get('name') or '')]
    assert not dead, '这些板块的官方名其实字面命中，不必登记为代理：%s' % dead
    stale = [s for s in SECTOR_PROXY_ALLOWED if s not in SECTOR_FUND_MAP]
    assert not stale, '代理登记表里有条目已经从静态表删掉了：%s' % stale


def test_proxy_registry_never_has_an_empty_reason():
    thin = {s: r for s, r in SECTOR_PROXY_ALLOWED.items()
            if not isinstance(r, str) or len(r.strip()) < 15}
    assert not thin, '代理理由短到没法复核（<15 字）：%s' % list(thin)


def test_registered_proxies_are_actually_reported_not_hidden(roster):
    """登记过的代理要单独出现在 E 桶里给人复核，不能悄悄算成"字面命中"。"""
    buckets = _judge(SECTOR_FUND_MAP, roster)
    assert sorted(r[0] for r in buckets['E_已登记代理']) == sorted(SECTOR_PROXY_ALLOWED)


def test_unregistered_proxy_goes_red(roster):
    """反证上一条：把 美股 从登记表里撤掉，它必须落进待处理桶而不是 R 桶。"""
    rest = {k: v for k, v in SECTOR_PROXY_ALLOWED.items() if k != '美股'}
    dirty = _dirty(_judge(SECTOR_FUND_MAP, roster, proxy=rest))
    assert '美股' in sum(dirty.values(), [])


# ---------- "不许硬凑"的板块不能被模糊匹配吸回来 ----------

def test_blocked_sectors_resolve_to_none_not_to_a_nearby_fund(monkeypatch):
    """从表里删掉的板块必须真的查不出基金。

    第 27 轮实测：只删键不够 —— `get_fund_for_sector('卫星互联网')` 会被 '互联网'
    这个键子串吸走，返回 517200 互联网ETF嘉实；'铜' 会被别名吸到 有色金属。
    所以"宁可交给 agent 也不硬凑"要写成显式名单（SECTOR_NO_STATIC_FUND）。
    """
    from src.constants import sector_fund_map as m
    monkeypatch.setattr(m, '_load_db_aliases', lambda: {})
    assert '互联网' in m.SECTOR_FUND_MAP, '反例前提没了：表里得有"互联网"这个键'
    for sector in m.SECTOR_NO_STATIC_FUND:
        assert sector not in m.SECTOR_FUND_MAP, '%s 既在表里又在"不许硬凑"名单里' % sector
        got = m.get_fund_for_sector(sector)
        assert got is None, '%s 本该交给 agent，却被吸到了 %s' % (sector, got)


def test_blocked_sector_can_be_overridden_by_a_user_alias(monkeypatch):
    """名单压得住代码里的别名与模糊匹配，但压不住老板在界面上登记的自定义别名。"""
    from src.constants import sector_fund_map as m
    monkeypatch.setattr(m, '_load_db_aliases', lambda: {'风电': '光伏'})
    assert m.get_fund_for_sector('风电') == m.SECTOR_FUND_MAP['光伏']
    # 人登记的是**更长说法**时同样优先（那是人的决定，不是代码在猜）
    monkeypatch.setattr(m, '_load_db_aliases', lambda: {'卫星互联网产业': '半导体'})
    assert m.get_fund_for_sector('卫星互联网产业') == m.SECTOR_FUND_MAP['半导体']
    monkeypatch.setattr(m, '_load_db_aliases', lambda: {})
    assert m.get_fund_for_sector('风电') is None


def test_blocked_sector_cannot_be_bypassed_by_a_longer_name():
    """第 27 轮 C-M2：名单原来只挡**逐字相同**的键，于是 `卫星互联网产业` 从名单边上绕过去，
    再被 `get_fund_for_sector` 第 5 步的子串匹配吸到表里更短的键 `互联网` 上 ⇒ 拿回的正是
    本轮声称要挡住的那只 517200 互联网ETF嘉实。`normalize_sector_name` 第 4 步连
    `len >= 3` 的门槛都没有，而它牵动 sector_core 身份判据 ⇒ 两条腿都要钉住。
    """
    from src.constants import sector_fund_map as m
    offenders = []
    for blocked in sorted(SECTOR_NO_STATIC_FUND):
        for name in [blocked] + [blocked + v for v in ('产业', '主题', '板块', '产业链', '概念')]:
            fund = m.get_fund_for_sector(name)
            if fund is not None:
                offenders.append('%s → %s' % (name, fund))
            normalized = m.normalize_sector_name(name)
            if normalized != name:
                offenders.append('%s 被归一成 %s' % (name, normalized))
    assert not offenders, '屏蔽名单被更长的板块名绕过：%s' % '；'.join(offenders)


def test_blocklist_never_swallows_a_static_table_key():
    """反方向也要成立：名单只挡"名册里查无对口基金"的板块，不许把表里真有的键吸掉。

    `_literal_block_hit` 的判据是"最具体的一方说了算"，这条盯的就是那个比较的另一半。
    """
    from src.constants import sector_fund_map as m
    blocked_keys = [k for k in m.SECTOR_FUND_MAP if m._literal_block_hit(k)]
    assert not blocked_keys, '这些静态表键被"不许硬凑"名单挡住了：%s' % '、'.join(blocked_keys)
    wrong = [(k, m.get_fund_for_sector(k)) for k in m.SECTOR_FUND_MAP
             if m.get_fund_for_sector(k) != m.SECTOR_FUND_MAP[k]]
    assert not wrong, '这些表键解析不出自己那行基金：%s' % wrong[:5]


def test_blocked_sectors_really_have_no_literal_fund_in_the_roster():
    """"名册里查无对口基金"必须是夹具里的数据，不是注释里的一句话。

    `d1_words[词]` 只有在**全网名册里存在名字含该词的基金**时才会被 `emit_fixture` 写进去，
    所以"被屏蔽板块的核心词不出现在 d1_words"就等于"全名册查无对口基金"。
    第 27 轮实测：名单里 13 个词在 27905 只里的命中数全为 0。
    """
    raw = json.load(io.open(FIXTURE, encoding='utf-8'))
    d1, checked = raw.get('d1_words') or {}, set(raw.get('checked_sectors') or [])
    # 第 27 轮 D-MINOR-4：拿一份被截断/空的名册扫一遍，同样会得到"查无对口基金"，
    # 所以"扫过多少只"和"扫得到别的板块"这两件正对照必须先成立，下面的断言才有意义
    assert raw.get('roster_size', 0) >= 20000, (
        '夹具记录的名册规模只有 %s 只 ⇒ 名册被截断，"查无对口"这条前提不成立，重跑 --emit-fixture'
        % raw.get('roster_size'))
    assert d1, '夹具里 d1_words 是空的 ⇒ 名册一条都没扫到，下面的"查无"属于空判'
    blind = [s for s in ('半导体', '白酒', '军工', '黄金')
             if not any(v in d1 for v in audit.sector_variants(s))]
    assert not blind, '这些板块名册里明明有对口基金却扫不到 ⇒ 判据或夹具坏了：%s' % '、'.join(blind)
    missing = sorted(set(SECTOR_NO_STATIC_FUND) - checked)
    assert not missing, (
        '夹具没扫过这些被屏蔽的板块（是旧数据刷的？）：%s ⇒ 重跑 --emit-fixture' % '、'.join(missing))
    have = ['%s（名册里有 %s）' % (s, d1[v]) for s in sorted(SECTOR_NO_STATIC_FUND)
            for v in audit.sector_variants(s) if v in d1]
    assert not have, '这些板块其实有字面对口的基金，不该屏蔽而该改挂：%s' % '、'.join(have)


def test_d1_by_code_leg_names_another_fund_not_the_row_itself():
    """`by_code` 那条腿（第 26 轮 BLOCKER 的修法）单独钉一次。

    第 27 轮 D-MINOR-2：把这条腿退回"把自己当成更对口的候选、再被 `!= code` 剔掉"的
    自指写法，当时 16 条守护用例全绿 ⇒ 在线跑法（名册 2.79 万只）从 D1 掉成 D2，
    丢掉"该换成哪只"这句话，而没人会发现。所以这里要的不仅是"落进 D1 桶"，
    还要**证据里点出的是另一只基金**。
    """
    entries = {'机器人': {'code': '159852', 'name': '云计算ETF'}}
    by_code = {'159852': {'name': '云计算ETF嘉实'},
               '562500': {'name': '机器人ETF华夏'}}
    buckets = audit.classify(entries, by_code, set(entries.keys()) | {'云计算'},
                             SECTOR_PROXY_ALLOWED, d1_words={})
    found = buckets['D1_另有更对口']
    assert [r[0] for r in found] == ['机器人'], buckets
    assert '562500' in found[0][4] and '159852' not in found[0][4], found[0][4]
    # 第 27 轮 D-MINOR-1/C-MINOR：回执串必须是 `代码:官方名`，不能是 dict 的 repr——
    # 这一格人和 LLM 都要读，`161725:{'name': …}` 等于把证据换成噪声
    assert '机器人ETF华夏' in found[0][4] and "{'name'" not in found[0][4], found[0][4]


def test_d1_roster_word_leg_is_reachable_and_wired():
    """`d1_words`（全网名册里"另有更对口"的那张表）必须是**接上的保险**，不是装饰。

    第 27 轮 C-M3：把 `classify` 收到的 `d1_words` 强制置空，当时 16 条守护用例全绿 ⇒
    这条腿没人测过。今天表内 109 行确实没有一行**需要**它（`by_code` 那条腿自己就能给出
    候选），所以不能拿"真实数据里 D1 出自哪条腿"来钉；这里改钉两件事：
    ① 构造一个只有 `d1_words` 能救的场景 ⇒ 有它落 D1、没它落 D2（腿本身是活的）；
    ② 脚本 `main()` 里那次 `classify(...)` 必须显式带 `d1_words=` ⇒ 接线不会被顺手删掉。
    """
    entries = {'机器人': {'code': '159852', 'name': '云计算ETF'}}
    by_code = {'159852': {'name': '云计算ETF嘉实'}}    # 光靠 by_code 找不出更对口的候选
    words = {v: '562500:机器人ETF华夏' for v in audit.sector_variants('机器人')}

    def judge(d1_words):
        return audit.classify(entries, by_code, {'机器人', '云计算'},
                              SECTOR_PROXY_ALLOWED, d1_words=d1_words)

    found = judge(words)['D1_另有更对口']
    assert [r[0] for r in found] == ['机器人'], found
    assert '562500' in found[0][4], found
    dropped = judge({})
    assert not dropped['D1_另有更对口']
    assert [r[0] for r in dropped['D2_主题冲突嫌疑']] == ['机器人'], dropped

    source = io.open(os.path.join(ROOT, 'scripts', 'audit_static_sector_map.py'),
                     encoding='utf-8').read()
    calls = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'classify']
    assert calls, '脚本里找不到 classify 调用 ⇒ 用例判据已失效，请同步改这里'
    unconnected = [c.lineno for c in calls
                   if not any(kw.arg == 'd1_words' for kw in c.keywords)]
    assert not unconnected, (
        'classify 没带 d1_words= ⇒ 名册那条腿断了（离线时 D1 会瞎）：行 %s' % unconnected)


# ---------- 补档案/净值的脚本：空名字不算成功 ----------
def test_fill_missing_names_fills_from_roster_and_reports_the_rest(test_db, monkeypatch):
    """第 27 轮实测：`update_fund_info` 对新代码会留下**空名字档案**，而我上一版把它算成 `[ok]`
    ⇒ 一次跑完凭空多出 26 行没有名字的基金档案。现在"补上了几个"和"仍补不上几个"都要报数。
    """
    from src.models.database import FundInfo
    from src.fund import fund_api
    test_db.add(FundInfo(fund_code='159999', fund_name=None))
    test_db.add(FundInfo(fund_code='158888', fund_name='   '))
    test_db.add(FundInfo(fund_code='157777', fund_name='已经有名的'))
    test_db.commit()
    monkeypatch.setattr(
        fund_api, 'load_fund_roster',
        lambda refresh=False: {'by_code': {'159999': {'name': '补名测试ETF甲'},
                                          '158888': {'name': ''}}},
        raising=False)
    filled, still = sync.fill_missing_names(
        test_db, ['159999', '158888', '157777', '156666'])
    test_db.commit()
    assert filled == [('159999', '补名测试ETF甲')]
    assert still == ['158888'], '名册里也没名字的必须报出来，不能静默'
    assert test_db.query(FundInfo).filter_by(fund_code='159999').first().fund_name == '补名测试ETF甲'
    assert test_db.query(FundInfo).filter_by(fund_code='157777').first().fund_name == '已经有名的'


def test_fill_missing_names_survives_a_dead_roster(test_db, monkeypatch):
    """名册取不到时只跳过补名这一步，别把已经补好的净值回执一起吞掉。"""
    from src.models.database import FundInfo
    from src.fund import fund_api
    test_db.add(FundInfo(fund_code='159999', fund_name=None))
    test_db.commit()

    def boom(refresh=False):
        raise RuntimeError('名册站点挂了')

    monkeypatch.setattr(fund_api, 'load_fund_roster', boom, raising=False)
    filled, still = sync.fill_missing_names(test_db, ['159999'])
    assert filled == [] and still == ['159999']




