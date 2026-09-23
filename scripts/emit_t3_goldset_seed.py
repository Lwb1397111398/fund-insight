# -*- coding: utf-8 -*-
"""生成 T3 语义判据的**金标集种子表**（只读，不写库、不调 LLM）。

为什么要这个：`sector_fund_agent.py:31` 的及格线 `T3_PASS_SCORE = 70` 是当年拍的，
从没对过答案。第 27 轮量过一次后确定了两件事：
  ① 不能拿"名字字面重叠"当闸门 —— AI→人工智能ETF、海力士→芯片ETF 这类**语义对**会被误杀；
  ② 也确实有 `应用→新能源ETF(90)`、`液冷→电池ETF(75)` 这种可疑配对拿高分。
所以要标的是**人判的"合不合适"**，不是字符串相似度。本脚本把待标的行连证据一起摊出来：

    python scripts/emit_t3_goldset_seed.py            # 写到 docs/迭代计划/run-2026-09-20/
    # 列：板块 / 代码 / 名册官方名 / t3 分 / 是不是代理 / 审查状态 / 字面是否相关 / (人填)判定

只读镜像；名册用本地缓存 `data/_roster_full.json`（没有就退回行上的 `fund_name`）。
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

# 默认输出**不入库**：第 35 轮 B 抓到原来的默认路径正好落在已提交的金标种子 CSV 上，
# 谁按文档裸跑一次命令，就把大家核对用的基准销毁一次（同一族病只改了兄弟脚本）。
OUT_DEFAULT = os.path.join(ROOT, 'data', 't3-goldset-seed.csv')


def find_t3(node):
    """在 evidence 的任意嵌套形状里找带 `score` 的 T3 记录。

    不写死 `tiers` 是 dict 还是 list：上一版探针就按 dict 取，当场被真实结构（list）打回。
    """
    out = []
    if isinstance(node, dict):
        low = {str(k).lower(): v for k, v in node.items()}
        if 'score' in low and ('t3' in str(low.get('stage', '')).lower()
                               or 'proxy' in low or 'suitable' in low):
            out.append(node)
        for v in node.values():
            out += find_t3(v)
    elif isinstance(node, list):
        for v in node:
            out += find_t3(v)
    return out


def pick_own_t3(records, code):
    """取**属于这一行那只基金**的 T3 记录；找不到返回 None，绝不兜底给第一条。

    T3 是逐个候选评估的（一行多条），`records[0]` 通常是别的候选的分数。
    第 27 轮 D-MAJOR-2 实测：52 行里 29 行发布出去的分不属于本行基金
    （例：`核聚变/159525` 自己那只是 90.0，却被写成第一条 `562350` 的 30.0）。
    这跟 claim_sim 那次"47 条其实是 10 条"是同一个错：行级字段配了别行的子记录。
    """
    target = str(code or '').strip()
    if not target:
        return None                      # 空代码不许与"同样为空"的记录配对
    for rec in records or []:
        if str((rec or {}).get('code') or '').strip() == target:
            return rec
    return None


def literal_related(sector, official_name):
    from src.services.sector_identity_audit import contains_core, cjk_core, sector_core
    core = sector_core(sector) or sector
    if not official_name:
        return False
    if any(contains_core(official_name, v) for v in (core, sector) if v):
        return True
    return bool(set(cjk_core(core)) & set(cjk_core(official_name)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=OUT_DEFAULT)
    ap.add_argument('--roster', default=os.path.join(ROOT, 'data', '_roster_full.json'))
    args = ap.parse_args()

    pin_local_sqlite(use_mirror_default=True)
    from src.models.database import SectorFundMapping, SessionLocal

    roster = {}
    if os.path.exists(args.roster):
        raw = json.load(io.open(args.roster, encoding='utf-8'))
        roster = (raw.get('by_code') if 'by_code' in raw else raw) or {}

    db = SessionLocal()
    try:
        rows = db.query(SectorFundMapping).all()
    finally:
        db.close()

    out, no_t3, unpaired = [], 0, 0
    for r in rows:
        try:
            ev = json.loads(r.evidence or '{}')
        except Exception:
            ev = {}
        t3s = find_t3(ev.get('tiers', ev))
        if not t3s:
            no_t3 += 1
            continue
        t3 = pick_own_t3(t3s, r.fund_code)
        if t3 is None:
            # 本行那只基金压根没被评过分：不能拿别的候选的分数顶，报出来让人看见
            unpaired += 1
            continue
        low = {str(k).lower(): v for k, v in t3.items()}
        score = low.get('score')
        if not isinstance(score, (int, float)):
            no_t3 += 1
            continue
        code = r.fund_code
        official = ((roster.get(code) or {}).get('name')
                    if isinstance(roster.get(code), dict) else roster.get(code)) or r.fund_name or ''
        official = (official or '').strip()
        state = ('老板已确认' if r.reviewed_by == 'owner'
                 else ('已审查(机器)' if r.reviewed else '待审'))
        out.append({'sector': r.sector_name, 'code': code, 'official_name': official,
                    't3_score': score,
                    'proxy': str(bool(low.get('proxy'))).lower(),
                    'suitable': str(low.get('suitable')).lower(),
                    'review_state': state,
                    'literal_related': str(literal_related(r.sector_name, official)).lower(),
                    'human_label': ''})

    out.sort(key=lambda d: (-d['t3_score'], d['sector']))
    cols = ['sector', 'code', 'official_name', 't3_score', 'proxy', 'suitable',
            'review_state', 'literal_related', 'human_label']
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with io.open(args.out, 'w', encoding='utf-8', newline='') as f:
        f.write(','.join(cols) + '\n')
        for d in out:
            f.write(','.join('"%s"' % str(d[c]).replace('"', '""') for c in cols) + '\n')

    hi = [d for d in out if d['t3_score'] >= 70]
    print('[种子] 待标 %d 行（另有 %d 行没有 t3 记录、%d 行的 evidence 里没有本行那只基金的记录）；'
          '≥70 分 %d 行' % (len(out), no_t3, unpaired, len(hi)))
    print('[口径] 名册=本地缓存 %s（%d 只）；库=本地镜像 data/fund_insight.db；本脚本不写库、不调 LLM'
          % (os.path.basename(args.roster), len(roster)))
    print('[提示] `human_label` 留空给你填：合适 / 勉强代理 / 不对。'
          '`literal_related` 只是参考列 —— AI→人工智能ETF 会是 false，但它是**对的**。')
    print('[产物] %s' % args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
