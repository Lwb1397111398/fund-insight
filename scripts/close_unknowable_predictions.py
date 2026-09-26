# -*- coding: utf-8 -*-
"""把**永远判不出来**的预测收进回收站（默认只出计划；真跑要确认词；能一键还原）。

要解决的问题：有些预测挂的标的已经停止发布净值（2026-09-26 生产实测：`003033` 库里
20 行净值、末条 2020-12-08；`002413` 末条 2023-07-07，而预测窗口都是 2026-08~09），
验证器每次问都拿到"这段没有"⇒ 这批预测会永远留在"到期未判"里，老板每次点验证都白跑一遍。

**为什么不能直接按"到期很久了"关**（第 12 轮 MAJOR-3 撤掉过纯日历推断，这里不再放回来）：
一把脚本必须同时拿到两条证据才动手 ——
① **现场再问一次数据源**：`get_fund_history_range` 对这个窗口答 **0 条**才算"问过且没有"；
   接口抛错／没答话（`None`）一律**不放行**（第 10 轮 B-1 那一课：一次抖动不许被记成事实）；
② **本地库里这只代码最后一条净值早于窗口起点**：否则它只是"我们几天没同步"，不是"产品停更"。
两条都在同一个窗口上成立，才关闭；只中一条 ⇒ 原样报"还在等"。

用法：
    python scripts/close_unknowable_predictions.py                      # 本地镜像：只读预检 + 出计划
    python scripts/close_unknowable_predictions.py --production         # 生产：只读预检 + 出计划
    python scripts/close_unknowable_predictions.py --production --apply --confirm CLOSE-UNVERIFIABLE
    python scripts/close_unknowable_predictions.py --restore-from backup/close-unknowable-xxx.json

关闭走的是 `PredictionService.close_as_unverifiable`（与页面"归档"同一条咽喉：快照 → 归档 →
重算博主统计 → 写台账 → 提交），**不写 `is_correct`** ⇒ 既不进判对也不进判错，准确率不动。
"""
import argparse
import io
import json
import os
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

CONFIRM_TOKEN = 'CLOSE-UNVERIFIABLE'
BACKUP_DIR = os.path.join(ROOT, 'backup')


def _today_beijing():
    from src.services.prediction_lifecycle import current_as_of
    return current_as_of()


def plan(db, today):
    """列出候选：到期未判 && 源端这个窗口答 0 条 && 库里这只代码最后一条净值早于窗口起点。"""
    from src.models.database import FundHistory, Prediction
    from src.services.prediction_lifecycle import (close_as_stale_target_note,
                                                   nav_cannot_cover_window)

    rows = (db.query(Prediction)
            .filter(Prediction.is_deleted == False,          # noqa: E712
                    Prediction.is_correct.is_(None),
                    Prediction.target_date.isnot(None),
                    Prediction.target_date <= today,
                    Prediction.prediction_type != 'flat')
            .order_by(Prediction.target_date.asc())
            .all())
    out, source_cache, skipped = [], {}, []
    for p in rows:
        code = (p.fund_code or '').strip()
        if not code:
            continue
        start, end = p.prediction_date, p.target_date
        # 同一窗口只问一次：数据源对"这段有没有"答两次的结论不会变，白打接口还有限流风险
        probe_key = (code, start, end)
        if probe_key not in source_cache:
            try:
                from src.fund.fund_api import fund_api
                got = fund_api.get_fund_history_range(code, start, end)
            except Exception as exc:
                got = None
                print('[warn] %s 问数据源抛错：%s ⇒ 这一轮一律不关' % (code, exc))
            source_cache[probe_key] = got
        got = source_cache[probe_key]
        if got is None:
            skipped.append((p.id, code, '数据源没答话（不算"没有"）'))
            continue
        newest = db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == code).order_by(FundHistory.nav_date.desc()).first()
        latest = newest[0] if newest else None
        if got or not nav_cannot_cover_window(latest, start):
            skipped.append((p.id, code,
                            '源端给了 %d 条或库里最后一条 %s 不早于窗口起点 %s ⇒ 还在等'
                            % (len(got), latest, start)))
            continue
        out.append({'prediction_id': p.id, 'fund_code': code, 'fund_name': p.fund_name,
                    'blogger_id': p.blogger_id, 'target_date': str(end),
                    'window_start': str(start), 'local_latest_nav': str(latest),
                    'source_rows': len(got),
                    'note': close_as_stale_target_note(p, latest, start)})
    return out, skipped


def _snapshot(db, ids):
    from src.models.database import Prediction
    cols = [c.key for c in Prediction.__table__.columns]
    rows = db.query(Prediction).filter(Prediction.id.in_(ids)).all()
    return [{c: (getattr(r, c).isoformat() if isinstance(getattr(r, c), (date, datetime))
                 else getattr(r, c)) for c in cols} for r in rows]


def apply_close(db, items, today):
    from src.services.prediction_service import PredictionService

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, 'close-unknowable-%s.json' % stamp)
    ids = [i['prediction_id'] for i in items]
    payload = {'created_at': datetime.now().isoformat(), 'as_of': str(today),
               'reason_kind': 'stale_target_no_source_history',
               'rows': _snapshot(db, ids), 'plan': items}
    with open(backup_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print('[备份] 写 %d 行原样 → %s' % (len(payload['rows']), os.path.basename(backup_path)))

    svc = PredictionService(db)
    done = 0
    for i in items:
        if svc.close_as_unverifiable(i['prediction_id'], i['note']):
            done += 1
            print('[已关闭] 预测 %s 标的 %s（窗口 %s~%s，源端 %d 条，库里末条净值 %s）'
                  % (i['prediction_id'], i['fund_code'], i['window_start'], i['target_date'],
                     i['source_rows'], i['local_latest_nav']))
        else:
            print('[没关] 预测 %s：行已不在活跃列表（或已被别人处理）' % i['prediction_id'])
    print('[回执] 关闭 %d / 计划 %d 行；还原命令：python scripts/close_unknowable_predictions.py '
          '--restore-from %s' % (done, len(items), backup_path))
    return backup_path


def restore(db, path, apply_it):
    from src.services.prediction_service import PredictionService

    with open(path, encoding='utf-8') as fh:
        payload = json.load(fh)
    ids = [r['id'] for r in payload.get('rows') or []]
    print('[计划] 备份里有 %d 行，还原 = 把它们从回收站放回活跃列表（%s）'
          % (len(ids), '真还原' if apply_it else 'dry-run，一行都不动'))
    if not apply_it:
        return 3
    svc = PredictionService(db)
    done = sum(1 for i in ids if svc.restore_prediction(i))
    print('[还原] %d / %d 行已回到活跃列表' % (done, len(ids)))
    return 0


def main():
    ap = argparse.ArgumentParser(description='关闭"标的已停更、永远判不出来"的预测（默认只出计划）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--production', action='store_true', help='显式对生产执行')
    ap.add_argument('--restore-from', help='按备份 JSON 还原（默认 dry-run）')
    ap.add_argument('--confirm-restore', help='还原到生产时必须等于 %s' % CONFIRM_TOKEN)
    args = ap.parse_args()

    # 确认词排在**连库与取数之前**（这条仓库规矩：用法错不该先付一次网络与数据库的代价，
    # 更不该让人以为"给了 --apply 就会写"）：
    if args.apply and not args.restore_from and (args.confirm or '') != CONFIRM_TOKEN:
        print('[abort] 真关闭要 --confirm %s（默认只出计划）' % CONFIRM_TOKEN)
        return 4

    if args.production:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        from _db_guard import _db_hosts, _is_the_production_host, db_kind
        hosts = _db_hosts(url or '')
        if not (url.lower().startswith(('postgres', 'postgresql'))
                and hosts and all(_is_the_production_host(h) for h in hosts)):
            print('[abort] --production 要求连接**就是本项目那台线上库**，现在是 %s'
                  % db_kind(url))
            return 4
        print('[target] %s' % db_kind(url))
    else:
        from _db_guard import db_kind, pin_local_sqlite
        print('[target] %s' % db_kind(pin_local_sqlite(use_mirror_default=True)))

    from src.models.database import SessionLocal
    from src.services.prediction_lifecycle import current_as_of

    db = SessionLocal()
    try:
        today = current_as_of()
        if args.restore_from:
            if args.production and args.confirm_restore != CONFIRM_TOKEN:
                print('[abort] 还原到生产要 --confirm-restore %s' % CONFIRM_TOKEN)
                return 4
            return restore(db, args.restore_from, args.apply)

        items, skipped = plan(db, today)
        print('[计划] 到期未判里可以判定"永远问不出来"的：%d 条；仍在等的：%d 条'
              % (len(items), len(skipped)))
        for i in items:
            print('  [可关] 预测 %s %s(%s) 窗口 %s~%s ⇒ 源端 %d 条、库里末条净值 %s'
                  % (i['prediction_id'], i['fund_code'], i['fund_name'], i['window_start'],
                     i['target_date'], i['source_rows'], i['local_latest_nav']))
        for pid, code, why in skipped:
            print('  [还在等] 预测 %s %s：%s' % (pid, code, why))
        if not items:
            print('[结论] 没有要关的行（要么都判出来了，要么证据不齐）')
            return 0
        if not args.apply:
            print('[dry-run] 一行都没动。真执行加 --apply --confirm %s' % CONFIRM_TOKEN)
            return 2
        apply_close(db, items, today)
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
