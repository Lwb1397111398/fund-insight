# -*- coding: utf-8 -*-
"""修掉一次**我自己造成的**污染：重放脚本误写进了本地镜像库。

事故经过（必须写在这里，别只留在聊天记录里）：
`scripts/replay_verifications_on_copy.py` 首版在 `pin_local_sqlite(use_mirror_default=True)` 之后就
`from src.models.database import ...` —— engine 在 import 那一刻已经绑定到
`data/fund_insight.db`，之后再把 `os.environ['DATABASE_URL']` 改指副本**是无效的**。
于是那两次号称"在副本上重放"的运行，实际把镜像库里 88 条已验证预测的
结论字段清掉重算了（`verified_at >= 2026-09-21 20:43` 就是那两批）。

为什么能从快照修回来：老板 09-20 导出的 JSON 里有这 88 条的完整原值（逐字段实测齐全），
其中 9 条在导出里本来就是未验证（它们是今天 19:3x 那轮正常验证验出来的）——
这 9 条先还原成"未验证"，再由正常验证路径重验，不手工拼结论。

每条被改的预测都写 `prediction_change_logs`，博主统计用 `recalculate_blogger_stats` 重算。

用法：
    python scripts/repair_replay_side_effects.py                    # 只报告
    python scripts/repair_replay_side_effects.py --apply            # 还原 + 重验那 9 条
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

from _db_guard import pin_local_sqlite  # noqa: E402

pin_local_sqlite(use_mirror_default=True)

from src.models.database import Prediction  # noqa: E402
from src.services.prediction_change_log_service import (  # noqa: E402
    add_prediction_change_log, snapshot_prediction)

EXPORT = os.path.join(os.path.expanduser('~'), 'Downloads',
                      'fund_insight_export_2026-09-20.json')
CUTOFF = datetime(2026, 9, 21, 20, 43)
# 带 run_id 才能整批回滚（scripts/restore_prediction_batch.py --run-id 就是这个用途）
RUN_ID = 'replay-repair-20260921a'
DATE_FIELDS = ('start_nav_date', 'end_nav_date', 'current_nav_date',
               'last_verify_date', 'verified_at')
# 还原清单不再手抄：第 15 轮漂移闸门抓到这条手抄清单**漏了 `verify_score`**，
# 于是 88 行还原后 11 行的标量分数停在误写值上（`is_correct=True` 配 49 分，
# 台账那条明明是 100），博主平均分一起偏。清单只有 restore_prediction_batch 一处。
from restore_prediction_batch import RESTORE_FIELDS  # noqa: E402


def _coerce(field, value):
    if value is None or field not in DATE_FIELDS:
        return value
    text = str(value).replace('T', ' ')
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return value
    if field.endswith('_date') and not field.endswith('_at'):
        return out.date() if isinstance(out, datetime) else out
    return out


def main():
    ap = argparse.ArgumentParser(description='还原被误写的镜像库结论')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--reverify', action='store_true',
                    help='还原后把"导出里本来就未验证"的那些走正常验证路径重验')
    args = ap.parse_args()

    if not os.path.exists(EXPORT):
        print('[abort] 找不到导出快照：%s' % EXPORT)
        return 4
    snapshots = {int(x['id']): x for x in json.load(io.open(EXPORT, encoding='utf-8'))
                 .get('predictions') or [] if str(x.get('id', '')).isdigit()}

    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(Prediction).filter(Prediction.verified_at >= CUTOFF).all()
        print('[受影响] verified_at >= %s：%d 条' % (CUTOFF, len(rows)))
        missing = [r.id for r in rows if r.id not in snapshots]
        if missing:
            print('[abort] 这些预测在导出里没有快照，不能盲修：%s' % missing[:20])
            return 5
        pending_in_export = [r.id for r in rows if snapshots[r.id].get('is_correct') is None]
        print('   导出里当时已有结论的 %d 条（直接还原）；'
              '导出里本来就未验证的 %d 条（还原成未验证后需重验）：%s'
              % (len(rows) - len(pending_in_export), len(pending_in_export), pending_in_export))
        flipped = []
        for r in rows:
            snap = snapshots[r.id]
            was = (r.is_correct, r.verify_score)
            now = (snap.get('is_correct'), snap.get('verify_score'))
            if was[0] != now[0]:
                flipped.append((r.id, r.fund_code, was, now))
        print('   结论方向被改掉、需要还原的：%d 条' % len(flipped))
        for row in flipped[:15]:
            print('      id=%-5s %s 当前 correct/score=%s/%s → 快照 %s/%s'
                  % (row[0], row[1], row[2][0], row[2][1], row[3][0], row[3][1]))
        if not args.apply:
            print('\n未写库。确认后用 --apply --reverify 执行。')
            return 0

        touched_bloggers = set()
        for r in rows:
            snap = snapshots[r.id]
            before = snapshot_prediction(r)
            for f in RESTORE_FIELDS:
                if f in snap:
                    setattr(r, f, _coerce(f, snap.get(f)))
            r.next_verify_date = None
            add_prediction_change_log(db, r, action='replay_repair', source='maintenance',
                                      before_state=before, run_id=RUN_ID)
            touched_bloggers.add(r.blogger_id)
        db.commit()
        print('[applied] 已还原 %d 条' % len(rows))

        if args.reverify and pending_in_export:
            from src.services.prediction_verify_service import PredictionVerifyService
            service = PredictionVerifyService(db)
            ok = 0
            for pid in pending_in_export:
                res = service.verify_prediction(pid)
                if res.get('success') and not res.get('skipped'):
                    ok += 1
                else:
                    print('   重验 id=%s：%s' % (pid, (res.get('message') or '')[:90]))
            print('[reverify] %d 条走正常路径重验，成功 %d 条' % (len(pending_in_export), ok))

        from src.utils.blogger_stats import recalculate_blogger_stats
        for blogger_id in touched_bloggers:
            if blogger_id:
                recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()

        # 还原之后必须自查"标量与台账是否还打脸"：首版就是漏了这一步，
        # 还原清单少一个 verify_score 也照样报"已还原 88 条"（第 15 轮 MAJOR）。
        from resync_verdict_scalars import find_desynced
        repaired = db.query(Prediction).filter(Prediction.id.in_([r.id for r in rows])).all()
        still, manual = find_desynced(repaired)
        for p, field, have, want in still:
            print('[残留] id=%s 的 %s 仍是 %s（应为 %s）' % (p.id, field, have, want))
        for p, why in manual:
            print('[需人工] id=%s：%s' % (p.id, why))
        if still or manual:
            print('[fail] 还原后还有 %d 行字段互相矛盾，别把这句当"修好了"'
                  % (len(still) + len(manual)))
            return 6

        yes = db.query(Prediction).filter(Prediction.is_deleted == False,
                                          Prediction.is_correct == True).count()
        no = db.query(Prediction).filter(Prediction.is_deleted == False,
                                         Prediction.is_correct == False).count()
        print('[核对] 全库 is_correct=True %d、False %d' % (yes, no))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
