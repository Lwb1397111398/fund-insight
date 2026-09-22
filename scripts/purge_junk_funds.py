# -*- coding: utf-8 -*-
"""删除老板已批准的 6 个垃圾 `fund_info` 码（默认只预览，真删要确认词，删完能还原）。

为什么不是 `DELETE FROM fund_info WHERE fund_code IN (...)` 一行完事：
1. 有外键指向 `fund_info` 的表**不止一张**：`sector_fund_mapping`、`fund_sync_retry`
   （第 24 轮评审指出上一版只认前一张）。不先按顺序处理就会删不掉、或删完留下指向空的行；
2. 外键在两库上的生效程度不同（SQLite 靠连接串里的 `PRAGMA foreign_keys=ON`，生产实测
   只有 3 条指向 fund_info 的外键）⇒ **同一份操作在两个库上的结果可能相反**：
   镜像会拒绝、生产会静默留下孤儿行。所以两边都要显式预检，而不是"看运气"；
3. 镜像与生产的现状本来就不同：2026-09-22 实测镜像这 6 个码的映射行是 **0 行**
   （早前的 `--rename-to-official` / 清理批次已经把它们清掉了），生产侧待单独预检
   ——上一版文件头写"实测镜像与生产各 6 行"，那是没在镜像上跑过预检就写下的数字。
4. 这个仓库反复为"批量删除"付过账 ⇒ 一律：先备份被删的行、逐行回执、`--restore-from` 可还原。

用法：
    python scripts/purge_junk_funds.py                       # 只读预检 + 出计划
    python scripts/purge_junk_funds.py --production          # 对生产做只读预检
    python scripts/purge_junk_funds.py --apply --confirm PURGE-JUNK
    python scripts/purge_junk_funds.py --restore-from backup/purge-junk-xxx.json
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
    """每个码的现状：活/软删预测数、净值行数、映射行（含署名与锁定）、同步重试行数。"""
    import sqlalchemy as sa

    from src.models.database import (FundHistory, FundInfo, FundSyncRetry, Prediction,
                                     SectorFundMapping)

    out = []
    for code in codes:
        info = db.query(FundInfo).filter(FundInfo.fund_code == code).first()
        live_pred = db.query(sa.func.count(Prediction.id)).filter(
            Prediction.fund_code == code, Prediction.is_deleted == False).scalar() or 0  # noqa: E712
        verified_pred = db.query(sa.func.count(Prediction.id)).filter(
            Prediction.fund_code == code, Prediction.is_deleted == False,        # noqa: E712
            Prediction.is_correct != None).scalar() or 0
        # 软删的预测也算：`predictions.fund_code` 上没有外键，删掉档案就是留一条
        # 指向不存在基金的"可恢复行"——回收站里打开它就是 500
        dead_pred = db.query(sa.func.count(Prediction.id)).filter(
            Prediction.fund_code == code, Prediction.is_deleted == True).scalar() or 0  # noqa: E712
        nav_rows = db.query(sa.func.count(FundHistory.fund_code)).filter(
            FundHistory.fund_code == code).scalar() or 0
        retry_rows = db.query(sa.func.count(FundSyncRetry.fund_code)).filter(
            FundSyncRetry.fund_code == code).scalar() or 0
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
                             'reviewed_by': m.reviewed_by, 'owner_locked': bool(m.owner_locked),
                             'other_rows_for_sector': others})
        out.append({
            'fund_code': code, 'fund_name': (info.fund_name if info else None),
            'in_fund_info': info is not None, 'live_predictions': live_pred,
            'verified_predictions': verified_pred, 'dead_predictions': dead_pred,
            'nav_rows': nav_rows, 'retry_rows': retry_rows, 'mappings': siblings,
        })
    return out


def plan(rows, allow_dead_predictions=False, allow_owner_rows=False):
    """把现状变成可执行动作；任何一条安全条件不满足就整批拒绝（而不是静默跳过那一行）。

    删除顺序 `retry → mapping → fund_info`：前两张表都对外键指向 `fund_info`。
    """
    actions, blockers = [], []
    for r in rows:
        if not r['in_fund_info'] and not r['mappings'] and not r['retry_rows']:
            continue                          # 这个库里根本没有该码（生产/镜像可能不同）
        if r['live_predictions'] or r['nav_rows']:
            # 有活预测或有净值历史就不是"垃圾码"：那种行只能改名不能删
            # （名下净值属于同码那只真基金，第 9 轮实测过）
            blockers.append('%s：活预测 %d / 净值 %d 行 ⇒ 不许删'
                            % (r['fund_code'], r['live_predictions'], r['nav_rows']))
            continue
        if r['dead_predictions'] and not allow_dead_predictions:
            blockers.append('%s：回收站里还有 %d 条软删预测挂着这个码（predictions 无外键，'
                            '删完就是指向空档案的可恢复行）⇒ 要过先加 --allow-dead-predictions'
                            % (r['fund_code'], r['dead_predictions']))
            continue
        owner_rows = [m for m in r['mappings'] if m['owner_locked'] or m['reviewed_by'] == 'owner']
        if owner_rows and not allow_owner_rows:
            blockers.append('%s：%d 行映射是老板署名/锁定的有意代理 ⇒ 机器不替他删，'
                            '要过先加 --allow-owner-rows' % (r['fund_code'], len(owner_rows)))
            continue
        for _ in range(r['retry_rows']):
            actions.append(('fund_sync_retry', r['fund_code'], r['fund_code'], '', 0))
        for m in r['mappings']:
            actions.append(('mapping', m['mapping_id'], r['fund_code'], m['sector'],
                            m['other_rows_for_sector']))
        if r['in_fund_info']:
            actions.append(('fund_info', r['fund_code'], r['fund_name'], '', 0))
    return actions, blockers


def _dump_rows(db, codes):
    """把将被删的每一行原样抄一份（列名→值），供 `--restore-from` 逐列写回。"""
    from src.models.database import FundHistory, FundInfo, FundSyncRetry, SectorFundMapping

    out = []
    for c in codes:
        for model, tag in ((FundInfo, 'fund_info'), (SectorFundMapping, 'sector_fund_mapping'),
                           (FundSyncRetry, 'fund_sync_retry'), (FundHistory, 'fund_history')):
            for row in db.query(model).filter(model.fund_code == c).all():
                out.append({'table': tag,
                            'row': {col.name: getattr(row, col.name)
                                    for col in model.__table__.columns}})
    return out


def _coerce_row(model, row):
    """把备份 JSON 里的字符串按列类型转回 Python 值。

    为什么要专门写这一小段：备份是 JSON，`datetime` 被 `default=str` 打成字符串，
    直接 `model(**row)` 还原会在 SQLite 上抛 "DateTime type only accepts ... date objects"。
    也就是说**上一版的"备份可回滚"在第一次真还原时就崩**——那条承诺从没跑过。
    """
    from datetime import date, datetime as _dt

    from sqlalchemy import Date, DateTime

    out = {}
    for col in model.__table__.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if isinstance(value, str) and isinstance(col.type, (DateTime, Date)):
            try:
                out[col.name] = (_dt if isinstance(col.type, DateTime) else date).fromisoformat(value)
            except ValueError:
                out[col.name] = value          # 转不动就原样交出去，让数据库自己报错而不是静默改值
        else:
            out[col.name] = value
    return out


def restore(db, payload_path):
    """按备份 JSON 逐列写回。备份里有什么就还原什么（含 id，映射行按原 id 回插）。"""
    from src.models.database import FundHistory, FundInfo, FundSyncRetry, SectorFundMapping

    models = {'fund_info': FundInfo, 'sector_fund_mapping': SectorFundMapping,
              'fund_sync_retry': FundSyncRetry, 'fund_history': FundHistory}
    rows = json.load(io.open(payload_path, encoding='utf-8'))
    restored, skipped = 0, 0
    for entry in rows:
        model = models.get(entry['table'])
        if model is None:
            print('[skip] 备份里有不认识的表 %s' % entry['table'])
            skipped += 1
            continue
        pk = list(model.__table__.primary_key.columns)[0]
        payload = _coerce_row(model, entry['row'])
        key = payload.get(pk.name)
        if key is not None and db.query(model).filter(getattr(model, pk.name) == key).first():
            skipped += 1                        # 已经在了就不重复插（幂等：还原跑两次不会翻倍）
            continue
        db.add(model(**payload))
        restored += 1
    db.commit()
    print('[ok] 按 %s 还原 %d 行（%d 行本就在库里，跳过）' % (payload_path, restored, skipped))
    return restored


def main():
    ap = argparse.ArgumentParser(description='删除 6 个垃圾 fund_info 码（默认只出计划）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--production', action='store_true', help='显式对生产执行（默认本地镜像）')
    ap.add_argument('--codes', help='逗号分隔，覆盖默认那 6 个')
    ap.add_argument('--allow-dead-predictions', action='store_true',
                    help='明知该码还挂着软删预测仍然删（默认拒绝）')
    ap.add_argument('--allow-owner-rows', action='store_true',
                    help='明知映射行是老板署名/锁定的仍然删（默认拒绝）')
    ap.add_argument('--restore-from', help='按备份 JSON 还原（删错了用它）')
    args = ap.parse_args()

    codes = tuple(c.strip() for c in (args.codes or '').split(',') if c.strip()) or JUNK_CODES

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

    from src.models.database import FundInfo, SectorFundMapping, SessionLocal

    db = SessionLocal()
    try:
        if args.restore_from:
            if not os.path.exists(args.restore_from):
                print('[abort] 备份文件不存在：%s' % args.restore_from)
                return 4
            return 0 if restore(db, args.restore_from) is not None else 1

        rows = inspect(db, codes)
        for r in rows:
            print('  %-8s %-14s fund_info=%s 活预测=%d 已判=%d 回收站=%d 净值=%d 重试=%d 映射=%d%s'
                  % (r['fund_code'], (r['fund_name'] or '')[:14], r['in_fund_info'],
                     r['live_predictions'], r['verified_predictions'], r['dead_predictions'],
                     r['nav_rows'], r['retry_rows'], len(r['mappings']),
                     '' if not r['mappings'] else '  ' + json.dumps(
                         [m['sector'] for m in r['mappings']], ensure_ascii=False)))
        actions, blockers = plan(rows, args.allow_dead_predictions, args.allow_owner_rows)
        if blockers:
            print('[abort] 有 %d 个码不满足安全条件，整批不动：%s' % (len(blockers), blockers))
            return 5
        print('[计划] 将删除 %d 行同步重试 + %d 行映射 + %d 行 fund_info'
              '（顺序按外键：retry/mapping → fund_info）'
              % (sum(1 for a in actions if a[0] == 'fund_sync_retry'),
                 sum(1 for a in actions if a[0] == 'mapping'),
                 sum(1 for a in actions if a[0] == 'fund_info')))
        for kind, ident, code, sector, others in actions:
            print('   - %-15s %s %s%s' % (kind, code, ident,
                                          ('（板块 %s，同板块另有 %d 行在用）' % (sector, others))
                                          if kind == 'mapping' else ''))
        if not args.apply:
            print('\ndry-run：未写库。真删：--apply --confirm %s' % CONFIRM_TOKEN)
            return 2
        if args.confirm != CONFIRM_TOKEN:
            print('[abort] --apply 需要 --confirm %s' % CONFIRM_TOKEN)
            return 4

        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        dump = os.path.join(ROOT, 'backup', 'purge-junk-%s.json' % stamp)
        os.makedirs(os.path.dirname(dump), exist_ok=True)
        # 备份必须先落盘再动手：删完就没有第二次机会（本仓库为"删了才发现要回滚"付过账）
        rows_bak = _dump_rows(db, codes)
        io.open(dump, 'w', encoding='utf-8').write(
            json.dumps(rows_bak, ensure_ascii=False, indent=1, default=str))
        print('[ok] 被删的行已备份：%s（%d 行，还原用 --restore-from 该文件）' % (dump, len(rows_bak)))

        for code in codes:
            from src.models.database import FundSyncRetry
            db.query(FundSyncRetry).filter(FundSyncRetry.fund_code == code).delete()
            db.query(SectorFundMapping).filter(
                SectorFundMapping.fund_code == code).delete()
            db.query(FundInfo).filter(FundInfo.fund_code == code).delete()
        db.commit()

        after = inspect(db, codes)
        left = [r for r in after if r['in_fund_info'] or r['mappings'] or r['retry_rows']]
        if left:
            print('[warn] 仍有残留：%s' % [(r['fund_code'], len(r['mappings'])) for r in left])
            return 3
        print('[ok] 这些码的 fund_info、映射行与同步重试行已删除；'
              '还原：python scripts/purge_junk_funds.py --restore-from %s' % dump)
        return 0
    except Exception as exc:
        db.rollback()
        print('[fail] 已回滚：%s' % exc)
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
