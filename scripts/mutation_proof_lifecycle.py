# -*- coding: utf-8 -*-
"""逻辑侧的变异体检：把这一批判据挨个摘掉，对应用例必须变红。

第 53 轮 A-10 的账：这份东西原来躺在**不入库**的 `data/_review_tmp/` 里
（`.gitignore` 把整个 `data/` 挡掉了），于是"变异全 RED"这句话在干净克隆上没法复核 ——
第 43 轮前端那 104 处变异就是靠随仓库走的脚本 + 原始日志才成为证据，逻辑侧欠了同一笔。

三条规矩是从踩过的坑里来的，不是装饰：
① **锚点现读、不手抄**。上一版这里留着两条已被实测驳回的设计的锚点
   （`return bool(target_is_non_trading_day)` 那行早就不在了）⇒ 它报的是"锚点不唯一"，
   而不是"判据有效"。锚点命中数不是 1 就记 ANCHOR-MISS 并把退码弄红：**ANCHOR-MISS 是失败**。
② **判定只认"断言失败"那种红**（同 `mutation_proof_frontend.py` 第 33 轮 A-MAJOR-4 那条）：
   退码 2/4（用法错、收集错、conftest 起手就炸）不算判据有效，记 HARNESS-FAIL。
③ **跑完逐文件字节回读比对**，不一致就退红 —— 变异脚本自己把源码写坏了得能看见。

用法：
    python scripts/mutation_proof_lifecycle.py                 # 全跑
    python scripts/mutation_proof_lifecycle.py --list          # 只列，不改一个字节
    python scripts/mutation_proof_lifecycle.py --only pre_inception
"""
import argparse
import importlib.util
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 两把互斥锁（第 54 轮 B-10）：这份工具会**就地改写 `src/*.py`** 再跑 pytest，
# 与任何 pytest 会话并发都会互相污染（它自己抢不到锁就照跑 ⇒ 别人的会话读到的是变异体）。
# 加载方式与前端那份一样按**文件路径**：`from src.utils import ...` 会执行 `src/__init__.py`，
# 一路拉起 ORM 并按 `.env` 建出绑生产的 engine（第 45 轮 A-M5 那条链）。
_lock_spec = importlib.util.spec_from_file_location(
    'mutation_lock', os.path.join(ROOT, 'src', 'utils', 'mutation_lock.py'))
mutation_lock = importlib.util.module_from_spec(_lock_spec)
_lock_spec.loader.exec_module(mutation_lock)
assert 'src.models.database' not in sys.modules, \
    '加载锁的时候把 ORM 拉起来了 ⇒ 这个进程会按 .env 建 engine，正是要避免的那种事'
LIFECYCLE = 'src/services/prediction_lifecycle.py'
VERIFY = 'src/services/prediction_verify_service.py'
SCRIPT = 'scripts/close_unknowable_predictions.py'
SYNC = 'src/fund/fund_sync_manager.py'
SVC = 'src/services/prediction_service.py'
QUERY = 'src/services/prediction_query_service.py'
HOLD_TESTS = 'tests/unit/test_structurally_unverifiable_hold.py'
CLOSE_TESTS = 'tests/unit/test_close_unknowable_predictions.py'
QUERY_TESTS = 'tests/unit/test_prediction_query.py'
SAFE_TESTS = 'tests/unit/test_prediction_management_safety.py'

MUTATIONS = [
    # label, 文件, 锚点, 改成什么, 用例文件, 用例名
    ('M1_any_failure_is_structural', LIFECYCLE,
     '    return verdict_reason in STRUCTURAL_VERDICT_REASONS',
     '    return bool(verdict_reason)',
     HOLD_TESTS, 'test_a_failure_that_was_never_asked_about_does_not_hold_the_row'),
    ('M2_degenerate_endpoint_becomes_closable', LIFECYCLE,
     "CLOSABLE_VERDICT_REASONS = ('no_source_history',)",
     "CLOSABLE_VERDICT_REASONS = ('no_source_history', 'same_nav_endpoint')",
     HOLD_TESTS, 'test_a_degenerate_endpoint_is_never_closed_even_on_the_second_ask'),
    ('M3_past_date_counts_as_second_ask', LIFECYCLE,
     '    return hold is not None and target is not None and hold > target',
     '    return hold is not None and target is not None',
     HOLD_TESTS, 'test_a_first_ask_never_closes_even_when_the_fund_has_stopped'),
    ('M3b_close_script_stops_asking_the_whole_gate', SCRIPT,
     '                previous_hold=p.next_verify_date, target_date=end,',
     '                previous_hold=None, target_date=end,',
     CLOSE_TESTS, 'test_both_evidences_present_closes_and_leaves_the_verdict_untouched'),
    ('M4_hold_only_covers_no_source_history', LIFECYCLE,
     "STRUCTURAL_VERDICT_REASONS = ('no_source_history', 'same_nav_endpoint')",
     "STRUCTURAL_VERDICT_REASONS = ('no_source_history',)",
     HOLD_TESTS, 'test_a_degenerate_endpoint_holds_the_row_out_of_the_due_queue'),
    ('M5_rosters_stop_adding_up', LIFECYCLE,
     "LOCK_ONLY_VERDICT_REASONS = ('same_nav_endpoint',)",
     "LOCK_ONLY_VERDICT_REASONS = ()",
     HOLD_TESTS, 'test_the_structural_reasons_are_all_dispositioned'),
    ('M5b_roster_invents_a_reason_the_verifier_never_answers', LIFECYCLE,
     "STRUCTURAL_VERDICT_REASONS = ('no_source_history', 'same_nav_endpoint')",
     "STRUCTURAL_VERDICT_REASONS = ('no_source_history', 'same_nav_endpoint',"
     " 'insufficient_points')",
     HOLD_TESTS, 'test_the_verifier_s_whole_vocabulary_is_dispositioned'),
    ('M6_second_copy_of_the_structural_ruler', VERIFY,
     "            if is_structural_verdict(data_check.get('reason')):",
     "            if data_check.get('reason') in ('no_source_history',"
     " 'same_nav_endpoint'):",
     HOLD_TESTS, 'test_only_one_place_decides_whether_a_failure_is_structural'),
    ('M7_refused_close_locks_nothing', VERIFY,
     '                if not archived:',
     '                if archived:',
     HOLD_TESTS, 'test_a_refused_close_still_puts_the_lock_back'),
    ('M8_pre_inception_evidence_withdrawn', LIFECYCLE,
     '    return first is not None and end is not None and first > end',
     '    return False',
     HOLD_TESTS, 'test_a_window_entirely_before_the_first_nav_row_closes_the_row'),
    ('M9_retag_keeps_the_old_targets_lock', SYNC,
     '                and pred.next_verify_date > pred.target_date):',
     '                and False):',
     HOLD_TESTS, 'test_a_retag_sends_the_old_targets_lock_back_below_the_target'),
    ('M10_manual_rebind_bypasses_the_evidence_gate', SVC,
     '            if gap:\n'
     '                raise ValueError("不能把这条预测改到 %s：%s" % (wanted_code, gap))',
     '            if False:\n'
     '                raise ValueError("不能把这条预测改到 %s：%s" % (wanted_code, gap))',
     SAFE_TESTS, 'test_put_prediction_rebind_asks_the_evidence_gate_and_clears_the_lock'),
    ('M11_pending_bucket_reads_the_legacy_column_again', QUERY,
     '        if status in ("verified", "pending", "unverified"):\n'
     '            return conclusion_conditions(status)',
     '        if status in ("verified", "pending", "unverified"):\n'
     '            return [Prediction.status == "pending"]',
     QUERY_TESTS, 'test_the_pending_bucket_asks_for_a_conclusion_not_for_the_status_column'),
    ('M12_archive_stamp_goes_back_to_the_wall_clock', SVC,
     '        prediction.deleted_at, prediction.restore_before = archive_stamp()',
     '        prediction.deleted_at, prediction.restore_before = '
     'datetime.now(), date.today() + timedelta(days=30)',
     CLOSE_TESTS, 'test_the_archive_stamp_and_the_restore_deadline_come_from_the_beijing_clock'),
    ('M13_force_restore_promises_but_still_skips', SCRIPT,
     "        blockers.append('%s：%s' % (pid, why))\n        if not force:",
     "        blockers.append('%s：%s' % (pid, why))\n        if True:",
     CLOSE_TESTS, 'test_restore_refuses_to_overwrite_a_row_somebody_else_touched'),
    # 第 54 轮 A-5：退化端点这一档没有凭据可跟，跟着 3 天那一档走 ⇒ 镜像那 5 行每三天
    # 弹回「待验证到期」。这一处变异把它改回"两档共用一个节奏"，两条分档判据都要红。
    ('M14_both_tiers_share_the_credential_clock', LIFECYCLE,
     '    if verdict_reason in LOCK_ONLY_VERDICT_REASONS:\n'
     '        from src.core.config import config\n\n'
     '        return int(config.NAV_HISTORY_LOOKBACK_DAYS) + 1\n',
     '    if False:\n'
     '        from src.core.config import config\n\n'
     '        return int(config.NAV_HISTORY_LOOKBACK_DAYS) + 1\n',
     HOLD_TESTS, 'test_the_two_structural_tiers_wait_for_their_own_clock'),
    ('M15_the_reask_interval_becomes_a_second_number', LIFECYCLE,
     '        return int(config.NAV_HISTORY_LOOKBACK_DAYS) + 1',
     '        return 31',
     HOLD_TESTS, 'test_the_nav_lookback_has_one_home_for_both_questions'),
    # 第 54 轮 A-7：`--fix-wording` 这一支以前只有内部函数用例，CLI 层零判据 ⇒
    # 把 dry-run 那一支摘掉，全套绿灯一声不响，而"先看一眼"会真改回收站。
    ('M16_fix_wording_dry_run_actually_writes', SCRIPT,
     "            if not args.apply:\n"
     "                print('[dry-run] 一行都没动。真订正加 --apply --confirm %s' % CONFIRM_TOKEN)",
     "            if False:\n"
     "                print('[dry-run] 一行都没动。真订正加 --apply --confirm %s' % CONFIRM_TOKEN)",
     CLOSE_TESTS, 'test_fix_wording_dry_run_leaves_the_row_alone_and_says_so'),
]


def _run_one(test_file, test_name):
    env = dict(os.environ)
    # 放行标记：这一份 pytest 是我自己起的子会话，别被 conftest 那把体检锁拦死
    # （不带这个标记 ⇒ 每次跑批都"子会话起手就退、父进程把它记成红"，全是假红）。
    env[mutation_lock.ENV_PID] = str(os.getpid())
    env['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run(
        [sys.executable, '-m', 'pytest', test_file, '-q', '-k', test_name,
         '--no-header', '-p', 'no:cacheprovider'],
        capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)


def main(argv=None):
    ap = argparse.ArgumentParser(description='逻辑侧变异体检（摘掉判据，用例必须红）')
    ap.add_argument('--only', help='只跑 label 含这个词的变异')
    ap.add_argument('--list', action='store_true', help='只列出处，不碰任何文件')
    args = ap.parse_args(argv)

    todo = [m for m in MUTATIONS if not args.only or args.only in m[0]]
    if args.list:
        for label, path, _a, _m, test_file, test in todo:
            print('%-58s %s :: %s' % (label, path, test))
        print('共 %d 处变异，覆盖 %d 个用例文件'
              % (len(todo), len({m[4] for m in todo})))
        return 0
    if not todo:
        print('[abort] --only %r 一处都没匹配到 ⇒ 这次什么都没测' % args.only)
        return 4
    # 两把锁各管一个方向（与前端那份同一套）：会话锁被 pytest 握着就不启动；
    # 启动后自己握住体检锁，让并发的 pytest 起手就退出。
    if not mutation_lock.harness_may_start(ROOT):
        print('[abort] 已经有一个 pytest 会话握着 %s ⇒ 本次会就地改写 src/，'
              '两边并发时报出来的红绿都不作数。等它跑完再启动。' % mutation_lock.SESSION_NAME)
        return 5
    try:
        guard = mutation_lock.held_exclusively(ROOT)
        guard.__enter__()
    except RuntimeError as exc:
        print('[abort] %s' % exc)
        return 5

    cache, backups, rc = {}, [], 0
    try:
        for label, path, anchor, mutant, test_file, test in todo:
            full = os.path.join(ROOT, path)
            if path not in cache:
                cache[path] = io.open(full, encoding='utf-8', newline='').read()
            original = cache[path]
            # 换行符也算形状：仓库里 `src/` 有一批 CRLF 文件（git 检出按 `core.autocrlf` 决定），
            # 跨行锚点里写死的 `\n` 在那些文件上永远 0 次命中 ⇒ 按文件自己的换行重拼一次。
            nl = '\r\n' if '\r\n' in original else '\n'
            anchor_x = anchor.replace('\n', nl)
            mutant_x = mutant.replace('\n', nl)
            hits = original.count(anchor_x)
            if hits != 1 and original.count(anchor) == 1:
                anchor_x, mutant_x, hits = anchor, mutant, 1
            anchor, mutant = anchor_x, mutant_x
            if hits != 1:
                # 锚点不唯一/不存在 ⇒ 这条变异今天量不到任何东西，按失败处理（规矩①）
                print('%-58s ⇒ ANCHOR-MISS（锚点命中 %d 次）' % (label, hits))
                rc = 1
                continue
            backup = full + '.mutbackup'
            io.open(backup, 'w', encoding='utf-8', newline='').write(cache[path])
            backups.append((full, backup))
            tmp = full + '.mut.tmp'
            with io.open(tmp, 'w', encoding='utf-8', newline='') as fh:
                fh.write(cache[path].replace(anchor, mutant))
            os.replace(tmp, full)

            r = _run_one(test_file, test)
            out = (r.stdout or '') + (r.stderr or '')
            if '1 failed' in out and ('AssertionError' in out or 'assert' in out):
                # 只认"断言失败"那种红（同 `mutation_proof_frontend.py` 的规矩）：
                # 崩在外呼、夹具、导入上的红替被改的判据作不了保
                print('%-58s ⇒ RED（判据有效）' % label)
            elif r.returncode == 0:
                print('%-58s ⇒ GREEN（摘掉判据没人发现 ⇒ 那条用例只是在描述自己）' % label)
                rc = 1
            else:
                print('%-58s ⇒ HARNESS-FAIL（退码 %s，不是断言失败那种红 ⇒ 这条量不到）'
                      % (label, r.returncode))
                for line in out.splitlines()[-6:]:
                    print('      | %s' % line)
                rc = 1
            os.replace(backup, full)
    finally:
        for full, backup in backups:
            if os.path.exists(backup):
                os.replace(backup, full)
        for path, original in cache.items():
            disk = io.open(os.path.join(ROOT, path), encoding='utf-8', newline='').read()
            if disk != original:
                print('[还原失败] %s 与运行前不一致 ⇒ 这次的结果一概别信' % path)
                rc = 1
            else:
                print('已还原 %s' % path)
    return rc


if __name__ == '__main__':
    sys.exit(main())
