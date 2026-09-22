# -*- coding: utf-8 -*-
"""把标量结论字段同步回"台账最后一条说了什么"，修掉字段之间的互相打脸。

为什么要有这个脚本（第 15 轮漂移闸门抓出来的）：
`scripts/replay_verifications_on_copy.py --limit 200` 唯一那条"未解释漂移"是 id=3180
（`is_correct` True↔False，端点数值却完全一致）。顺着台账查下去，根因不是判据，
而是我自己写的还原脚本：`repair_replay_side_effects.py` 的 `RESTORE_FIELDS`
**漏了 `verify_score`**，于是 88 行里 11 行的标量分数停在误写值上 ——
`is_correct=True` 配 `verify_score=49`（台账那条明明是 100），
博主平均分也跟着偏。

同步方向只有一个：**台账（`verify_history` 末条）是当次验证的原场记录**，
标量字段是它的投影。投影和原场不一致时按原场改投影，绝不反过来 ——
反向就等于凭空造一个从未算过的结论。

只改能靠行内证据确定的行；证据本身矛盾（没有台账、台账与标量结论方向相反、
`verify_score` 为空却仍有结论）一律只报不改，交给人判断。

用法：
    python scripts/resync_verdict_scalars.py                  # 只报告，不写库
    python scripts/resync_verdict_scalars.py --apply          # 改库 + 写 change log + 重算博主统计
    python scripts/resync_verdict_scalars.py --ids 3180,960  # 只看指定行
退出码：0 干净；3 还有需要人看的行（不静默放过）。
"""
import argparse
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from _db_guard import pin_local_sqlite  # noqa: E402

# 注意：本模块** import 时不碰数据库、不 import ORM** —— 单测要直接 import
# `find_desynced` 这条纯判据，而 `pin_local_sqlite(use_mirror_default=True)` 一旦放在模块顶层就会在
# 测试进程里改 DATABASE_URL。连接动作留在 main() 里，顺序仍是"先钉库再 import ORM"。


def find_desynced(rows):
    """按行内证据找出可自动同步的行与必须人工判断的行。

    返回 (fixable, manual)：
      fixable: [(prediction, 字段名, 现值, 应为)]
      manual:  [(prediction, 原因)]

    同步方向只有一个：**台账（`verify_history` 末条）是当次验证的原场记录**。
    但"末条"必须先被证明**就是产生这些标量的那一次观察** ——
    `PredictionService.verify()`（人工确认）只改 `is_correct/actual_change/ai_judgment`、
    既不追加台账也不动 `verify_score`（`prediction_service.py:151-157`），
    那种行的末条属于另一个观察窗口，照它覆盖等于把无关窗口的分数写进来（第 16 轮 m-3）。
    """
    fixable, manual = [], []
    for p in rows:
        if p.is_correct is None:
            continue                       # 未验证：没有需要对齐的结论
        ledger = (p.verify_history or [None])[-1] if p.verify_history else None
        if not ledger:
            manual.append((p, '有结论但没有台账，没有可依据的原场记录'))
            continue
        if 'is_correct' not in ledger:
            manual.append((p, '台账末条缺 is_correct，无法确认它和标量结论是同一次'))
            continue
        if bool(ledger['is_correct']) != bool(p.is_correct):
            manual.append((p, '台账与标量结论方向相反（%s vs %s）——'
                              '哪个是当次真实结论无法从行内判断'
                           % (ledger['is_correct'], p.is_correct)))
            continue
        if not _same_observation(ledger, p):
            manual.append((p, '台账末条与标量不是同一次观察（涨跌幅 %s vs %s）'
                              % (ledger.get('change'), p.actual_change)))
            continue
        raw = ledger.get('score')
        if raw is None:
            manual.append((p, '台账末条没有 score'))
            continue
        if p.verify_score is None:
            manual.append((p, 'verify_score 为空却仍有结论'))
            continue
        try:
            want = int(raw)
        except (TypeError, ValueError):
            manual.append((p, '台账 score 不是整数：%r' % (raw,)))
            continue
        have = int(p.verify_score)
        if want != have:
            fixable.append((p, 'verify_score', have, want))
        # m-1 的遗留行：同一端点在 `current_nav_date` 与 `end_nav_date` 里是两个日期
        # （新代码已经同口径，2026-09-22 之前落库的 21 行不是）。只有两处净值确实同一个
        # 数时才敢按 `end_nav_date` 改日期，否则说明它们描述的是两回事，交给人看。
        if (p.end_nav_date and p.current_nav_date
                and p.current_nav_date != p.end_nav_date):
            if _same_number(p.current_nav, p.end_nav):
                fixable.append((p, 'current_nav_date', p.current_nav_date, p.end_nav_date))
            else:
                manual.append((p, 'current_nav %s 与 end_nav %s 不是同一个数，'
                                  '两个日期字段谁对无法判断'
                               % (p.current_nav, p.end_nav)))
    return fixable, manual


def _same_number(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return a == b


def _same_observation(ledger, prediction):
    """台账末条的涨跌幅必须与标量 `actual_change` 是同一次算出来的，才算"当次原场"。"""
    tail = ledger.get('change')
    if tail is None or prediction.actual_change is None:
        return True          # 缺一侧就退回"方向一致"这个弱锚，别把正常行挡在门外
    return _same_number(tail, prediction.actual_change)


def main():
    ap = argparse.ArgumentParser(description='按台账同步标量结论字段（默认只报告）')
    ap.add_argument('--apply', action='store_true', help='真的写库（默认 dry-run）')
    ap.add_argument('--ids', help='逗号分隔的 prediction id，只处理这些行')
    ap.add_argument('--run-id', help='change log 批次号，默认 verify-score-resync-<今天>')
    args = ap.parse_args()

    pin_local_sqlite(use_mirror_default=True)
    from src.models.database import Prediction

    wanted = None
    if args.ids:
        try:
            wanted = {int(x) for x in args.ids.split(',') if x.strip()}
        except ValueError:
            print('[abort] --ids 必须是逗号分隔的整数')
            return 2

    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        q = db.query(Prediction).filter(Prediction.is_deleted == False)  # noqa: E712
        rows = [p for p in q.all() if wanted is None or p.id in wanted]
        fixable, manual = find_desynced(rows)

        print('[扫描] %d 行；可按台账同步 %d 处；需要人工判断 %d 行'
              % (len(rows), len(fixable), len(manual)))
        for p, field, have, want in fixable:
            print('   [不同步] id=%-5s %s 目标%s 结论=%s %s：%s → %s'
                  % (p.id, p.fund_code, p.target_date, p.is_correct, field, have, want))
        for p, why in manual:
            print('   [人工] id=%-5s %s：%s' % (p.id, p.fund_code, why))

        if not fixable and not manual:
            print('[ok] 标量结论字段与台账一致，无需改动')
            return 0
        if not args.apply:
            print('\n未写库。确认后用 --apply 执行。')
            return 0 if not manual else 3

        from src.services.prediction_change_log_service import (
            add_prediction_change_log, snapshot_prediction)
        run_id = args.run_id or 'verify-score-resync-%s' % date.today().strftime('%Y%m%d')
        touched_bloggers = set()
        for p, field, _have, want in fixable:
            before = snapshot_prediction(p)
            setattr(p, field, want)
            add_prediction_change_log(db, p, action='scalar_resync', source='maintenance',
                                      before_state=before, run_id=run_id)
            touched_bloggers.add(p.blogger_id)

        from src.utils.blogger_stats import recalculate_blogger_stats
        for blogger_id in touched_bloggers:
            if blogger_id:
                recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()
        print('[applied] 已按台账同步 %d 处（run_id=%s；整批撤销用 '
              'python scripts/restore_prediction_batch.py --run-id %s）'
              % (len(fixable), run_id, run_id))

        # 改完再自查一遍：还剩多少行不一致（含只报不改的人工行）
        db.expire_all()
        rows = [p for p in db.query(Prediction).filter(
            Prediction.is_deleted == False).all()          # noqa: E712
            if wanted is None or p.id in wanted]
        still, manual_now = find_desynced(rows)
        if still:
            print('[warn] 同步后仍有 %d 处不一致：%s'
                  % (len(still), [(p.id, f) for p, f, _a, _b in still][:10]))
            return 3
        if manual_now:
            print('[need-human] %d 行证据矛盾，本脚本不动：%s'
                  % (len(manual_now), [p.id for p, _w in manual_now][:10]))
            return 3
        print('[ok] 同步后无残留不一致')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
