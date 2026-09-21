# -*- coding: utf-8 -*-
"""只读探针：把 145 个板块名的核心词/拒绝表打出来，并对 flagged 行试算候选 ETF。"""
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import _db_guard  # noqa: E402

_db_guard.pin_local_sqlite()   # 生产 Supabase 连接串在本进程一律作废（只读探针，不写库）

from src.models.database import SessionLocal, SectorFundMapping  # noqa: E402
from src.services.sector_identity_audit import (  # noqa: E402
    sector_core, etf_candidates, sector_core_names, normalize_sector_text)
from src.fund.fund_api import fund_api  # noqa: E402

db = SessionLocal()
try:
    rows = db.query(SectorFundMapping).order_by(SectorFundMapping.id).all()
    fund_api.load_fund_roster()
    cores = sector_core_names(db)
    table = []
    for m in rows:
        table.append({'id': m.id, 'sector': m.sector_name,
                      'norm': normalize_sector_text(m.sector_name),
                      'core': sector_core(m.sector_name),
                      'code': m.fund_code, 'name': m.fund_name,
                      'reviewed': bool(m.reviewed)})
    with_core = [t for t in table if t['core']]
    print('[核心词] %d 有 / %d 拒绝，共 %d 行' % (len(with_core), len(table) - len(with_core), len(table)))
    print('[拒绝] %s' % ', '.join('%s(%s)' % (t['sector'], t['norm']) for t in table if not t['core']))

    from src.services.sector_identity_audit import sector_relevance  # noqa: E402
    flagged = []
    for t in table:
        official = (fund_api.get_fund_domain_name(t['code']).get('name')
                    or t['name'] or '')
        t['official'] = official
        if not sector_relevance(t['sector'], official):
            cands = etf_candidates(t['sector'], db=db, all_cores=cores, limit=6)
            flagged.append((t, cands))
    print('[相关性存疑] %d 条' % len(flagged))
    for t, cands in flagged:
        print('   id %-4s %-10s %-8s %-18s 核心词=%-8s 候选=%s'
              % (t['id'], t['sector'], t['code'], t['official'], t['core'] or '-',
                 ['%s %s' % (c['code'], c['name']) for c in cands] or '无'))
    path = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20', 'core-matcher-golden.json')
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump({'sectors': [{k: v for k, v in t.items() if k != 'official'} | {
                       'official_name_probe': t.get('official'),
                       'relevance_low': not sector_relevance(t['sector'], t.get('official') or '')}
                   for t in table],
                   'counts': {'rows': len(table), 'with_core': len(with_core),
                              'rejected': len(table) - len(with_core),
                              'relevance_low': len(flagged)}},
                  f, ensure_ascii=False, indent=1)
    # 文件名必须与测试读的完全一致：写成连字符会让测试读下划线那份，
    # "重新生成金标"这条退路实际空转，断言被冻结在旧快照上
    fixture = os.path.join(ROOT, 'tests', 'fixtures', 'core_matcher_golden.json')
    shutil.copy2(path, fixture)      # 测试读 fixtures 这份：文件缺失就不再静默 skip
    print('[金标] %s（+ %s）' % (path, fixture))
finally:
    db.close()
