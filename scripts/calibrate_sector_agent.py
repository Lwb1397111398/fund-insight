# -*- coding: utf-8 -*-
"""金标集标定：量化 agent 到底比"沿用旧映射"好多少。

为什么必须有：置信度阈值（0.80 / 0.68）与 T3 及格线如果是拍脑袋定的，
"准确率提升"就只是感觉。本脚本用一份人工写死的金标集
（`docs/迭代计划/gold-standard-50.json`，每条给出可接受的基金名关键词与
 direct/proxy/no_fund 判定）算混淆矩阵，并同时给出 **no-op 基线**
（什么都不做、沿用库里现有映射的得分）——只有 agent 明显赢过 no-op 才算有效。

用法：
    DATABASE_URL="sqlite:///<本地镜像库>" python scripts/calibrate_sector_agent.py --from-db
    python scripts/calibrate_sector_agent.py --live --limit 10      # 现场跑 agent
退出码：0=达标；1=未达标（precision<0.90 或 相对 no-op 提升<0.15 或 有误自动审查）
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402

GOLD_PATH = os.path.join(ROOT, "docs", "迭代计划", "gold-standard-50.json")
PRECISION_FLOOR = 0.90
UPLIFT_FLOOR = 0.15


def load_gold():
    with io.open(GOLD_PATH, encoding='utf-8') as f:
        return json.load(f)


def hits(expected, name):
    name = name or ''
    return any(tok and tok in name for tok in expected)


def db_answers(sectors):
    from src.models.database import SessionLocal, SectorFundMapping
    db = SessionLocal()
    try:
        out = {}
        for s in sectors:
            row = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == s,
                SectorFundMapping.is_active == True    # noqa: E712
            ).order_by(SectorFundMapping.reviewed.desc(),
                       SectorFundMapping.id.asc()).first()
            if row:
                out[s] = {'code': row.fund_code, 'name': row.fund_name or '',
                          'reviewed': bool(row.reviewed),
                          'source': row.match_source or 'legacy',
                          'confidence': row.confidence}
        return out
    finally:
        db.close()


def live_answers(sectors):
    from src.services.sector_fund_agent import resolve_sector_fund
    out = {}
    for s in sectors:
        d = resolve_sector_fund(s, budget_ms=45000)
        c = d.chosen
        out[s] = {'code': c.code if c else None,
                  'name': (c.display_name if c else ''),
                  'reviewed': bool(d.auto_reviewable),
                  'source': 'agent', 'confidence': d.confidence,
                  'status': d.status}
    return out


def score(entries, answers, label):
    graded = []
    for item in entries:
        sector = item['sector']
        ans = answers.get(sector)
        name = (ans or {}).get('name') or ''
        code = (ans or {}).get('code')
        if item['kind'] == 'no_fund':
            ok = (code is None) or (not hits(item['expect_any'], name) and not name)
            graded.append((sector, bool(ok), name or '(空)', item['kind']))
            continue
        ok = bool(code) and hits(item['expect_any'], name)
        graded.append((sector, ok, name or '(无映射)', item['kind']))
    good = sum(1 for g in graded if g[1])
    total = len(graded)
    print('\n[%s] 命中 %d/%d = %.1f%%' % (label, good, total, 100.0 * good / max(1, total)))
    for sector, ok, name, kind in graded:
        if not ok:
            print('   [X] %-10s 期望 %s / 实际 %s' % (sector, kind, name[:34]))
    return good, total, graded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-db', action='store_true', help='用库里现有答案评分（跑批后）')
    ap.add_argument('--live', action='store_true', help='现场跑 agent 评分')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--report', default=os.path.join(
        ROOT, 'docs', '迭代计划', 'run-2026-09-20', 'calibration.md'))
    args = ap.parse_args()

    _db_guard.pin_local_sqlite()
    gold = load_gold()
    if args.limit:
        gold = gold[:args.limit]
    sectors = [g['sector'] for g in gold]

    legacy = db_answers(sectors)          # no-op 基线：老板看到的现状
    agent = live_answers(sectors) if args.live else legacy
    if not args.live and not args.from_db:
        print('提示：--from-db 用库内答案评分，--live 现场跑 agent；本次按 --from-db 处理')

    lg, lt, _ = score(gold, legacy, 'no-op 基线（沿用现有映射）')
    ag, at, graded = score(gold, agent, 'agent 结果')

    proxy_items = [g for g in gold if g['kind'] == 'proxy']
    proxy_kept = sum(1 for g in proxy_items
                     if hits(g['expect_any'], (agent.get(g['sector']) or {}).get('name') or ''))
    wrong_auto = 0
    for g in gold:
        a = agent.get(g['sector']) or {}
        if a.get('reviewed') and not hits(g['expect_any'], a.get('name') or ''):
            wrong_auto += 1
            print('   !! 金标判错配却被自动置已审查：%s → %s' % (g['sector'], a.get('name')))

    noop_p = lg / max(1, lt)
    agent_p = ag / max(1, at)
    lines = [
        '# 金标集标定报告', '',
        '- 金标条目：%d（其中有意代理 %d 条，agent 保住 %d 条）' % (len(gold), len(proxy_items), proxy_kept),
        '- no-op 基线 precision：**%.1f%%**' % (100 * noop_p),
        '- agent precision：**%.1f%%**' % (100 * agent_p),
        '- 相对提升：%.1f 个百分点（门禁要求 ≥%.0f）' % (
            100 * (agent_p - noop_p), 100 * UPLIFT_FLOOR),
        '- 金标判错配却被自动 reviewed：%d 条（门禁要求 0）' % wrong_auto,
    ]
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with io.open(args.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines))

    ok = agent_p >= PRECISION_FLOOR and (agent_p - noop_p) >= UPLIFT_FLOOR and wrong_auto == 0
    print('\n判定：%s' % ('达标' if ok else '未达标'))
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
