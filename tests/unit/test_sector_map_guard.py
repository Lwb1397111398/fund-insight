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

    assert audit.fix_labels(by_code, apply=False, path=str(src)) == 3
    assert io.open(str(src), encoding='utf-8').read() == text, 'dry-run 把文件写了'
    assert audit.fix_labels(by_code, apply=True, path=str(src)) == 3
    fixed = io.open(str(src), encoding='utf-8').read()
    assert '随便写个名字' not in fixed
    for sector in victims:
        assert SECTOR_FUND_MAP[sector]['name'] in fixed
    assert audit.fix_labels(by_code, apply=False, path=str(src)) == 0
    # 只改标签不该动到代码：逐行对比，差异行数必须正好是那 3 行
    diff = [(a, b) for a, b in zip(text.split('\n'), fixed.split('\n')) if a != b]
    assert len(diff) == 3 and all(len(a.split("'")) == len(b.split("'")) for a, b in diff)



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
    monkeypatch.setattr(m, '_load_db_aliases', lambda: {})
    assert m.get_fund_for_sector('风电') is None


def test_blocked_sectors_really_have_no_literal_fund_in_the_roster():
    """"名册里查无对口基金"必须是夹具里的数据，不是注释里的一句话。

    `d1_words[词]` 只有在**全网名册里存在名字含该词的基金**时才会被 `emit_fixture` 写进去，
    所以"被屏蔽板块的核心词不出现在 d1_words"就等于"全名册查无对口基金"。
    第 27 轮实测：名单里 13 个词在 27905 只里的命中数全为 0。
    """
    raw = json.load(io.open(FIXTURE, encoding='utf-8'))
    d1, checked = raw.get('d1_words') or {}, set(raw.get('checked_sectors') or [])
    missing = sorted(set(SECTOR_NO_STATIC_FUND) - checked)
    assert not missing, (
        '夹具没扫过这些被屏蔽的板块（是旧数据刷的？）：%s ⇒ 重跑 --emit-fixture' % '、'.join(missing))
    have = ['%s（名册里有 %s）' % (s, d1[v]) for s in sorted(SECTOR_NO_STATIC_FUND)
            for v in audit.sector_variants(s) if v in d1]
    assert not have, '这些板块其实有字面对口的基金，不该屏蔽而该改挂：%s' % '、'.join(have)


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




