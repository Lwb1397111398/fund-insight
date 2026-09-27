# -*- coding: utf-8 -*-
"""把**永远判不出来**的预测收进回收站（默认只出计划；真跑要确认词；能一键还原）。

要解决的问题：有些预测挂的标的已经停止发布净值（2026-09-26 生产实测：`003033` 库里
20 行净值、末条 2020-12-08；`002413` 末条 2023-07-07，而预测窗口都是 2026-08~09），
验证器每次问都拿到"这段没有"⇒ 这批预测会永远留在"到期未判"里，老板每次点验证都白跑一遍。

**为什么不能直接按"到期很久了"关**（第 12 轮 MAJOR-3 撤掉过纯日历推断，这里不再放回来）：
一道门必须同时拿到三条证据才动手 ——
① **现场再问一次数据源**：`get_fund_history_range` 对这个窗口答 **0 条**才算"问过且没有"；
   接口抛错／没答话（`None`）一律**不放行**（第 10 轮 B-1 那一课：一次抖动不许被记成事实）；
② **这段窗口永久给不出净值**，两种形状任一成立（`stale_close_evidence` 一处回答，第 53 轮 B-1
   补上第二种）：**停更**＝库里这只代码最后一条净值早于窗口起点；**还没开始**＝窗口整段早于
   库里第一笔净值。都不满足 ⇒ 原样报"还在等"（包括"库里一行都没有"那种说不清的）；
③ **这一行确实被问过第二次**：`was_locked_previously` 问的是"行上那根重问日期是否晚于它
   自己的目标日"，与验证器同一条尺子（第 53 轮 A-2：上一版只抄了①②，同一行验证器说不关、
   脚本说可关）。
三条都在同一个窗口上成立才关闭；少任何一条 ⇒ 只上锁或不关，回收站里那句话也就写不出口。

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

# 第 53 轮 B-6：A-4 修之前那一版收口脚本收掉的行，回收站里都写着这句"已问过两次"，
# 而台账前像证明它们**从没被重问锁压过**（第一次问出来就关了）。
# 订正只换这半句，别的一个字不动 —— 判"有没有证据"用的是 `was_locked_previously`，
# 与验证器那条门同一把尺子（生产那 15 行日期确实晚于目标日 ⇒ 话是真的，一行都不该动）。
UNPROVEN_CLAIM = '已问过两次仍无答案'
HONEST_CLAIM = ('按区间问过仍无答案（这一行没有更早的重问记录：'
                '2026-09-26 那版收口脚本第一次问出来就关了，那句"已问过两次"是写多的）')


def _today_beijing():
    from src.services.prediction_lifecycle import current_as_of
    return current_as_of()


def plan(db, today):
    """列出候选：到期未判 && 源端这个窗口答 0 条 && 关闭判据整条点头（与验证器同一把尺子）。"""
    from src.models.database import FundHistory, Prediction
    from src.services.prediction_lifecycle import (
        close_as_stale_target_note, should_close_as_stale_target, stale_close_evidence,
        was_locked_previously)

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
        if got:
            skipped.append((p.id, code,
                            '源端这段给了 %d 条 ⇒ 是本地没补到，不是它没有' % len(got)))
            continue
        newest = db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == code).order_by(FundHistory.nav_date.desc()).first()
        latest = newest[0] if newest else None
        oldest = db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == code).order_by(FundHistory.nav_date.asc()).first()
        first = oldest[0] if oldest else None
        if stale_close_evidence(local_latest_nav=latest, window_start=start,
                                local_first_nav=first, window_end=end) is None:
            # 这句话不许说反（第 54 轮 B-8）：库里一行净值都没有时，`stale_close_evidence`
            # 因为两个日期都说不清而放行 ⇒ 旧写法"库里净值 None 至 None **覆盖得到**这段窗口"
            # 正好说成了它的反面。放行是对的（拿不准就别关），话要说对。
            if latest is None:
                skipped.append((p.id, code,
                                '库里这只代码一行净值都没有 ⇒ 说不清它给不给得出这段，不关'))
            else:
                skipped.append((p.id, code,
                                '库里净值 %s 至 %s 覆盖得到这段窗口 ⇒ 还能判，不关' % (first, latest)))
            continue
        # **整条判据交回给验证器那一个函数**，不在脚本里各抄一半：上一版这里只抄了
        # `was_locked_previously` 而漏掉"锁未到点不关"，实测同一行（锁在未来）验证器 False、
        # 脚本"可关 1 条"（第 53 轮 A-2）⇒ 现在少任何一环都关不成。
        if not should_close_as_stale_target(
                verdict_reason='no_source_history',   # 这一趟现问出来的就是这一档
                previous_hold=p.next_verify_date, target_date=end,
                local_latest_nav=latest, window_start=start,
                local_first_nav=first, window_end=end, today=today):
            # 哪一道没过，用**同一条尺子**现问一遍只为把话说准（判定仍然只由上面那道门回答）
            if not was_locked_previously(p.next_verify_date, end):
                why = ('行上没有结构性重问锁（重问日 %s ≤ 目标日 %s）⇒ 这是第一次问出来，'
                       '按验证器同一条判据只该上锁' % (p.next_verify_date, end))
            else:
                why = ('重问锁 %s 还没到点（今天 %s）⇒ 这一轮连"第二次"都还没成立，不许关'
                       % (p.next_verify_date, today))
            skipped.append((p.id, code, why))
            continue
        out.append({'prediction_id': p.id, 'fund_code': code, 'fund_name': p.fund_name,
                    'blogger_id': p.blogger_id, 'target_date': str(end),
                    'window_start': str(start), 'local_latest_nav': str(latest),
                    'local_first_nav': str(first), 'source_rows': len(got),
                    'note': close_as_stale_target_note(
                        p, latest, start, first_nav=first, window_end=end)})
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


def restore(db, path, apply_it, force=False):
    """按备份还原。**动手前逐行比对现状**：这一行还是我关闭时的样子吗？

    第 53 轮 B-3 实测：旧写法只按 id 盖回去 —— 中间如果老板手动归档过同一行
    （`deleted_by='user'`、自己写的 `delete_reason`），还原会把署名与原因**无声清掉**，
    台账还自称 `source='user'`。还原不是"回到备份"，是"撤销我这一次动作"，
    所以我必须先确认现在这行确实是我那次动作留下的形状。
    """
    from src.models.database import Prediction
    from src.services.prediction_service import PredictionService

    with open(path, encoding='utf-8') as fh:
        payload = json.load(fh)
    # 这份备份是**哪一种动作**留下的，决定还原该往哪个方向走（第 54 轮 B-1 / A-8）：
    # `--fix-wording` 的备份里 `plan[]` 只有 `old/new` 两句文案、没有 `note` ⇒ 下面那道
    # "行上原因与我关闭时写的不一样"的比对结构性永不触发，其余三道拦对这种行又都为真，
    # 于是拿它 `--restore-from --apply` 会把**该留在回收站**的行放回活跃列表。
    # 文案订正的反向是"把 old 写回去"，不是"撤销关闭"。
    reason_kind = payload.get('reason_kind')
    if reason_kind != 'stale_target_no_source_history':
        print('[abort] 这份备份的 reason_kind=%r 不是关闭留的档 ⇒ 还原命令只认关闭备份；'
              '文案订正（unproven_two_ask_claim）要改回去请用 --fix-wording 反向把 old 写回'
              % reason_kind)
        return 4
    notes = {i['prediction_id']: i.get('note') or '' for i in payload.get('plan') or []}
    if not any(notes.values()):
        print('[警告] 这份关闭备份里一行 `note` 都没有 ⇒ 少一道"行上原因与关闭时一致"的比对，'
              '只剩署名/在回收站/无结论三道拦得住')
    ids = list(notes) or [r['id'] for r in payload.get('rows') or []]
    print('[计划] 备份里有 %d 行，还原 = 把它们从回收站放回活跃列表（%s）'
          % (len(ids), '真还原' if apply_it else 'dry-run，一行都不动'))
    if not ids:
        print('[abort] 备份里一行都没有 ⇒ 没什么可还原（先核对这份 JSON 是不是那份）')
        return 4
    rows = db.query(Prediction).filter(Prediction.id.in_(ids)).all()
    by_id = {r.id: r for r in rows}
    blockers, blocked = [], set()

    def _block(pid, why):
        blockers.append('%s：%s' % (pid, why))
        if not force:
            # `--force-restore` 说的是"这些行也照样盖回"⇒ 那就不能同时把它们踢出待还原名单。
            # 旧写法两件事各写一半：警告照印、`todo` 照排除 ⇒ 结果是
            # "拦下 1 行、还原 0 行"却回 0（成功），等于把硬盖 promise 落空了还不报。
            blocked.add(pid)

    for pid in ids:
        row = by_id.get(pid)
        if row is None:
            _block(pid, '库里查不到这一行')
            continue
        if not row.is_deleted:
            _block(pid, '这一行现在**不在回收站**（早就还原过、或被动过）')
            continue
        if row.deleted_by != 'system':
            _block(pid, '现在的归档署名是 %r，不是 system ⇒ 这一行被谁手动动过，'
                       '还原会把他的署名与原因一起抹掉' % (row.deleted_by,))
            continue
        if notes.get(pid) and (row.delete_reason or '') != notes[pid]:
            _block(pid, '行上那句原因与我关闭时写的不一样 ⇒ 中间被改过')
            continue
        if row.is_correct is not None:
            _block(pid, '这一行现在带着结论（is_correct=%s）⇒ 还原前先查是谁判的'
                       % row.is_correct)
    if blockers:
        print('[现状不符] %d / %d 行不许还原：' % (len(blockers), len(ids)))
        for line in blockers:
            print('  - %s' % line)
        if not force:
            print('[abort] 要硬盖请显式加 --force-restore（会连别人的署名与原因一起清掉）')
            return 4
        print('[警告] --force-restore：上面这些行也照样盖回，差异不再拦')
    if not apply_it:
        return 3
    svc = PredictionService(db)
    todo = [pid for pid in ids if pid not in blocked]
    done = sum(1 for i in todo if svc.restore_prediction(i))
    print('[还原] %d / %d 行已回到活跃列表（拦下 %d 行）'
          % (done, len(todo), len(ids) - len(todo)))
    return 0


def find_unproven_claims(db):
    """回收站里那句"已问过两次"，逐行问一句：**这一行当时真被锁过吗**（第 53 轮 B-6）。

    A-4 修之前，这一版脚本收掉的行全都**没**经过重问锁（镜像实测 5 行，`next_verify_date`
    全部 ≤ 自己的目标日），而回收站里那句话照写不误。判据只有一把：
    `was_locked_previously` —— 那根日期落在自己的目标日**之后**才是锁写下的
    （创建期的排日被两个出口夹在目标日之前）。生产那 15 行逐行问过，日期确实晚于目标日 ⇒
    话是真的，这一条就一行都不该动。
    """
    from src.models.database import Prediction
    from src.services.prediction_lifecycle import was_locked_previously

    rows = (db.query(Prediction)
            .filter(Prediction.is_deleted == True,                    # noqa: E712
                    Prediction.deleted_by == 'system')
            .order_by(Prediction.id.asc()).all())
    out = []
    for p in rows:
        reason = p.delete_reason or ''
        if UNPROVEN_CLAIM not in reason:
            continue
        if was_locked_previously(p.next_verify_date, p.target_date):
            continue                       # 行上确实有那把锁 ⇒ 那句话有出处，不动
        out.append((p, reason, reason.replace(UNPROVEN_CLAIM, HONEST_CLAIM)))
    return out


def apply_reword(db, items, today):
    """把那句写多的话改口：**只动 `delete_reason` 这一列**，别的一字不碰。

    先写备份（原句逐行留着），`db.commit()` 成功之后才逐行回执（第 54 轮 B-9：旧写法
    把 `[已订正]` 排在提交之前，提交失败时话已经说出去了）。
    ⚠ 这份备份**不能**喂给 `--restore-from` —— 那条命令的方向是"撤销关闭"，会把行放回
    活跃列表；这份要改回去是把 `old` 写回那一列（`restore()` 现在按 `reason_kind` 拒）。
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, 'archive-note-%s.json' % stamp)
    payload = {'created_at': datetime.now().isoformat(), 'as_of': str(today),
               'reason_kind': 'unproven_two_ask_claim',
               'rows': _snapshot(db, [p.id for p, _o, _n in items]),
               'plan': [{'prediction_id': p.id, 'old': old, 'new': new}
                        for p, old, new in items]}
    with open(backup_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print('[备份] 写 %d 行原句 → %s' % (len(items), os.path.basename(backup_path)))

    from src.services.prediction_change_log_service import (
        add_prediction_change_log, snapshot_prediction)

    done = []
    for p, old, new in items:
        before = snapshot_prediction(p)
        p.delete_reason = new
        add_prediction_change_log(db, p, action='archive_note_fixed', source='system',
                                  before_state=before)
        done.append(p.id)
    db.commit()
    # 回执排在提交之后（第 54 轮 B-9）：提交失败时话不能说在前头
    for pid in done:
        print('[已订正] 预测 %s：只改 delete_reason（其余列一字未动）' % pid)
    print('[回执] 订正 %d / %d 行；原句在 %s' % (len(done), len(items), backup_path))
    return backup_path


def main():
    ap = argparse.ArgumentParser(description='关闭"标的已停更、永远判不出来"的预测（默认只出计划）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--production', action='store_true', help='显式对生产执行')
    ap.add_argument('--restore-from', help='按备份 JSON 还原（默认 dry-run）')
    ap.add_argument('--confirm-restore', help='还原到生产时必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--force-restore', action='store_true',
                    help='硬盖现状不一致的行（默认不许：那会把别人手动归档的署名与原因一起清掉）')
    ap.add_argument('--fix-wording', action='store_true',
                    help='把回收站里那句拿不出证据的"已问过两次"改口（默认只列出，不写）')
    args = ap.parse_args()

    # 确认词排在**连库与取数之前**（这条仓库规矩：用法错不该先付一次网络与数据库的代价，
    # 更不该让人以为"给了 --apply 就会写"）：
    if args.apply and not args.restore_from and (args.confirm or '') != CONFIRM_TOKEN:
        print('[abort] 真写要 --confirm %s（默认只出计划）' % CONFIRM_TOKEN)
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
        if args.restore_from:
            if args.production and args.confirm_restore != CONFIRM_TOKEN:
                print('[abort] 还原到生产要 --confirm-restore %s' % CONFIRM_TOKEN)
                return 4
            return restore(db, args.restore_from, args.apply,
                           force=args.force_restore)

        if args.fix_wording:
            # 改文案**不问数据源**：这一支只看回收站里的行与它自己那根日期（`plan()` 会
            # 逐条打接口，让一次纯文案订背上 15 次外呼是没必要的代价）
            stale = find_unproven_claims(db)
            print('[计划] 回收站里 %d 行的"%s"拿不出证据（%s）'
                  % (len(stale), UNPROVEN_CLAIM, '真订正' if args.apply else 'dry-run，一行都不改'))
            for p, _old, new in stale:
                print('  [改口] 预测 %s（%s）⇒ %s' % (p.id, p.fund_code, new))
            if not stale:
                # 这句话以前在镜像上跑也印"生产实测"（第 54 轮 B-4 / A-7）⇒ 库名要从实际连的那台来
                print('[结论] %s里没有拿不出证据的"%s"' % (
                    '线上生产库' if target_label.startswith('线上') else '这个库', UNPROVEN_CLAIM))
                return 0
            if not args.apply:
                print('[dry-run] 一行都没动。真订正加 --apply --confirm %s' % CONFIRM_TOKEN)
                return 2
            apply_reword(db, stale, today)
            return 0

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
