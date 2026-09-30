# -*- coding: utf-8 -*-
"""给**卡住的存量观点行**补上回收站那一对时间戳（默认只出清单；真写要确认词与选哪一天）。

要解决的问题（任务 #142 的第二半）：AI 判"情绪表达/广告引流/新闻转述/无关内容"这四类时，
观点行被 `is_deleted = True` 放进回收站。第 69 轮之前那一支**一个时间戳都不写** ⇒
线上唯一会删观点行的那把尺子（`retention_three_buckets._deleted_viewpoint_ids`，条件
`deleted_at.isnot(None)` 且 `deleted_at < 今天 - N 天`）结构性选不中它们：
镜像 2026-09-30 实测软删 418 行、带归档时刻 400 行 ⇒ **18 行永远进不了清理桶**。
代码那一半已修（`viewpoint_workflow_service._apply_deep_analysis` 现在一起写那一对），
但**代码只修未来的行** —— 这 18 行（以及生产同形状的存量）还卡着，得单独写一次。

**为什么"补哪一个时刻"是一个决定、不是默认值**：
* `--stamp-from today` ⇒ 归档时刻就是今天：恢复窗口从今天重新算，这 18 行**再等满 N 天**
  才成为清理候选（最保守，等于"当作刚进回收站"）。
* `--stamp-from created` ⇒ 用这一行自己的 `created_at`：早就过了 N 天的那几行**当场**
  成为清理候选，下一次三桶清理就会真删它们（这符合"它其实已经在回收站里放了很久"，
  但代价是那一批行不再有任何恢复窗口）。
两个选项都会把行变成"能被选中"，区别只在**哪天**开始能被选中 ⇒ 我不替你选：
`--apply` 必须点名 `--stamp-from`，默认（dry-run）把两种选择的结果**各算一遍列给你看**。

时间戳本身不在这里算：只有 `prediction_lifecycle.archive_stamp()` 一处会写那一对列
（第 54 轮 A-1 / B-2 那把棘轮按 (文件, 函数) 数站点，并核"每处写的来路是不是那只钟"）；
保留天数也不在这里立第二个数：取 `ThreeBucketPolicy().deleted_viewpoint_days`。

用法：
    python scripts/backfill_viewpoint_archive_stamps.py                    # 本地镜像：只出清单
    python scripts/backfill_viewpoint_archive_stamps.py --production       # 生产：只出清单
    python scripts/backfill_viewpoint_archive_stamps.py --production \
        --apply --stamp-from created --confirm BACKFILL-VP-STAMPS
    python scripts/backfill_viewpoint_archive_stamps.py --restore-from backup/backfill-vp-stamps-xxx.json
"""
import argparse
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

CONFIRM_TOKEN = 'BACKFILL-VP-STAMPS'
# 备份目录的出口是给**测试**用的（与 `CLOSE_BACKUP_DIR` 同一课：夹具真跑 `--apply` 时
# 不许往仓库级 `backup/` 落残渣）。
BACKUP_DIR = os.path.join(os.environ.get('VP_BACKFILL_BACKUP_DIR') or ROOT, 'backup')
REASON_KIND = 'vp_archive_stamp_backfill'


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def policy_retention_days():
    """这一族行的保留天数**只有三桶策略一个出处**，不在这里立第二个数。

    单独成一个函数（而不是写在 `main()` 里）只为了一件事：让"把天数写死成 30"这种
    改动能被一条用例量到（`tests/unit/test_backfill_viewpoint_archive_stamps.py`）。
    """
    from src.services.retention_three_buckets import ThreeBucketPolicy
    return ThreeBucketPolicy().deleted_viewpoint_days


def candidates(db):
    """回收站里**缺那一对时间戳**的观点行（缺哪一列都算：那两列是一套的）。"""
    from src.models.database import Viewpoint
    return (db.query(Viewpoint)
            .filter(Viewpoint.is_deleted == True,                    # noqa: E712
                    (Viewpoint.deleted_at.is_(None)) | (Viewpoint.restore_before.is_(None)))
            .order_by(Viewpoint.id.asc())
            .all())


def gap_kinds(rows):
    """把候选行按**缺哪一列**分成两组 —— 两组的后果相反，不许并成一句。

    `no_stamp`（缺 `deleted_at`）：清理那把（`retention_three_buckets._deleted_viewpoint_ids`）
    只看这一列 ⇒ 这一组永远进不了删除候选。
    `no_deadline`（有 `deleted_at`、缺 `restore_before`）：已经在删除候选里，缺的只是
    "保留到哪天"那句话 ⇒ 补这一列**不会**让它们更晚被删。
    """
    no_stamp = [v for v in rows if v.deleted_at is None]
    no_deadline = [v for v in rows if v.deleted_at is not None]
    return no_stamp, no_deadline


def plan_for(db, rows, stamp_from, retention_days, today):
    """逐行算"要写什么"。**不写库**，也拒绝替缺 `created_at` 的行编一个时刻。"""
    from src.services.prediction_lifecycle import archive_stamp

    items, skipped = [], []
    for v in rows:
        if stamp_from == 'today':
            stamp, until = archive_stamp(retention_days=retention_days)
        else:
            if not isinstance(v.created_at, datetime):
                # 这一行连"它是什么时候进来的"都说不清 ⇒ 不许拿今天冒充它自己的时刻，
                # 也不许拿 `viewpoint_date` 顶（那是观点内容对应的日子，不是归档日子）。
                skipped.append((v.id, '这一行没有 `created_at`（%r）⇒ --stamp-from created '
                                      '无从算起，要补请改用 --stamp-from today'
                                      % (v.created_at,)))
                continue
            stamp, until = archive_stamp(retention_days=retention_days, base=v.created_at)
        items.append({'viewpoint_id': v.id, 'analysis_summary': v.analysis_summary,
                      'viewpoint_date': _iso(v.viewpoint_date),
                      'created_at': _iso(v.created_at),
                      'had_deleted_at': _iso(v.deleted_at),
                      'had_restore_before': _iso(v.restore_before),
                      # 键名**故意不叫那两列的名字**：这把棘轮按 AST 数"往归档列写了几次"，
                      # 而备份里那份计划只是**要写的值**、不是写站（第 47 轮"印出来≠报得出来"
                      # 的反面：序列化一份备份不该被数成第二处写，正如 `get_detail` 的回显字典不算）。
                      'stamp': stamp, 'until': until})
    return items, skipped


def _snapshot(db, ids):
    from src.models.database import Viewpoint
    cols = [c.key for c in Viewpoint.__table__.columns]
    rows = db.query(Viewpoint).filter(Viewpoint.id.in_(ids)).all()
    return [{c: _iso(getattr(r, c)) for c in cols} for r in rows]


def apply_backfill(db, items, retention_days, today, stamp_from):
    """写那一对列，然后**拿真尺子回查**：这些行今天到底进没进清理候选。

    那一对的值在这里**再算一次**、不从计划里抄：为的是让"每处写都出自 `archive_stamp()`"
    那条闸（`test_one_ruler_per_question.py`）在这个函数身上真能核到，而不是靠注释担保。
    计划与这里用同一把钟、同一份 `retention_days`、同一个 `stamp_from` ⇒ 算出的是同一个值。
    """
    from src.models.database import Viewpoint
    from src.services.prediction_lifecycle import archive_stamp

    os.makedirs(BACKUP_DIR, exist_ok=True)
    run_stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, 'backfill-vp-stamps-%s.json' % run_stamp)
    ids = [i['viewpoint_id'] for i in items]
    payload = {'created_at': datetime.now().isoformat(), 'as_of': str(today),
               'reason_kind': REASON_KIND, 'stamp_from': stamp_from,
               'retention_days': retention_days,
               'rows': _snapshot(db, ids),
               'plan': [{k: _iso(v) for k, v in i.items()} for i in items]}
    with open(backup_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print('[备份] 写 %d 行原样 → %s' % (len(payload['rows']), os.path.basename(backup_path)))

    by_id = {v.id: v for v in db.query(Viewpoint).filter(Viewpoint.id.in_(ids)).all()}
    done = 0
    written_ids = set()
    for i in items:
        v = by_id.get(i['viewpoint_id'])
        if v is None:
            print('[没写] 观点 %s：库里已经查不到这一行' % i['viewpoint_id'])
            continue
        if not v.is_deleted:
            print('[没写] 观点 %s：这一行已经不在回收站（被还原过），不补' % v.id)
            continue
        if v.deleted_at is not None and v.restore_before is not None:
            print('[没写] 观点 %s：那一列已经有值了（%s / %s），不覆盖别人的写'
                  % (v.id, _iso(v.deleted_at), _iso(v.restore_before)))
            continue
        # 与 `_apply_deep_analysis` 同一把钟、同一个形状：那一对的值只出自 `archive_stamp()`。
        # 为什么写成"先交给一个名字、再解包进那两列"，而不是 `stamp, until = …` 再赋：
        # 那把闸认的是"**这一处写的值**来自那只钟"，而它认的名字只到"整个表达式是一次调用"
        # （`_stamp_names` 只收 `Assign` 且右值是 `archive_stamp(...)` 调用）。三目那种"两臂都是调用"
        # 它同样不认 —— 那是刻意的：把钟的输出先经手一层判断再落地，追责就要落在那两个分支上。
        if stamp_from == 'today':
            stamped = archive_stamp(retention_days=retention_days)
        else:
            stamped = archive_stamp(retention_days=retention_days, base=v.created_at)
        v.deleted_at, v.restore_before = stamped
        done += 1
        written_ids.add(v.id)
        print('[已补] 观点 %s 归档时刻 %s ⇒ 保留到 %s（原来那一列：%s / %s）'
              % (v.id, _iso(stamped[0]), _iso(stamped[1]),
                 _iso(i['had_deleted_at']), _iso(i['had_restore_before'])))
    db.commit()

    # 回查用**那把真尺子**，不用我这里的算式：补完之后三桶的预览到底选中几行。
    # 只问我真正写过的那几行 ⇒ `written_ids` 之外（已有值、被还原过）不算"这轮回查的对象"。
    from src.services.retention_three_buckets import ThreeBucketRetentionService
    svc = ThreeBucketRetentionService(db, today=today)
    plan = svc.build_plan()
    got = set(plan.candidate_ids[ThreeBucketRetentionService.BUCKET_DELETED_VP])
    mine = [i for i in items if i['viewpoint_id'] in written_ids]
    now_candidates = [i for i in mine if i['viewpoint_id'] in got]
    starved = [i for i in mine if i['viewpoint_id'] not in got]
    print('[回执] 补了 %d / 计划 %d 行；本轮清理预览在「已删观点」这一桶选中 %d 行'
          '（选中与否由 `retention_three_buckets` 回答，不是这里的算式）'
          % (done, len(items), len(now_candidates)))
    # "没被选中"有两种完全不同的原因，并成一句就是说谎（2026-09-30 生产实测：计划那句按窗口
    # 算出 411 行已过，回执只说 75 行 ⇒ 差额不是"日子还没到"，是清理那一次的**全局单次额度**
    # `max_total_per_run` 被排在前面的桶（回收站预测当场选出 425 行）先用完了，`plan.truncated` 为真）。
    if starved:
        past_due = [i for i in starved if i['until'] <= today]
        print('[回执] 没被本轮选中的 %d 行：其中 %d 行窗口已经过了、%d 行确实还没到各自那个保留日。'
              % (len(starved), len(past_due), len(starved) - len(past_due)))
        # 那句"缺额度"只在真有过期却没轮到的行时才成立：全是"还没到期"时把它印出来，
        # 就把一句日历的话说成了容量的话（同一族，反过来也错）。
        if past_due:
            print('[回执] 窗口已过却没被选中的那 %d 行缺的是额度不是日历（清理单次全局上限 %d 行，'
                  '按 `%s` 的顺序分给各桶，这一桶前面还排着回收站预测/清理日志/结构性不可验三档%s）'
                  % (len(past_due), svc.policy.max_total_per_run,
                     ' → '.join(ThreeBucketRetentionService.BUCKETS[:4]),
                     '，本轮预览自己报了 truncated ⇒ 前面那些桶一旦清掉，后面的桶才轮到'
                     if plan.truncated else
                     '，而本轮预览没报 truncated ⇒ 请核对清理那把尺子'))
    print('[回执] 还原命令：python scripts/backfill_viewpoint_archive_stamps.py '
          '--restore-from %s' % backup_path)
    return backup_path


def restore(db, path, apply_it, force=False):
    """按备份把那一对列**改回补之前的值**（今天这两列都是 NULL ⇒ 还原 = 让行重新卡住）。

    动手前逐行比对现状：这一行现在还是我这次补上去的那个形状吗？被别人动过的行默认拦下
    （与 `close_unknowable_predictions.restore` 同一条规矩：还原是"撤销我这一次动作"，
    不是"按 id 硬盖"，否则会把别人写的归档时刻与恢复下界一起清掉）。
    """
    with open(path, encoding='utf-8') as fh:
        payload = json.load(fh)
    if payload.get('reason_kind') != REASON_KIND:
        print('[abort] 这份备份的 reason_kind=%r 不是补时间戳留的档 ⇒ 这条还原只认这种档'
              % payload.get('reason_kind'))
        return 4
    plan = payload.get('plan') or []
    if not plan:
        print('[abort] 这份备份里 `plan` 是空的 ⇒ 没什么可还原（先核对这份 JSON 是不是那份）')
        return 4
    ids = [i['viewpoint_id'] for i in plan]
    wanted = {i['viewpoint_id']: i for i in plan}
    from src.models.database import Viewpoint
    rows = db.query(Viewpoint).filter(Viewpoint.id.in_(ids)).all()
    by_id = {r.id: r for r in rows}
    blockers, blocked = [], set()

    def _block(vid, why):
        blockers.append('%s：%s' % (vid, why))
        if not force:
            blocked.add(vid)

    for vid in ids:
        row = by_id.get(vid)
        if row is None:
            _block(vid, '库里查不到这一行')
            continue
        w = wanted[vid]
        if _iso(row.deleted_at) != w['stamp'] or _iso(row.restore_before) != w['until']:
            _block(vid, '这一行现在写的不是我这一次补的值（现在 %s / %s，我补的是 %s / %s）'
                   '⇒ 硬盖会把别人写的归档时刻与恢复下界一起清掉'
                   % (_iso(row.deleted_at), _iso(row.restore_before),
                      w['stamp'], w['until']))
    todo = [vid for vid in ids if vid not in blocked]
    print('[计划] 备份里有 %d 行，本次要还原 %d 行（%s）'
          % (len(ids), len(todo), '真还原' if apply_it else 'dry-run，一行都不动'))
    for msg in blockers:
        print('  [拦下] 观点 %s%s' % (msg, '' if force else ''))
    if blockers:
        print('[警告] %d 行现状与备份不一致%s' % (len(blockers),
              '，--force-restore 已放行硬盖' if force else '，默认不动它们'))
    if not apply_it:
        print('[dry-run] 一行都没动。真还原加 --apply --confirm %s' % CONFIRM_TOKEN)
        return 3
    for vid in todo:
        row = by_id[vid]
        row.deleted_at = (datetime.fromisoformat(wanted[vid]['had_deleted_at'])
                          if isinstance(wanted[vid]['had_deleted_at'], str)
                          else wanted[vid]['had_deleted_at'])
        row.restore_before = (date.fromisoformat(wanted[vid]['had_restore_before'])
                              if isinstance(wanted[vid]['had_restore_before'], str)
                              else wanted[vid]['had_restore_before'])
        print('[已还原] 观点 %s ⇒ deleted_at=%s / restore_before=%s'
              % (vid, _iso(row.deleted_at), _iso(row.restore_before)))
    db.commit()
    print('[回执] 还原 %d / 备份 %d 行。注意：还原就是把那两列清回 NULL ⇒ '
          '这些行重新变成"永远进不了清理桶"的形状' % (len(todo), len(ids)))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='给回收站里缺时间戳的存量观点行补上那一对（默认只出清单）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--stamp-from', choices=['today', 'created'],
                    help='归档时刻写哪一天：today=窗口重新算；created=按这一行进来的那天'
                         '（--apply 时必须点名）')
    ap.add_argument('--production', action='store_true', help='显式对生产执行')
    ap.add_argument('--restore-from', help='按备份 JSON 还原（默认 dry-run）')
    ap.add_argument('--confirm-restore', help='还原到生产时必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--force-restore', action='store_true',
                    help='硬盖现状不一致的行（默认不许）')
    args = ap.parse_args()

    # 确认词排在**连库与取数之前**（本仓规矩：用法错不该先付一次连库的代价，
    # 更不该让人以为"给了 --apply 就会写"）。
    if args.apply and not args.restore_from:
        if (args.confirm or '') != CONFIRM_TOKEN:
            print('[abort] 真写要 --confirm %s（默认只出清单）' % CONFIRM_TOKEN)
            return 4
        if not args.stamp_from:
            print('[abort] 真写还要 --stamp-from today|created：补哪一个时刻是决定，'
                  '不是默认值（清单里两种各补几行、几行的窗口已经过了都列了，'
                  '逐行的计划只对点名的那一种印，看过再点名）')
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
        target_label = '线上生产库'
    else:
        from _db_guard import db_kind, pin_local_sqlite
        print('[target] %s' % db_kind(pin_local_sqlite(use_mirror_default=True)))
        target_label = '本地镜像库'

    from src.models.database import SessionLocal
    from src.services.prediction_lifecycle import current_as_of

    db = SessionLocal()
    try:
        today = current_as_of()
        retention = policy_retention_days()
        if args.restore_from:
            if args.production and args.confirm_restore != CONFIRM_TOKEN:
                print('[abort] 还原到生产要 --confirm-restore %s' % CONFIRM_TOKEN)
                return 4
            return restore(db, args.restore_from, args.apply, force=args.force_restore)

        rows = candidates(db)
        # 缺的那一列不是同一列，后果就完全不同 ⇒ 这句话必须分开说（2026-09-29 镜像实测：
        # 418 行里 18 行连 `deleted_at` 都没有、400 行有 `deleted_at` 只缺 `restore_before`）。
        # `retention_three_buckets._deleted_viewpoint_ids` 只比 `deleted_at`，压根不读另一列
        # ⇒ 前一组是"永远进不了清理候选"，后一组是"今天就在候选里、只是页面上说不出保留到哪天"。
        no_stamp, no_deadline = gap_kinds(rows)
        print('[现状] %s回收站里有 %d 行观点缺那一对时间戳（保留天数按三桶策略 = %d 天）：'
              '其中 %d 行连 `deleted_at` 都没有 ⇒ 清理那把只看这一列，它们永远进不了删除候选；'
              '%d 行有 `deleted_at`、只缺 `restore_before` ⇒ 已在删除候选里，只是页面说不出保留到哪天'
              % ('线上生产库' if target_label.startswith('线上') else '',
                 len(rows), retention, len(no_stamp), len(no_deadline)))
        if not rows:
            print('[结论] 没有要补的行（这一库里没有这种形状，或者已经补过了）')
            return 0
        for choice in ('today', 'created'):
            items, skipped = plan_for(db, rows, choice, retention, today)
            aged = sum(1 for i in items if i['until'] <= today)
            print('  [%s] 可补 %d 行：归档时刻=%s ⇒ 保留到各自那天；其中 %d 行的窗口已经过了'
                  % (choice, len(items), '今天' if choice == 'today' else '这一行自己的 created_at',
                     aged))
            for vid, why in skipped:
                print('    [跳过] 观点 %s：%s' % (vid, why))
            if choice == 'today':
                today_items = items
            else:
                created_items = items
        chosen = today_items if (args.stamp_from or 'today') == 'today' else created_items
        for i in chosen:
            print('  [计划] 观点 %s（%s，进来于 %s）⇒ %s / %s'
                  % (i['viewpoint_id'], i['analysis_summary'], _iso(i['created_at']),
                     _iso(i['stamp']), _iso(i['until'])))
        if not args.apply:
            print('[dry-run] 一行都没动。真补加 --apply --confirm %s，并且必须点名 --stamp-from'
                  % CONFIRM_TOKEN)
            return 2
        apply_backfill(db, chosen, retention, today, args.stamp_from)
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
