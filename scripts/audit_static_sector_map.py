# -*- coding: utf-8 -*-
"""给**写死的静态板块表** `src/constants/sector_fund_map.py` 做一次身份体检（只读）。

为什么单独一个脚本：映射表的体检（`sweep_sector_mappings.py`）只遍历
`sector_fund_mapping` 的行，静态表从来不在它的覆盖面里。而 2026-09-22 实测：
1616 条活预测里有 **916 条**所在板块只被静态表覆盖（映射表没有已审查行）
⇒ 老板抱怨的"基金离板块差十万八千里"有一大块源头在这张表上，我前 26 轮都在修下游。

分类判据（全部来自名册/官方名，不用行业直觉）。**两根轴各自独立判，不互相短路**：

轴一 · 板块 ↔ 官方名（`relevance`）：
  A  查无此码      —— 代码在名册里根本没有；
  R  字面命中      —— 官方名含板块核心词，或与之共字；
  D1 另有更对口    —— 官方名与板块无关，**但名册里有含该核心词的基金** ⇒ 明确错码；
  D2 名册无更优    —— 官方名与板块无关且名册里查不到更对口的 ⇒ 需要人/agent 判。
  额外一条"主题冲突"信号：官方名里含着**另一个板块的核心词**（区块链→疫苗ETF、
  鸿蒙→房地产ETF）——这类即便落在 D2，也能确定性指认为错，因为那只基金本身就是
  别的板块的代表标的，不可能是"没有更好选择"的合法代理。
  E  已登记代理    —— 落在 D1/D2/D3 的任意一格，但在 `SECTOR_PROXY_ALLOWED` 里写了理由。

轴二 · 静态标签 ↔ 官方名（`label_problems`）：表里的 `name` 是**手写**的，
  只说明"作者想让它叫什么"，不说明"这只基金到底是什么"。第 27 轮实测：
  旧版把"官方名以标签开头"（B_后缀差异）当成**免检通行证**，直接跳过轴一，
  于是 110 条里有 82 条从来没被判过板块相关性 —— 把 白酒 换成 512480 半导体ETF
  并把标签同步改成"半导体ETF"，旧版全绿。这就是"验证工具给自己背书"的同义反复
  （同第 25 轮 `SET` + `SHOW` 那条）。现在标签只用来报"名字写歪了"，不再决定相关性。

用法：
    python scripts/audit_static_sector_map.py                    # 打网名册
    python scripts/audit_static_sector_map.py --roster-cache f.json
    python scripts/audit_static_sector_map.py --emit-fixture tests/fixtures/x.json
退出码：0 干净；5 存在 A/D1/D2/D3 或标签与官方名不符 ⇒ 可卡合入/跑批。
"""
import argparse
import io
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from _db_guard import pin_local_sqlite          # noqa: E402  必须先钉再碰 ORM

OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')


def load_roster(path=None):
    if path and os.path.exists(path):
        return json.load(io.open(path, encoding='utf-8'))
    from src.fund import fund_api
    roster = fund_api.load_fund_roster()
    if path:
        io.open(path, 'w', encoding='utf-8').write(json.dumps(roster, ensure_ascii=False))
    return roster


def sector_variants(sector):
    from src.services.sector_identity_audit import core_variants, sector_core
    return [v for v in core_variants(sector_core(sector)) if v]


def is_relevant(sector, official):
    """板块名与基金官方名是否字面相关（核心词命中，或共用汉字）。"""
    from src.services.sector_identity_audit import contains_core, cjk_core, sector_core
    if not official:
        return False
    if any(contains_core(official, v) for v in sector_variants(sector)):
        return True
    return bool(set(cjk_core(sector_core(sector))) & set(cjk_core(official)))


def label_problems(entries, by_code):
    """轴二：表里的 `name` 必须**逐字等于**名册官方名。

    判"板块↔基金"永远用官方名，不用这个手写标签 —— 所以标签写歪了不会让相关性变绿，
    它只会在这里红一次："你换代码忘了改名"（前端展示的就是这个名字）。
    """
    out = []
    for sector, entry in entries.items():
        label = ((entry or {}).get('name') or '').strip()
        code = (entry or {}).get('code')
        official = ((by_code.get(code) or {}).get('name') or '').strip()
        if not official:
            continue                      # A 桶已经在报"查无此码"，不重复
        if not label:
            out.append((sector, code, '', official, '标签为空'))
        elif label != official:
            out.append((sector, code, label, official, '标签与官方名不逐字相等'))
    return out



def classify(entries, by_code, sector_keys, proxy_allowed=None, d1_words=None):
    """返回 {相关性分类: [(板块, 代码, 静态标签, 官方名, 证据)]}。

    `d1_words`：{核心词: '代码:官方名'}，来自**全网名册**（离线夹具带这份表）。
    离线时 `by_code` 只有表里那几十只，"名册里另有更对口"会瞎；夹具再带一张
    `d1_words`（核心词 → 全网名册里的最佳候选），离线也就能判 D1，守护用例不必打网。
    """
    proxy_allowed = proxy_allowed or {}
    d1_words = d1_words or {}
    buckets = {'A_查无此码': [], 'R_字面命中': [], 'D1_另有更对口': [],
               'D2_主题冲突嫌疑': [], 'D3_可能是合法代理': [], 'E_已登记代理': []}
    for sector, entry in entries.items():
        code = (entry or {}).get('code')
        label = ((entry or {}).get('name') or '').strip()
        official = ((by_code.get(code) or {}).get('name') or '').strip()
        if not official:
            buckets['A_查无此码'].append((sector, code, label, '', '名册里没有这个代码'))
            continue
        variants = sector_variants(sector)
        relevant = is_relevant(sector, official)
        if not relevant:
            better = {d1_words[v] for v in variants if v in d1_words}
            # 名册里**别的**含该词的基金（`by_code` 线上是全量 2.79 万，离线是夹具那几十只）。
            # 这里不能写 `'%s:%s' % (code, official)` —— 那是把自己当成"更对口的候选"，
            # 再被下面 `!= code` 剔掉，结果这条判据永远不会命中（D1 变哑）。
            better |= {f'{c}:{n}' for c, n in by_code.items()
                       if c != code and any(v in (n.get('name') or '') for v in variants)}
            better = sorted(b for b in better if b.split(':')[0] != code)
        # 官方名自己就是"另一个板块"的字面代表 ⇒ 不可能是"没有更好选择"的代理
        other = [s for s in sector_keys
                 if s != sector and is_relevant(s, official)]
        if sector in proxy_allowed:
            # 登记过的代理不再算"嫌疑"，但要单独列出来给人复核 —— 藏进 R 桶就没人再看了
            if not relevant:
                buckets['E_已登记代理'].append((sector, code, label, official,
                                               proxy_allowed[sector]))
                continue
            buckets['R_字面命中'].append((sector, code, label, official,
                                         '登记了代理，但官方名其实字面命中'))
            continue
        if relevant:
            buckets['R_字面命中'].append((sector, code, label, official, '官方名与板块字面相关'))
        elif better:
            buckets['D1_另有更对口'].append((sector, code, label, official,
                                            '名册里有含该词的基金：%s' % '、'.join(better[:3])))
        elif other:
            buckets['D2_主题冲突嫌疑'].append((sector, code, label, official,
                                              '官方名是板块「%s」的字面标的' % '、'.join(other[:3])))
        else:
            buckets['D3_可能是合法代理'].append((sector, code, label, official, '宽基/跨市场代理？'))
    return buckets


def _rank_candidates(word, by_code, nav_counts):
    """名册里官方名含该词的最佳候选：本地有净值 > 场内 ETF 代码段（15/50/51/52/53/56/58）
    > 名头以该词开头/更短 > 代码。

    第 26 轮实测：上一版按代码字典序排，把"大数据"的 018134 联接A 排在 515400 大数据ETF富国
    前面 —— 而老板的要求明确是"能配场内 ETF 就用 ETF"。
    """
    from src.services.sector_identity_audit import contains_core
    cands = [(c, (e or {}).get('name') or '') for c, e in by_code.items()
             if contains_core((e or {}).get('name') or '', word)]
    def key(t):
        c, nm = t
        return (-min(nav_counts.get(c, 0), 400),
                0 if c[:2] in ('15', '50', '51', '52', '53', '56', '58') else 1,
                0 if nm.startswith(word) else 1, len(nm), c)
    cands.sort(key=key)
    return cands


def emit_fixture(entries, by_code, path, extra_sectors=()):
    """把守护用例要用的两份证据落成离线夹具：表里每个代码的官方名 + 每个板块核心词
    在**全网名册**里的最佳对口候选。改完静态表后重跑一次即可刷新。

    `extra_sectors` 传"不许硬凑"名单：它们不在表里，但**名单成立的前提是"名册里查无对口
    基金"** —— 不把这些词也扫一遍并落成证据，守护用例就没法离线核对这条前提（第 27 轮
    我先把理由写成注释，被自己审出来是"没有证据的说法"）。
    """
    d1_words = {}
    for sector in list(entries) + list(extra_sectors):
        for v in sector_variants(sector):
            if v in d1_words or len(v) < 2:
                continue
            cands = _rank_candidates(v, by_code, {})
            if cands:
                d1_words[v] = '%s:%s' % cands[0]
    codes = sorted({(e or {}).get('code') for e in entries.values() if (e or {}).get('code')})
    payload = {'taken_at': datetime.now().isoformat(timespec='seconds'),
               'source': '东财 fundcode_search.js（2.79 万只全量名册里挑出来的两份证据）',
               'checked_sectors': sorted(set(list(entries) + list(extra_sectors))),
               'by_code': {c: (by_code.get(c) or {}).get('name') or '' for c in codes},
               'd1_words': d1_words}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    io.open(path, 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False,
                                                          indent=1, sort_keys=True))
    return payload


MAP_SOURCE = os.path.join(ROOT, 'src', 'constants', 'sector_fund_map.py')
# 只认"一行一条、且带 code+name"的写法；SECTOR_ALIASES 那种 `'酒': '白酒',` 不会被匹上
ROW_RE = re.compile(r"^(?P<pad>\s+')(?P<sector>[^']+)'"
                    r"(?P<mid>:\s*\{'code':\s*')(?P<code>[^']+)'"
                    r"(?P<nm>,\s*'name':\s*')(?P<name>[^']*)'(?P<tail>\},.*)$")


def fix_labels(by_code, apply=False, path=MAP_SOURCE):
    """把静态表里的 `name` 逐字改成名册官方名（改码忘改名是第 27 轮实测出来的 92 处）。

    只换 `'name'` 那一段（按匹配位置切片，注释/缩进/引号一概不重排）；官方名查不到、
    或里面带引号，都单独报数并跳过 —— 宁可漏改也不拿正则把源码改坏（第 26 轮就这么改坏
    过一个脚本）。默认 dry-run，且**改完的行数必须等于 `label_problems` 的条数**，
    对不上就是正则没匹上（这条在 tests/unit/test_sector_map_guard.py 里钉着）。
    """
    lines = io.open(path, encoding='utf-8').read().split('\n')
    planned, skipped = [], []
    for idx, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        sector, code, name = m.group('sector'), m.group('code'), m.group('name')
        official = ((by_code.get(code) or {}).get('name') or '').strip()
        if not official:
            skipped.append((sector, code, '名册里没有这个代码，不敢编名字'))
            continue
        if "'" in official or '"' in official:
            skipped.append((sector, code, '官方名里有引号，正则改写法太危险'))
            continue
        if name == official:
            continue
        start, end = m.span('name')
        planned.append((idx + 1, sector, code, name, official, line[:start] + official + line[end:]))
    print('[改标签] %s：%d 行；跳过 %d 行' % ('真写' if apply else 'dry-run',
                                             len(planned), len(skipped)))
    for ln, sector, code, old, new, _ in planned[:100]:
        print('   L%-4d %-8s %-8s %-24s → %s' % (ln, sector, code, old, new))
    for sector, code, why in skipped:
        print('   [跳过] %s %s：%s' % (sector, code, why))
    if not apply:
        print('dry-run：未改文件。真改：--fix-labels --apply --confirm FIX-LABELS')
        return len(planned)
    for ln, _s, _c, _o, _n, new_line in planned:
        lines[ln - 1] = new_line
    io.open(path, 'w', encoding='utf-8').write('\n'.join(lines))
    print('[完成] 已改写 %d 行标签' % len(planned))
    return len(planned)


def load_fixture(path):
    """读夹具：既兼容 `--emit-fixture` 的新结构，也兼容老的扁平 {code: name}。"""
    raw = json.load(io.open(path, encoding='utf-8'))
    if 'by_code' not in raw:
        raw = {'by_code': raw}
    by_code = {c: {'name': n or ''} for c, n in (raw.get('by_code') or {}).items()}
    return by_code, raw.get('d1_words') or {}




def propose_repoints(buckets, by_code, nav_counts):
    """给"明确错"的那几条找字面对口的候选：名册里官方名含板块核心词、优先场内 ETF、
    优先本地已有净值历史的（有历史才验得了预测）。只出建议，不改代码。
    """
    targets = (list(buckets['D1_另有更对口']) + list(buckets['D2_主题冲突嫌疑'])
               + list(buckets['A_查无此码']) + list(buckets['D3_可能是合法代理']))
    out = []
    for sector, code, label, official, evidence in targets:
        cands = []
        for v in sector_variants(sector):
            if len(v) < 2:
                continue
            for c, nm in _rank_candidates(v, by_code, nav_counts)[:4]:
                cands.append((v, c, nm))
        cands.sort(key=lambda t: (-min(nav_counts.get(t[1], 0), 400),
                                  0 if t[1][:2] in ('15', '50', '51', '52', '53', '56', '58')
                                  else 1,
                                  0 if t[2].startswith(t[0]) else 1, len(t[2]), t[1]))
        out.append((sector, code, official, label, cands[:4]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--roster-cache', help='名册 JSON 缓存路径（给了就复用/落盘，便于离线复跑）')
    ap.add_argument('--fixture', help='用守护夹具（tests/fixtures/sector_map_roster_snapshot.json）'
                                     '离线判，不必打网；D1 靠夹具里的 d1_words')
    ap.add_argument('--emit-fixture', metavar='PATH',
                    help='按当前表 + 全网名册刷新守护夹具（会打网），写到 PATH')
    ap.add_argument('--fix-labels', action='store_true',
                    help='把表里的 name 改成名册官方名（默认 dry-run，只改这一列）')
    ap.add_argument('--apply', action='store_true', help='配合 --fix-labels 才真写文件')
    ap.add_argument('--confirm', help='真写必须带 FIX-LABELS')
    ap.add_argument('--out', help='CSV 输出（默认写进 docs 运行目录）')
    ap.add_argument('--propose', action='store_true',
                    help='为明确错的条目出名册里字面对口的候选（只读，不写代码不写库）')
    args = ap.parse_args()

    pin_local_sqlite(use_mirror_default=True)
    import sqlalchemy as sa

    from src.constants.sector_fund_map import (SECTOR_FUND_MAP, SECTOR_PROXY_ALLOWED,
                                               SECTOR_NO_STATIC_FUND)
    from src.models.database import Prediction, SessionLocal

    d1_words = {}
    if args.fixture:
        by_code, d1_words = load_fixture(args.fixture)
        print('[夹具] %s：%d 个代码、%d 个核心词（离线，不查全网名册）'
              % (args.fixture, len(by_code), len(d1_words)))
    else:
        roster = load_roster(args.roster_cache)
        by_code = roster.get('by_code') or {}
        d1_words = roster.get('d1_words') or {}
    print('[名册] %d 只基金（静态表 %d 条待检）' % (len(by_code), len(SECTOR_FUND_MAP)))

    if args.fix_labels:
        if args.apply and args.confirm != 'FIX-LABELS':
            print('[abort] 真改源码要带 --confirm FIX-LABELS（口令拼错就当没看见）')
            return 2
        fix_labels(by_code, apply=args.apply)
        return 0

    if args.emit_fixture:
        payload = emit_fixture(SECTOR_FUND_MAP, by_code, args.emit_fixture,
                               extra_sectors=SECTOR_NO_STATIC_FUND)
        print('[夹具] 已写 %s：%d 个代码 + %d 个核心词'
              % (args.emit_fixture, len(payload['by_code']), len(payload['d1_words'])))
        print('[提示] 这份夹具是 tests/unit/test_sector_map_guard.py 的判据来源，改完表要一起提交')
        return 0

    db = SessionLocal()
    try:
        live = dict(db.query(Prediction.sector, sa.func.count(Prediction.id))
                    .filter(Prediction.is_deleted == False).group_by(          # noqa: E712
                        Prediction.sector).all())
    finally:
        db.close()

    buckets = classify(SECTOR_FUND_MAP, by_code, set(SECTOR_FUND_MAP.keys()),
                       SECTOR_PROXY_ALLOWED, d1_words)
    bad_labels = label_problems(SECTOR_FUND_MAP, by_code)
    path = args.out or os.path.join(OUT_DIR, 'static-map-audit.csv')
    lines = ['bucket,sector,code,static_name,official_name,evidence,live_predictions']
    for kind, rows in buckets.items():
        print('\n== %s：%d 条' % (kind, len(rows)))
        for sector, code, label, official, evidence in rows:
            n = live.get(sector) or 0
            print('   %-10s %-8s 静态=%-22s 官方=%-26s 预测%-4d  %s'
                  % (sector, code, label, official, n, evidence))
            lines.append('%s,%s,%s,"%s","%s","%s",%d' % (
                kind, sector, code, label, official, evidence, n))
        if kind.startswith(('A_', 'D')):
            tot = sum(live.get(s) or 0 for s, *_ in rows)
            print('   —— 本桶合计牵动 %d 条活预测' % tot)
    print('\n== 标签与官方名不符：%d 条' % len(bad_labels))
    for sector, code, label, official, evidence in bad_labels:
        print('   %-10s %-8s 静态=%-22s 官方=%-26s %s' % (sector, code, label, official, evidence))
        lines.append('F_标签不符,%s,%s,"%s","%s","%s",%d' % (
            sector, code, label, official, evidence, live.get(sector) or 0))
    io.open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print('\n[报告] %s' % path)
    print('[口径] 本地镜像 data/fund_insight.db，活预测按 is_deleted=0 统计；本脚本不写库')

    dirty = sum(len(buckets[k]) for k in ('A_查无此码', 'D1_另有更对口',
                                          'D2_主题冲突嫌疑', 'D3_可能是合法代理'))
    if args.propose:
        from src.models.database import FundHistory
        db2 = SessionLocal()
        try:
            nav_counts = {r[0]: r[1] for r in db2.query(
                FundHistory.fund_code, sa.func.count(FundHistory.id)).group_by(
                FundHistory.fund_code).all()}
        finally:
            db2.close()
        print('\n===== 改码建议（只读，不写文件不写库）=====')
        rows = propose_repoints(buckets, by_code, nav_counts)
        for sector, old_code, old_name, label, cands in rows:
            print('  %-8s 现在 %-8s=%-22s（静态标签 %s）' % (sector, old_code, old_name, label))
            for word, c, nm in cands:
                print('        候选「%s」 %-8s %-26s 本地净值 %d 行'
                      % (word, c, nm, nav_counts.get(c, 0)))
            if not cands:
                print('        名册里找不到字面对口的 ⇒ 只能删掉这条静态映射，交给 agent/映射表')
        pp = os.path.join(OUT_DIR, 'static-map-repropose.txt')
        io.open(pp, 'w', encoding='utf-8').write('\n'.join(
            '%s\t%s\t%s\t%s' % (s, oc, on, '; '.join('%s:%s(%s,%d行)'
                                                      % (w, c, n, nav_counts.get(c, 0))
                                                      for w, c, n in cs))
            for s, oc, on, _l, cs in rows) + '\n')
        print('[建议清单] %s' % pp)
    if dirty or bad_labels:
        print('\n[退码 5] 待处理：%d 条板块↔基金不相关、%d 条标签与官方名不符'
              % (dirty, len(bad_labels)))
        print('[怎么修] 换成名册里字面对口的代码，或把理由写进 SECTOR_PROXY_ALLOWED；'
              '改完跑 --emit-fixture 刷新守护夹具')
        return 5
    print('\n[干净] 静态表 %d 条：板块↔官方名全部相关或已登记代理，标签全部与官方名一致'
          % len(SECTOR_FUND_MAP))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
