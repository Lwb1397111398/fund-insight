# -*- coding: utf-8 -*-
"""删除老板已批准的 6 个垃圾 `fund_info` 码（默认只预览，真删要确认词）。

为什么不是 `DELETE FROM fund_info WHERE fund_code IN (...)` 一行完事：
1. `sector_fund_mapping.fund_code` 有外键指向 `fund_info` ⇒ 每只垃圾码都挂着 1 行映射
   （实测镜像与生产各 6 行），不先处理这些行就删不掉，或删完留下指向空的映射行；
2. 生产与镜像的外键生效程度不同（SQLite 要 `PRAGMA foreign_keys=ON`，生产实测
   只有 3 条指向 fund_info 的外键）⇒ **同一份操作在两个库上的结果可能相反**：
   镜像会拒绝、生产会静默留下孤儿行。所以两边都必须显式按同一个顺序做，而不是"看运气"；
3. 这个仓库反复为"批量删除"付过账 ⇒ 一律：先备份被删的行、逐行回执、可回滚。

用法：
    python scripts/purge_junk_funds.py                      # 只读预检 + 出计划
    python scripts/purge_junk_funds.py --apply --confirm PURGE-JUNK
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

JUNK_CODES = ('603758', '600189', '152788', 'HYNX', 'SBSP76', 'ign')
CONFIRM_TOKEN = 'PURGE-JUNK'


def inspect(db, codes=JUNK_CODES):
    """每个码的现状：活预测数、净值行数、映射行数（含同板块是否还有别的可服务行）。"""
    import sqlalchemy as sa

    from src.models.database import FundHistory, FundInfo, Prediction, SectorFundMapping

    out = []
    for code in codes:
        info = db.query(FundInfo).filter(FundInfo.fund_code == code).first()
        live_pred = db.query(sa.func.count(Prediction.id)).filter(
            Prediction.fund_code == code, Prediction.is_deleted == False).scalar() or 0  # noqa: E712
        verified_pred = db.query(sa.func.count(Prediction.id)).filter(
            Prediction.fund_code == code, Prediction.is_deleted == False,        # noqa: E712
            Prediction.is_correct != None).scalar() or 0
        nav_rows = db.query(sa.func.count(FundHistory.fund_code)).filter(
            FundHistory.fund_code == code).scalar() or 0
        maps = db.query(SectorFundMapping).filter(
            SectorFundMapping.fund_code == code).all()
        siblings = []
        for m in maps:
            others = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == m.sector_name,
                SectorFundMapping.fund_code != code,
                SectorFundMapping.is_active == True).count()      # noqa: E712
            siblings.append({'mapping_id': m.id, 'sector': m.sector_name,
                             'is_active': bool(m.is_active), 'reviewed': bool(m.reviewed),
                             'other_rows_for_sector': others})
        out.append({
            'fund_code': code, 'fund_name': (info.fund_name if info else None),
            'in_fund_info': info is not None, 'live_predictions': live_pred,
            'verified_predictions': verified_pred, 'nav_rows': nav_rows,
            'mappings': siblings,
        })
    return out


def plan(rows):
    """把现状变成可执行动作；任何一条不满足安全条件就整批拒绝。"""
    actions, blockers = [], []
    for r in rows:
        if not r['in_fund_info']:
            continue                          # 这个库里没有该码，跳过（生产/镜像可能不同）
        if r['live_predictions'] or r['nav_rows']:
            # 有活预测或有净值历史就不是"垃圾码"，一律不动 —— 之前评审也提醒过
            # "股票名挂在别人的基金码"那种行只能改名不能删（名下净值属于同码真基金）
            blockers.append('%s：活预测 %d / 净值 %d 行 ⇒ 不许删'
                            % (r['fund_code'], r['live_predictions'], r['nav_rows']))
            continue
        for m in r['mappings']:
            actions.append(('mapping', m['mapping_id'], r['fund_code'], m['sector'],
                            m['other_rows_for_sector']))
        actions.append(('fund_info', r['fund_code'], r['fund_name'], '', 0))
    return actions, blockers


def main():
    ap = argparse.ArgumentParser(description='删除 6 个垃圾 fund_info 码（默认只出计划）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--production', action='store_true', help='显式对生产执行（默认本地镜像）')
    ap.add_argument('--codes', help='逗号分隔，覆盖默认那 6 个')
    args = ap.parse_args()

    codes = tuple(c.strip() for c in (args.codes or '').split(',') if c.strip()) or JUNK_CODES
    if args.apply and args.confirm != CONFIRM_TOKEN:
        print('[abort] --apply 需要 --confirm %s' % CONFIRM_TOKEN)
        return 4

    if args.production:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        if not url.lower().startswith(('postgres', 'postgresql')):
            print('[abort] --production 要求 .env 指向 PostgreSQL')
            return 4
        print('[target] 线上生产库')
    else:
        from _db_guard import pin_local_sqlite
        pin_local_sqlite(use_mirror_default=True)
        print('[target] 本地镜像库')

    import sqlalchemy as sa

    from src.models.database import FundInfo, SectorFundMapping, SessionLocal

    db = SessionLocal()
    try:
        rows = inspect(db, codes)
        for r in rows:
            print('  %-8s %-14s fund_info=%s 活预测=%d 已判=%d 净值=%d 映射=%d%s'
                  % (r['fund_code'], (r['fund_name'] or '')[:14], r['in_fund_info'],
                     r['live_predictions'], r['verified_predictions'], r['nav_rows'],
                     len(r['mappings']),
                     '' if not r['mappings'] else '  ' + json.dumps(
                         [m['sector'] for m in r['mappings']], ensure_ascii=False)))
        actions, blockers = plan(rows)
        if blockers:
            print('[abort] 有 %d 个码不满足"零活预测 + 零净值"，整批不动：%s'
                  % (len(blockers), blockers))
            return 5
        print('[计划] 将删除 %d 行映射 + %d 行 fund_info（先删映射再删档案，顺序按外键来）'
              % (sum(1 for a in actions if a[0] == 'mapping'),
                 sum(1 for a in actions if a[0] == 'fund_info')))
        for kind, ident, code, sector, others in actions:
            print('   - %-9s %s %s%s' % (kind, code, ident,
                                         ('（板块 %s，同板块另有 %d 行可服务）' % (sector, others))
                                         if kind == 'mapping' else ''))
        if not args.apply:
            print('\ndry-run：未写库。真删：--apply --confirm %s' % CONFIRM_TOKEN)
            return 2

        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        dump = os.path.join(ROOT, 'backup', 'purge-junk-%s.json' % stamp)
        os.makedirs(os.path.dirname(dump), exist_ok=True)
        # 备份必须先落盘再动手：删完就没有第二次机会（本仓库为"删了才发现要回滚"付过账）
        rows_bak = []
        for c in codes:
            fi = db.query(FundInfo).filter(FundInfo.fund_code == c).first()
            if fi:
                rows_bak.append({'table': 'fund_info',
                                 'row': {col.name: getattr(fi, col.name)
                                         for col in FundInfo.__table__.columns}})
            for m in db.query(SectorFundMapping).filter(SectorFundMapping.fund_code == c).all():
                rows_bak.append({'table': 'sector_fund_mapping',
                                 'row': {col.name: getattr(m, col.name)
                                         for col in SectorFundMapping.__table__.columns}})
        io.open(dump, 'w', encoding='utf-8').write(
            json.dumps(rows_bak, ensure_ascii=False, indent=1, default=str))
        print('[ok] 被删的行已备份：%s（%d 行）' % (dump, len(rows_bak)))

        for kind, ident, code, _sector, _o in actions:
            if kind == 'mapping':
                db.query(SectorFundMapping).filter(
                    SectorFundMapping.id == ident).delete()
            else:
                db.query(FundInfo).filter(FundInfo.fund_code == code).delete()
        db.commit()
        after = inspect(db, codes)
        left = [r for r in after if r['in_fund_info'] or r['mappings']]
        if left:
            print('[warn] 仍有残留：%s' % [(r['fund_code'], len(r['mappings'])) for r in left])
            return 3
        print('[ok] 6 个码的 fund_info 与其映射行已删除；备份可回滚（按备份 JSON 重新 insert）')
        return 0
    except Exception as exc:
        db.rollback()
        print('[fail] 已回滚：%s' % exc)
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
