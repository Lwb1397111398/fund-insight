# -*- coding: utf-8 -*-
"""导出"已修好的板块映射"给生产库**定向回写**用（不导整库）。

为什么不用现成的导入接口：`/api/database/import` 是整库覆盖（要 `ENABLE_DATABASE_IMPORT`
+ 确认头），拿它回写 145 行等于把线上其它表一起赌上去。这里只导出映射表与其
外键依赖（`fund_info` 最小档案），生产侧走逐行 PUT（有身份体检与审查门保护），
失败可重试、可核对，不带任何"顺手覆盖"的口子。

    python scripts/export_repaired_mappings.py                # 生成清单
    python scripts/export_repaired_mappings.py --check        # 只校验既有清单
"""
import argparse
import hashlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import _db_guard  # noqa: E402

OUT = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20',
                   'prod-writeback-sector-mappings.json')

# 回写只带"结论性字段"；created_at/updated_at 由目标库自己盖
FIELDS = ('sector_name', 'fund_code', 'fund_name', 'keywords', 'is_active', 'reviewed',
          'match_source', 'match_kind', 'confidence', 'verified_at', 'verify_message',
          'llm_reason', 'is_fetchable', 'evidence', 'reviewed_by', 'owner_locked')


def build(db):
    from src.models.database import SectorFundMapping, FundInfo
    rows = db.query(SectorFundMapping).order_by(SectorFundMapping.id).all()
    mappings = []
    for r in rows:
        item = {'id': r.id}
        for f in FIELDS:
            v = getattr(r, f, None)
            item[f] = v.isoformat(sep=' ') if hasattr(v, 'isoformat') else v
        mappings.append(item)
    codes = sorted({m['fund_code'] for m in mappings if m['fund_code']})
    known = {c for (c,) in db.query(FundInfo.fund_code).filter(
        FundInfo.fund_code.in_(codes)).all()} if codes else set()
    payload = {
        'table': 'sector_fund_mapping',
        'generated_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
        'fields': list(FIELDS),
        'row_count': len(mappings),
        'mappings': mappings,
        # 外键依赖：新代码在目标库没档案就先补档案，否则 PUT 直接 IntegrityError
        'fund_info_needed': sorted(set(codes) - known),
        'sha256': hashlib.sha256(json.dumps(
            mappings, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16],
    }
    return payload


def check(data, db):
    """清单与库现状对不上就报错：防止"导出后我又改了库"这种静默错位。"""
    from src.models.database import SectorFundMapping
    problems = []
    if data.get('table') != 'sector_fund_mapping':
        problems.append('table 字段不对')
    local = {r.sector_name: r.fund_code for r in db.query(SectorFundMapping).all()}
    remote = {m['sector_name']: m['fund_code'] for m in data.get('mappings', [])}
    if local != remote:
        diff = {k: (local.get(k), remote.get(k))
                for k in set(local) | set(remote) if local.get(k) != remote.get(k)}
        problems.append('清单与本地库已不一致，%d 个板块：%s' % (len(diff), list(diff)[:5]))
    if data.get('row_count') != len(data.get('mappings', [])):
        problems.append('row_count 与 mappings 长度不符')
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只校验既有清单，不重写')
    args = ap.parse_args()
    url = _db_guard.pin_local_sqlite()
    assert url.startswith('sqlite'), url
    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        if args.check:
            data = json.load(io.open(OUT, encoding='utf-8'))
            problems = check(data, db)
            print('[校验] %s' % ('通过（%d 行，指纹 %s）'
                                 % (data['row_count'], data['sha256']) if not problems
                                 else '不通过：%s' % '; '.join(problems)))
            return 1 if problems else 0
        data = build(db)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with io.open(OUT, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        unservable = sum(1 for m in data['mappings'] if m['is_fetchable'] is False)
        print('[导出] %s' % OUT)
        print('       %d 行（不可服务 %d、老板锁定 %d），需补基金档案 %d 只，指纹 %s'
              % (data['row_count'], unservable,
                 sum(1 for m in data['mappings'] if m['owner_locked']),
                 len(data['fund_info_needed']), data['sha256']))
        print('       回写前先在生产跑 alembic 到 add_sector_mapping_keywords，'
              '再逐行走 PUT /api/config/sector-mappings/{id}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
