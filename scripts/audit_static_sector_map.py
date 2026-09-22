# -*- coding: utf-8 -*-
"""给**写死的静态板块表** `src/constants/sector_fund_map.py` 做一次身份体检（只读）。

为什么单独一个脚本：映射表的体检（`sweep_sector_mappings.py`）只遍历
`sector_fund_mapping` 的行，静态表从来不在它的覆盖面里。而 2026-09-22 实测：
1616 条活预测里有 **916 条**所在板块只被静态表覆盖（映射表没有已审查行）
⇒ 老板抱怨的"基金离板块差十万八千里"有一大块源头在这张表上，我前 26 轮都在修下游。

分类判据（全部来自名册/官方名，不用行业直觉）：
  A 查无此码      —— 代码在名册里根本没有；
  B 只是后缀差异  —— 静态标签是官方名的前缀（"家电ETF" ⊂ "家电ETF国泰"）；
  C 字面命中      —— 官方名含板块核心词，或与之共字；
  D1 另有更对口的 —— 官方名与板块无关，**但名册里有含该核心词的基金** ⇒ 明确错码；
  D2 名册无更优  —— 官方名与板块无关且名册里查不到更对口的 ⇒ 需要人/agent 判。
  额外一条"主题冲突"信号：官方名里含着**另一个板块的核心词**（区块链→疫苗ETF、
  鸿蒙→房地产ETF）——这类即便落在 D2，也能确定性指认为错，因为那只基金本身就是
  别的板块的代表标的，不可能是"没有更好选择"的合法代理。

用法：
    python scripts/audit_static_sector_map.py                  # 打网名册，出报告 + CSV
    python scripts/audit_static_sector_map.py --roster-cache path.json
"""
import argparse
import io
import json
import os
import sys

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


def classify(entries, by_code, roster_names, sector_keys):
    """返回 {分类: [(板块, 代码, 静态标签, 官方名, 证据)]}"""
    from src.services.sector_identity_audit import (
        contains_core, core_variants, cjk_core, sector_core)

    buckets = {'A_查无此码': [], 'B_后缀差异': [], 'C_字面命中': [],
               'D1_另有更对口': [], 'D2_主题冲突嫌疑': [], 'D3_可能是合法代理': []}
    for sector, entry in entries.items():
        code = (entry or {}).get('code')
        label = ((entry or {}).get('name') or '').strip()
        official = ((by_code.get(code) or {}).get('name') or '').strip()
        if not official:
            buckets['A_查无此码'].append((sector, code, label, '', '名册里没有这个代码'))
            continue
        if label and official.startswith(label):
            buckets['B_后缀差异'].append((sector, code, label, official, '静态名是官方名前缀'))
            continue
        variants = list(core_variants(sector_core(sector)))
        if any(contains_core(official, v) for v in variants) or \
                (set(cjk_core(sector_core(sector))) & set(cjk_core(official))):
            buckets['C_字面命中'].append((sector, code, label, official, '官方名与板块字面相关'))
            continue
        better = [v for v in variants if v and any(v in n for n in roster_names)]
        if better:
            buckets['D1_另有更对口'].append((sector, code, label, official,
                                            '名册里有含%s的基金' % '、'.join(better[:3])))
            continue
        # 官方名自己就是"另一个板块"的字面代表 ⇒ 不可能是"没有更好选择"的代理
        other = [s for s in sector_keys
                 if s != sector and any(contains_core(official, v)
                                        for v in core_variants(sector_core(s)))]
        if other:
            buckets['D2_主题冲突嫌疑'].append((sector, code, label, official,
                                              '官方名是板块「%s」的字面标的' % '、'.join(other[:3])))
        else:
            buckets['D3_可能是合法代理'].append((sector, code, label, official, '宽基/跨市场代理？'))
    return buckets


def propose_repoints(buckets, by_code, nav_counts):
    """给"明确错"的那几条找字面对口的候选：名册里官方名含板块核心词、优先 ETF、
    优先本地已有净值历史的（有历史才验得了预测）。只出建议，不改代码。
    """
    from src.services.sector_identity_audit import contains_core, core_variants, sector_core

    targets = list(buckets['D1_另有更对口']) + list(buckets['D2_主题冲突嫌疑'])
    extra_d3 = [r for r in buckets['D3_可能是合法代理'] if r[0] in ('区块链', '机场')]
    targets += extra_d3
    out = []
    for sector, code, label, official, evidence in targets:
        variants = [v for v in core_variants(sector_core(sector)) if v and len(v) >= 2]
        cands = []
        for c, entry in by_code.items():
            nm = (entry or {}).get('name') or ''
            if any(contains_core(nm, v) for v in variants):
                cands.append((c, nm))
        # 排序（第 26 轮实测出的毛病）：
        # ① 本地已有净值历史的优先（能验证 > 只能展示）；
        # ② **场内 ETF 代码**（15/50/51/52/53/56/58 开头）优先于联接基金——
        #    上一版按代码字典序排，结果"大数据"把 018134 联接A 排在 515400 大数据ETF富国
        #    前面，而老板的要求明确是"能配场内 ETF 就用 ETF"；
        # ③ 官方名以板块核心词开头/更短的优先（"大数据ETF富国" 优于 "华夏云计算与大数据ETF联接A"）；
        # ④ 最后才按代码。
        def key(t):
            c, nm = t
            return (-min(nav_counts.get(c, 0), 400),
                    0 if c[:2] in ('15', '50', '51', '52', '53', '56', '58') else 1,
                    0 if any(nm.startswith(v) for v in variants) else 1,
                    len(nm), c)
        cands.sort(key=key)
        out.append((sector, code, official, label, cands[:4]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--roster-cache', help='名册 JSON 缓存路径（给了就复用/落盘，便于离线复跑）')
    ap.add_argument('--out', help='CSV 输出（默认写进 docs 运行目录）')
    ap.add_argument('--propose', action='store_true',
                    help='为明确错的条目出名册里字面对口的候选（只读，不写代码不写库）')
    args = ap.parse_args()

    pin_local_sqlite(use_mirror_default=True)
    import sqlalchemy as sa

    from src.constants.sector_fund_map import SECTOR_FUND_MAP
    from src.models.database import Prediction, SessionLocal

    roster = load_roster(args.roster_cache)
    by_code = roster.get('by_code') or {}
    roster_names = [ (v or {}).get('name') or '' for v in by_code.values() ]
    print('[名册] %d 只基金（静态表 %d 条待检）' % (len(by_code), len(SECTOR_FUND_MAP)))

    db = SessionLocal()
    try:
        live = dict(db.query(Prediction.sector, sa.func.count(Prediction.id))
                    .filter(Prediction.is_deleted == False).group_by(          # noqa: E712
                        Prediction.sector).all())
    finally:
        db.close()

    buckets = classify(SECTOR_FUND_MAP, by_code, roster_names, set(SECTOR_FUND_MAP.keys()))
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
        if kind.startswith(('D1', 'D2')):
            tot = sum(live.get(s) or 0 for s, *_ in rows)
            print('   —— 本桶合计牵动 %d 条活预测' % tot)
    io.open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print('\n[报告] %s' % path)
    print('[口径] 本地镜像 data/fund_insight.db，活预测按 is_deleted=0 统计；本脚本不写库')

    if args.propose:
        import sqlalchemy as sa

        from src.models.database import FundHistory
        db2 = SessionLocal()
        try:
            nav_counts = {r[0]: r[1] for r in db2.query(
                FundHistory.fund_code, sa.func.count(FundHistory.id)).group_by(
                FundHistory.fund_code).all()}
        finally:
            db2.close()
        print('\n===== 改码建议（只读，不写文件不写库）=====')
        for sector, old_code, old_name, label, cands in propose_repoints(
                buckets, by_code, nav_counts):
            print('  %-8s 现在 %-8s=%-22s（静态标签 %s）' % (sector, old_code, old_name, label))
            for c, nm in cands:
                print('        候选 %-8s %-26s 本地净值 %d 行' % (c, nm, nav_counts.get(c, 0)))
            if not cands:
                print('        名册里找不到字面对口的 ⇒ 只能删掉这条静态映射，交给 agent/映射表')
        pp = os.path.join(OUT_DIR, 'static-map-repropose.txt')
        io.open(pp, 'w', encoding='utf-8').write('\n'.join(
            '%s\t%s\t%s\t%s' % (s, oc, on, '; '.join('%s:%s(%d行)' % (c, n, nav_counts.get(c, 0))
                                                    for c, n in cs))
            for s, oc, on, _l, cs in propose_repoints(buckets, by_code, nav_counts)) + '\n')
        print('[建议清单] %s' % pp)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
