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

# 本机控制台默认 cp936，而这份工具的每一行回执都带 `⇒`（U+21D2，不在 cp936 里）。
# 输出重定向到文件时 python 仍按 locale 编码 ⇒ 第 57 轮实测：跑到 CONTROL 那行
# `UnicodeEncodeError` 直接崩，而**它崩之前已经抢了体检锁**，看起来像"体检自己坏了"。
# 判"跑没跑成"要看产物，别把编码当成故障源；这里显式收一遍，两个流都管。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):     # 已被换成别的对象（pytest 捕获、io.StringIO）
        pass

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
MIGRATE_TESTS = 'tests/unit/test_prediction_migrations.py'
RULER_TESTS = 'tests/unit/test_one_ruler_per_question.py'
MAINT = 'src/services/prediction_maintenance_service.py'
ROUTES = 'src/api/routes/predictions.py'
GAP_TESTS = 'tests/unit/test_sector_gap_fill.py'
REMAP_TESTS = 'tests/unit/test_sector_remap.py'
RETENTION = 'src/services/retention_three_buckets.py'
RETENTION_TESTS = 'tests/unit/test_retention_three_buckets.py'
CONFIG_ROUTES = 'src/api/routes/config.py'
CLEANUP_API_TESTS = 'tests/unit/test_retention_cleanup_api.py'
DB_SPACE = 'src/services/db_space.py'
DB_SPACE_TESTS = 'tests/unit/test_db_space.py'
VP_WORKFLOW = 'src/services/viewpoint_workflow_service.py'
VP_TESTS = 'tests/unit/test_viewpoint_refactor.py'
VP_BACKFILL = 'scripts/backfill_viewpoint_archive_stamps.py'
VP_BACKFILL_TESTS = 'tests/unit/test_backfill_viewpoint_archive_stamps.py'

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
    # ⚠ 第 55 轮 M-1 之后取数改走 `nav_backfill_days()` ⇒ 这两条锚点跟着改了形状
    #（旧锚点现在会 ANCHOR-MISS，而 ANCHOR-MISS 按规矩①就是失败 —— 这正是它存在的意义）。
    ('M14_both_tiers_share_the_credential_clock', LIFECYCLE,
     '    if verdict_reason in LOCK_ONLY_VERDICT_REASONS:\n'
     '        return nav_backfill_days() + 1\n',
     '    if False:\n'
     '        return nav_backfill_days() + 1\n',
     HOLD_TESTS, 'test_the_two_structural_tiers_wait_for_their_own_clock'),
    ('M15_the_reask_interval_becomes_a_second_number', LIFECYCLE,
     '        return nav_backfill_days() + 1',
     '        return 31',
     HOLD_TESTS, 'test_the_nav_lookback_has_one_home_for_both_questions'),
    # 第 55 轮 M-1：那句"重问节奏跟着常规同步的回补范围走"要有两处牙 ——
    # 取数处不许退化成常数，写同步的那几处也不许再把天数写死。
    ('M17_the_backfill_window_becomes_a_constant', LIFECYCLE,
     '    return int(config.NAV_HISTORY_LOOKBACK_DAYS) if days is None else int(days)',
     '    return 30 if days is None else int(days)',
     HOLD_TESTS, 'test_the_backfill_window_is_read_at_call_time_not_at_import'),
    ('M18_the_sync_writer_hard_codes_its_window_again', 'src/fund/fund_api.py',
     '    def update_fund_history(self, fund_code: str, days: Optional[int] = None,',
     '    def update_fund_history(self, fund_code: str, days: int = 30,',
     HOLD_TESTS, 'test_the_sync_lookback_is_not_hard_coded_at_any_call_site'),
    # 第 55 轮 M-2：补到净值要当场解得开那把锁 —— 三条各自的变异
    ('M19_the_sync_writer_stops_releasing_the_lock', 'src/fund/fund_api.py',
     '            if inserted:\n'
     '                from src.services.prediction_lifecycle import (\n',
     '            if False:\n'
     '                from src.services.prediction_lifecycle import (\n',
     HOLD_TESTS, 'test_the_nav_unlock_path_is_wired_into_every_nav_writer'),
    ('M20_releasing_ignores_what_actually_changed', LIFECYCLE,
     '        if changed and not any(start is not None and start <= d <= end'
     ' for d in changed):',
     '        if False:',
     HOLD_TESTS, 'test_a_backfill_outside_the_held_window_does_not_release_the_hold'),
    ('M21_releasing_forgets_to_ask_the_verifier', LIFECYCLE,
     '        if target_cannot_evidence_window(in_window, latest, start, end,'
     ' today=today) is None:',
     '        if True:',
     HOLD_TESTS, 'test_a_backfill_that_still_cannot_evidence_the_window_keeps_the_hold'),
    # 第 54 轮 A-7：`--fix-wording` 这一支以前只有内部函数用例，CLI 层零判据 ⇒
    # 把 dry-run 那一支摘掉，全套绿灯一声不响，而"先看一眼"会真改回收站。
    ('M16_fix_wording_dry_run_actually_writes', SCRIPT,
     "            if not args.apply:\n"
     "                print('[dry-run] 一行都没动。真订正加 --apply --confirm %s' % CONFIRM_TOKEN)",
     "            if False:\n"
     "                print('[dry-run] 一行都没动。真订正加 --apply --confirm %s' % CONFIRM_TOKEN)",
     CLOSE_TESTS, 'test_fix_wording_dry_run_leaves_the_row_alone_and_says_so'),
    # 第 55 轮：全量跑批里那条"回退要留痕"偶发红、单跑绿 —— 根因是 alembic 的 env.py 照抄了
    # 官方模板那句 `fileConfig(...)`，而它的 `disable_existing_loggers` 默认 True ⇒
    # 一次进程内迁移把全部 `src.*` logger 永久禁掉，那行 WARNING 根本没被创建过。
    ('M22_env_py_silences_the_app_loggers_again', 'alembic/env.py',
     'fileConfig(config.config_file_name, disable_existing_loggers=False)',
     'fileConfig(config.config_file_name)',
     MIGRATE_TESTS,
     'test_running_a_migration_in_process_does_not_silence_the_application_loggers'),
    # 同一处改动带出来的第二条：回退那行日志里"缺 tzdata"这三个字是这条判据唯一的抓手，
    # 把话改得含糊（"时区取不到"）也算把痕迹擦掉。
    ('M23_the_as_of_fallback_trail_stops_naming_tzdata', LIFECYCLE,
     "'容器缺 tzdata 时页面日期可能差一天', exc)",
     "'容器缺时区时页面日期可能差一天', exc)",
     'tests/unit/test_stats_evidence_report.py',
     'test_current_as_of_fallback_leaves_a_trail'),
    # 第 56 轮：四把判据各自"只修了一半"，每一半都补一处变异 ——
    # 上一轮这几条之所以是 GREEN，正因为尺子只量了形状的一半。
    ('M24_lookback_written_as_a_positional_argument', 'src/fund/fund_auto_manager.py',
     'fund_data_manager.update_fund_history(fund_code, db=db)',
     'fund_data_manager.update_fund_history(fund_code, 30, db=db)',
     HOLD_TESTS, 'test_the_sync_lookback_is_not_hard_coded_at_any_call_site'),
    ('M25_unlock_called_with_an_empty_list_of_dates', 'src/fund/fund_api.py',
     'release_holds_after_nav_commit(db, fund_code, inserted,',
     'release_holds_after_nav_commit(db, fund_code, [],',
     HOLD_TESTS, 'test_the_nav_unlock_path_is_wired_into_every_nav_writer'),
    ('M26_unlock_hidden_behind_an_always_false_comparison', SYNC,
     '            if added:\n',
     '            if added and 1 == 0:\n',
     HOLD_TESTS, 'test_the_nav_unlock_path_is_wired_into_every_nav_writer'),
    # 归档那把棘轮以前按 (文件, 函数) 收 ⇒ 已登记的函数里再长一处墙钟写它看不见
    ('M27_second_wall_clock_archive_stamp_inside_a_registered_function', SVC,
     '        prediction.deleted_at, prediction.restore_before = archive_stamp()\n',
     '        prediction.deleted_at, prediction.restore_before = archive_stamp()\n'
     '        prediction.deleted_at = datetime.now()   # 变异：第二处墙钟写同一列\n',
     RULER_TESTS, 'test_archiving_a_prediction_always_stamps_with_the_shared_clock'),
    # 第 57 轮 M-1 的第④格：解锁的"新落的那几天"先交进一个空容器变量、再递进去 ——
    # 与直接写 `[]` 是同一件事，而上一版只认字面量，换个变量名就量不到（M25 那处变异
    # 因此只钉了一半）。这条变异问的是**回溯那一腿有没有牙**。
    ('M28_unlock_dates_piped_through_an_empty_variable', SYNC,
     "                release_holds_after_nav_commit(db, fund_code, added, "
     "where='每日基金同步')\n",
     "                _empties = []\n"
     "                release_holds_after_nav_commit(db, fund_code, _empties, "
     "where='每日基金同步')\n",
     HOLD_TESTS, 'test_the_nav_unlock_path_is_wired_into_every_nav_writer'),
    # ── 第 66 轮 任务 #157：板块没有可用标的 ⇒ 从内置表补一只。五道门各一处变异 ──
    # 第 69 轮：服务层把 `own_answer` 那一个值换成 `kind, _reason, cause` 三格解包
    # ⇒ M29/M43 只**换锚点**，载荷与判据语义一字未动（`if False and …` / `if True:`
    # 还是原来那个注入）。换锚点不动判据这件事，写在提交说明里也要写。
    ('M29_the_guard_that_keeps_servable_rows_is_blind', MAINT,
     "            if kind in ('evidenced', 'not_due', 'unknown'):\n",
     "            if False and kind in ('evidenced', 'not_due', 'unknown'):\n",
     GAP_TESTS, 'test_a_row_that_can_already_be_evidenced_is_left_alone'),
    ('M30_mapping_rows_are_created_even_when_nothing_moves', MAINT,
     '        gap_used = sorted({candidate["sector"] for candidate in candidates\n'
     '                           if candidate["via_gap_fill"]})\n',
     '        gap_used = sorted(label for label in gap_plan\n'
     '                          if not gap_plan[label].get("refused"))\n',
     GAP_TESTS, 'test_a_fillable_sector_with_nothing_to_move_gets_no_row'),
    ('M31_an_existing_sector_row_is_duplicated_not_rewritten', MAINT,
     "        if cand['mode'] == 'update' and rows:",
     "        if False and rows:",
     GAP_TESTS, 'test_a_dead_sector_row_is_rewritten_in_place_not_added_alongside'),
    ('M32_the_owner_signed_target_is_overwritten', MAINT,
     '            if owner_backed:',
     '            if False and owner_backed:',
     GAP_TESTS, 'test_an_owner_picked_target_is_never_swapped_by_the_machine'),
    ('M33_a_missing_archive_still_buys_a_refusal_reason', MAINT,
     "        if not info:\n",
     "        if False and not info:\n",
     GAP_TESTS, 'test_a_nav_row_without_an_archive_is_still_refused_and_the_db_proves_why'),
    # ── 第 66 轮复评（72 分）返修：MA-1/MA-2/MA-3/MA-4/MI-4/MI-6 各一处 ──
    ('M34_the_rewritten_row_stays_invisible_to_the_next_sync', MAINT,
     '            row.is_active = True\n',
     '            row.is_active = row.is_active\n',
     GAP_TESTS, 'test_a_filled_row_is_usable_the_next_time_the_sync_runs'),
    ('M35_the_old_targets_score_keeps_the_new_row_ineligible', MAINT,
     '            row.confidence = None\n',
     '            row.confidence = row.confidence\n',
     GAP_TESTS, 'test_a_filled_row_is_usable_the_next_time_the_sync_runs'),
    ('M36_every_refusal_is_blamed_on_the_static_table', ROUTES,
     'f"{len(refused)} 个板块仍然没有可用标的：{detail}"',
     'f"{len(refused)} 个板块仍然没有标的：内置表也说不出对口品种"',
     GAP_TESTS, 'test_the_route_names_the_reason_it_actually_hit'),
    ('M37_the_receipt_counts_rows_it_never_moved', MAINT,
     '                    result["predictions_via_gap_fill"] += 1\n',
     '                    pass\n',
     GAP_TESTS, 'test_the_execute_sentence_counts_rows_it_really_moved'),
    ('M38_the_retag_gate_gets_the_whole_history_instead_of_the_window', MAINT,
     '                days, latest = window_from_calendar(\n'
     '                    calendar, candidate["new_fund_code"],\n'
     '                    prediction.prediction_date, prediction.target_date)\n',
     '                days = calendar.get(candidate["new_fund_code"]) or []\n'
     '                latest = max(days) if days else None\n',
     GAP_TESTS, 'test_the_evidence_handed_to_the_retag_gate_is_the_window_slice'),
    ('M39_affix_spellings_of_one_sector_stop_sharing_a_row', MAINT,
     '                for key in (self._gap_label(raw), raw):\n',
     '                for key in (raw,):\n',
     GAP_TESTS, 'test_two_affix_spellings_of_one_sector_share_one_plan_row'),
    # 第 66 轮返修："只紧不松"那道门**只**属于补标那一路。命中库里映射行的预测
    # 从来不许被它挡下（这一批我把归一后的标签当映射键去查，四条 `test_sector_remap`
    # 一起红就是这一族的现场）。`via_gap` 因此必须是显式旗标，不是"标签在不在计划表里"
    # 那种推断 —— 把旗标恒真，门就盖到了映射那一路。
    ('M40_the_tightening_gate_also_applies_to_mapped_rows', MAINT,
     '            pairs.append((prediction, mapping, key, via_gap))\n',
     '            pairs.append((prediction, mapping, key, True))\n',
     GAP_TESTS, 'test_a_sector_with_its_own_mapping_row_is_never_treated_as_a_gap_fill'),
    # 第 67 轮复评 MAJOR-3：归一里除了摘前后缀，还有一条**别名替换**会把标签改成另一块板块
    # （实测 `normalize_sector_name('债券')` = `'券商'`）。上一版 `_gap_label` 照单全收 ⇒
    # "内置表答不出就不猜"被改成"归到别的板块、于是答得出"，机器给债券板块绑了一只券商 ETF。
    # 这一处变异把"只认词形归一"那道判据摘掉：把 `norm if norm in sector else sector`
    # 换成 `norm`，补标那一路就会为 `债券` 写一行 `券商` 的映射。
    ('M41_a_normalization_that_renames_the_sector_buys_a_target', MAINT,
     '        return norm if norm in sector else sector\n',
     '        return norm\n',
     GAP_TESTS, 'test_a_normalization_that_renames_the_sector_buys_no_target'),
    # 第 67 轮复评 MAJOR-5：清了几条结论必须在**点执行之前**就看得见。逐行 `reset_verified`
    # 一直都在，可从没人把它数成回执里那句话 ⇒ 这个聚合数被摘成常数 0 时，页面那句
    # "预计更新 N 条"读起来就像"挪一挪没事"。
    ('M42_the_receipt_hides_how_many_conclusions_get_cleared', MAINT,
     '            "predictions_with_verdict": sum(1 for candidate in candidates\n'
     '                                            if candidate["reset_verified"]),\n',
     '            "predictions_with_verdict": 0,\n',
     GAP_TESTS, 'test_the_preview_says_out_loud_how_many_conclusions_it_will_clear'),
    # 第 67 轮复评 MAJOR-8：那道门放行的四格（真给得出 / 还没到期 / 没档案 / 起点说不清）
    # 被并成一个键，于是页面上那句"它们自己那只标的就给得出这段窗口的净值"对 22.8% 的行是假话。
    # 这一处把分档的那一句抹平 ⇒ 两档数合回一个数。
    ('M43_a_window_that_has_not_arrived_is_counted_as_evidenced', MAINT,
     "                if kind == 'evidenced':\n",
     '                if True:\n',
     GAP_TESTS, 'test_a_window_that_has_not_arrived_is_not_reported_as_evidenced'),
    # 同轮 MAJOR-9②：`in archived` 那一腿以前**没有任何一格用例问过**（换成恒真 40 条全绿）。
    # 悬空代码（净值有行、档案表里没有）不该被当成"给得出证据的自有标的"。
    ('M44_the_archive_check_is_skipped', MAINT,
     '                if (via_gap and prediction.fund_code and prediction.fund_code in archived) \\\n',
     '                if (via_gap and prediction.fund_code) \\\n',
     GAP_TESTS, 'test_a_target_with_no_archive_is_not_the_same_as_being_evidenced'),
    # 同轮 MAJOR-9③：完成时那句必须念**改完之后回查**的那份数，不是建候选时的计划（第 51 轮 B-2 一族）。
    # 上一版那条用例里两个数恰好相等 ⇒ 换成计划那一份它一声不响。
    ('M45_the_execute_sentence_reads_the_plan_not_the_recount', ROUTES,
     "        via_done = result.get('predictions_via_gap_fill') or 0\n",
     "        via_done = result.get('predictions_via_gap_fill_planned') or 0\n",
     GAP_TESTS, 'test_the_execute_sentence_uses_the_recount_not_the_plan'),
    # 同轮 MINOR-10：六种拒收里三种（长名 / 同码 / 零净值）从没被走到过 —— 那句话的计数分支
    # 是空跑出来的。把 `same_as_current` 那一档的判定摘掉 ⇒ 三条新断言必须当场红。
    # 第 68 轮复评 MAJOR-1：完成时那句「带着已判结论 ⇒ 已清掉」念的是建候选时的计划数。
    # 摘掉 `dry_run` 这一腿 ⇒ 执行也照说那一句 ⇒ 新判据必须红（M45 同族，只差这句没接）。
    ('M47_the_receipt_promises_what_the_plan_said', ROUTES,
     '        if dry_run and wiped:\n',
     '        if wiped:\n',
     GAP_TESTS, 'test_the_verdict_count_is_promised_in_the_preview_not_claimed_in_the_receipt'),
    # 第 68 轮复评 MINOR-1：`unknown` 被并进“还没到期”那一档 ⇒ 页面上那句“等到期那天再说”
    # 对“有档案、库里一行净值都没有”那些行是假话（缺的是净值行，不是日历）。
    # 把第三档的计数并回第二档 ⇒ 新判据必须红。
    ('M48_the_unknown_answer_is_folded_back_into_not_due', MAINT,
     '                    kept_no_nav += 1\n',
     '                    kept_window_not_due += 1\n',
     GAP_TESTS, 'test_the_unknown_answer_gets_its_own_sentence_and_is_never_called_not_due'),
    # 同一批放行行在同一条消息里被说了两遍（2026-09-29 镜像真预览回执实测：
    # 481/155 各出现两次、两处措辞还不同 ⇒ 读的人只能猜是不是两批行）。
    # 摘掉“上面说过就不再说”那半个条件 ⇒ 新判据那条 count==1 必须红。
    ('M49_the_kept_buckets_are_described_twice', ROUTES,
     '        if kept and not buckets_spoken:\n',
     '        if kept:\n',
     GAP_TESTS, 'test_the_unknown_answer_gets_its_own_sentence_and_is_never_called_not_due'),
    # 第 69 轮复评 MINOR-4：`buckets_spoken` 只在 `kept` 一腿有牙 —— 摘掉 `waiting` /
    # `no_nav` / `no_start` 这三条腿上的同半个条件，同一批数就说两遍，
    # 而那份判据文件 24 条一声不响（评审席三次注入实测全绿）⇒ 每一腿各一处变异。
    ('M50_the_waiting_bucket_is_described_twice', ROUTES,
     '        if waiting and not buckets_spoken:\n',
     '        if waiting:\n',
     GAP_TESTS, 'test_the_unknown_answer_gets_its_own_sentence_and_is_never_called_not_due'),
    ('M51_the_no_nav_bucket_is_described_twice', ROUTES,
     '        if no_nav and not buckets_spoken:\n',
     '        if no_nav:\n',
     GAP_TESTS, 'test_the_unknown_answer_gets_its_own_sentence_and_is_never_called_not_due'),
    # 两种病因并回同一格 ⇒ 页面那句就给「窗口起点说不清」那半配错药（第 69 轮 MINOR-6）。
    # ⚠ 这一格第 69 轮只钉了**尺子**那一半，而当时给的理由写错了对象（第 70 轮复评 J-1）：
    # "`prediction_date` 是 NOT NULL ⇒ 夹具对它没有牙"只驳回**库行**夹具，我却据此把 M52 整条
    # 撤掉，等于顺手宣布"回执也配不出"。路由读的是服务交回的**那个计数**，换成桩就造得出
    # （同判据文件 `test_the_execute_sentence_uses_the_recount_not_the_plan`）。
    # 实际后果：路由里 `no_start` 那两句零判据零变异 ⇒ 摘掉 / 配错药 / 执行那一路缺席 /
    # 说两遍四种坏法由下面的 M55~M58 各自钉住（载荷落在**路由**那把，不是尺子那一格）。
    ('M53_the_two_answer_causes_are_folded_into_one', LIFECYCLE,
     "        return 'unknown', None, ('no_start' if start is None else 'no_nav')\n",
     "        return 'unknown', None, 'no_nav'\n",
     GAP_TESTS, 'test_the_answer_ruler_always_hands_back_three_slots'),
    # 少交一格（把 `cause` 摘掉）⇒ 调用方按三格解当场崩，那张形状表必须红。
    ('M54_the_answer_ruler_gives_back_only_two_slots', LIFECYCLE,
     "    return 'evidenced', None, None\n",
     "    return 'evidenced', None\n",
     GAP_TESTS, 'test_the_answer_ruler_always_hands_back_three_slots'),
    ('M46_the_same_code_as_current_is_not_a_refusal', MAINT,
     "                    'kind': 'same_as_current'}\n",
     "                    'kind': 'no_static_hit'}\n",
     GAP_TESTS, 'test_the_three_refusal_kinds_nobody_had_ever_asked_about'),
    # 第 70 轮复评 J-1（MAJOR）：路由里 `no_start` 那两句从此有牙，四种坏法各一处。
    # 载荷与还原都逐字对着 `data/_review_tmp/r70_probe_no_start.py` 的真实回执排过。
    ('M55_the_no_start_bucket_never_speaks', ROUTES,
     '            if no_start:\n',
     '            if False:\n',
     GAP_TESTS, 'test_the_no_start_sentences_are_pinnable_without_a_library_row'),
    ('M56_the_no_start_bucket_gets_the_wrong_medicine', ROUTES,
     'f"⇒ 补净值不会变，要动的是那条预测自己的起点日期")\n',
     'f"⇒ 现在判不了，跑一次「更新基金」把净值补齐再看")\n',
     GAP_TESTS, 'test_the_no_start_sentences_are_pinnable_without_a_library_row'),
    ('M57_the_no_start_leg_never_speaks_when_executing', ROUTES,
     '        if no_start and not buckets_spoken:\n',
     '        if False and not buckets_spoken:\n',
     GAP_TESTS, 'test_the_no_start_sentences_are_pinnable_without_a_library_row'),
    ('M58_the_no_start_bucket_is_described_twice', ROUTES,
     '        if no_start and not buckets_spoken:\n',
     '        if no_start:\n',
     GAP_TESTS, 'test_the_no_start_sentences_are_pinnable_without_a_library_row'),
    # 第 71 轮 #158 §C：真删崩在半路时，"已经删掉几行"这件事有三处出口（异常文本、
    # failed 台账、路由那句话）。每一处各坏一次，都由同一格行为判据点名。
    ('M59_the_abort_ledger_is_never_written', RETENTION,
     '                self.db.add(aborted)\n',
     '                pass\n',
     RETENTION_TESTS, 'test_an_abort_midway_reports_the_rows_already_gone'),
    # 台账写的是"计划里几行"而不是"已经消失几行" ⇒ 崩在半路那一次它替没删掉的行作保。
    ('M60_the_abort_ledger_counts_the_plan_not_the_committed', RETENTION,
     '                "failed", committed, dict(self._cascade_counts), datetime.now(), str(exc)\n',
     '                "failed", {k: len(v) for k, v in plan.candidate_ids.items()},\n'
     '                dict(self._cascade_counts), datetime.now(), str(exc)\n',
     RETENTION_TESTS, 'test_an_abort_midway_reports_the_rows_already_gone'),
    # 异常带着原始原因、却把已提交的数掏空 ⇒ 路由那句 task.error 只剩"模拟：…"，
    # "已经删掉 N 行"这件事到不了页面（载荷掏空 counts 而不是换异常类型：后者会让
    # pytest.raises 那一腿以"没抛出预期的异常"红，那按本工具的规矩算 HARNESS-FAIL）。
    ('M61_the_abort_exception_loses_the_committed_counts', RETENTION,
     '            raise CleanupInterrupted(committed, log_id, exc) from exc\n',
     '            raise CleanupInterrupted({}, log_id, exc) from exc\n',
     RETENTION_TESTS, 'test_an_abort_midway_reports_the_rows_already_gone'),
    # 第 71 轮 #158 §E：「回收磁盘空间」那一步有五种"没跑/没成"的形状，旧路由把它们和
    # "真跑完了"并成一句好话（`success = bool(ok) or bool(skipped)`）。这一处把跳过那一档
    # 的 success 翻回 True ⇒ 页面上"空间还欠着"与"空间已还"同形。
    ('M62_the_reclaim_success_flag_is_folded_with_skipped', CONFIG_ROUTES,
     '            "success": False,\n            "skipped": True,\n',
     '            "success": True,\n            "skipped": True,\n',
     CLEANUP_API_TESTS, 'test_a_reclaim_that_never_ran_is_never_reported_as_done'),
    # 服务层那圈"失败只记录不抛"的兜底，把异常换成一句"成功了"：删除本身确实成功了，
    # 可回收空间这一步一个字没做，回执却带着 success —— 由同一格服务层判据点名。
    ('M63_the_reclaim_wrapper_swallows_and_still_says_done', RETENTION,
     '            return {"success": False, "error": str(exc), "tables": {}}\n',
     '            return {"success": True, "error": str(exc), "tables": {}}\n',
     RETENTION_TESTS, 'test_a_reclaim_that_raised_is_reported_as_not_done'),
    # 第 71 轮 #158 §G：Postgres 是按表 VACUUM 的，数据库把原因逐表答出来，而页面上那一栏
    # 读的是顶层 error。上一版顶层只有 `success: False` ⇒ "数据库明明答了、页面上问不出来"
    # （本地 sqlite 整库那一路反而看不见，生产恰好是 PG 这一路）。摘掉服务层替页面补的那一句。
    ('M64_the_pg_reason_stays_where_the_database_put_it', DB_SPACE,
     '        payload["error"] = failure_detail(payload)\n',
     '        pass\n',
     DB_SPACE_TESTS, 'test_a_per_table_failure_puts_its_reason_where_the_page_reads_it'),
    # 「这次没跑成的原因是什么」有一把共用的尺子（failure_detail），两处消费方读同一句话。
    # 这一处让路由不再问它 ⇒ 回执那句只剩一个死字符串，页面上永远看不到数据库给的原因。
    ('M65_the_route_stops_asking_the_shared_ruler', CONFIG_ROUTES,
     '            "message": f"空间回收没有完成：{failure_detail(result)}",\n',
     '            "message": "空间回收没有完成：数据库没给出原因",\n',
     CLEANUP_API_TESTS,
     'test_a_pg_reclaim_failure_reaches_the_route_from_the_same_slot_the_page_reads'),
    # 同一件事的第二种坏法：路由自己按 tables 再拼一句（第二把尺子）。顶层已经写了这句话，
    # 于是两处措辞不同形，而判据第一格钉的正是"路由不许绕过共用尺子按表再拼一遍"。
    ('M66_the_route_derives_the_reason_a_second_time', CONFIG_ROUTES,
     '            "message": f"空间回收没有完成：{failure_detail(result)}",\n',
     '            "message": "空间回收没有完成：" + "; ".join(\n'
     '                f"{name}：" + str(entry.get("error"))\n'
     '                for name, entry in (result.get("tables") or {}).items()\n'
     '                if isinstance(entry, dict) and not entry.get("success")),\n',
     CLEANUP_API_TESTS,
     'test_a_pg_reclaim_failure_reaches_the_route_from_the_same_slot_the_page_reads'),
    # 共用那把尺子自己的第一档：数据库在顶层已经答了（sqlite 整库那一路）⇒ 原样带出。
    # 摘掉这一档 ⇒ 真原因被"数据库没给出原因"顶掉，而它是驱动逐字给的那句话。
    ('M67_the_shared_ruler_ignores_what_the_database_gave', DB_SPACE,
     '    error = result.get("error")\n    if error:\n',
     '    error = result.get("error")\n    if False:\n',
     DB_SPACE_TESTS, 'test_the_failure_reason_has_one_home_and_prefers_what_the_database_gave'),
    # M-2 那把尺子自己的三档（第 72 轮）：摘掉字典 ⇒ 键名搬上屏幕；不盖 reason_text ⇒
    # 「没跑」的出口只剩键；路由自己再翻译一遍 ⇒ 同一个原因两处两种口径。
    ('M68_the_skip_ruler_only_repeats_the_machine_key', DB_SPACE,
     '    return _SKIP_SENTENCES.get(reason, f"空间回收没跑：{reason}")\n',
     '    return f"空间回收没跑：{reason}"\n',
     DB_SPACE_TESTS, 'test_every_skip_exit_says_its_reason_in_human_words'),
    ('M69_the_skip_exit_stops_stamping_the_sentence', DB_SPACE,
     '    result["reason_text"] = skip_detail(result)\n',
     '    pass\n',
     DB_SPACE_TESTS, 'test_every_skip_exit_says_its_reason_in_human_words'),
    ('M70_the_route_translates_the_skip_key_a_second_time', CONFIG_ROUTES,
     '            "message": skip_detail(result),\n',
     '            "message": "空间回收没跑：" + str(result.get("reason")),\n',
     CLEANUP_API_TESTS, 'test_a_reclaim_that_never_ran_is_never_reported_as_done'),
    # 任务 #142：AI 判拒绝 ⇒ 进回收站那一站，今天第一次有了牙。三处各问一件不同的事：
    # ⑴ 那一对时间戳到底写不写（不写 ⇒ 那行永远进不了清理候选，正是 #142 的本体）；
    # ⑵ 保留天数是从**三桶策略**拿的还是这里自己抄了一个数（抄了 ⇒ 策略一改两边漂开）；
    # ⑶ 写进去的值是不是出自**那只共用的钟**（别处拿墙钟自己算 ⇒ 同座回收站两种"保留到 X 日"，
    #     这一格由归档那把棘轮的"来路"腿负责，不是由行为用例负责）。
    ('M71_the_rejected_viewpoint_gets_no_archive_stamp', VP_WORKFLOW,
     '            viewpoint.deleted_at, viewpoint.restore_before = archive_stamp(\n'
     '                retention_days=ThreeBucketPolicy().deleted_viewpoint_days)\n',
     '            pass\n',
     VP_TESTS, 'test_deep_analysis_rejects_emotional_content_via_soft_delete'),
    ('M72_the_rejected_viewpoint_hard_codes_its_retention', VP_WORKFLOW,
     '            viewpoint.deleted_at, viewpoint.restore_before = archive_stamp(\n'
     '                retention_days=ThreeBucketPolicy().deleted_viewpoint_days)\n',
     '            viewpoint.deleted_at, viewpoint.restore_before = archive_stamp(\n'
     '                retention_days=30)\n',
     VP_TESTS, 'test_the_retention_days_for_a_rejected_viewpoint_comes_from_the_policy'),
    ('M73_the_rejected_viewpoint_stamps_with_a_different_clock', VP_WORKFLOW,
     '            viewpoint.deleted_at, viewpoint.restore_before = archive_stamp(\n'
     '                retention_days=ThreeBucketPolicy().deleted_viewpoint_days)\n',
     '            viewpoint.deleted_at = datetime.now()\n'
     '            viewpoint.restore_before = datetime.now() + timedelta(days=30)\n',
     RULER_TESTS, 'test_archiving_a_prediction_always_stamps_with_the_shared_clock'),
    # 任务 #142 **第二半**（给存量那 18 行补戳的一次性脚本）。三条各问一件不同的事，
    # 全部落在 `scripts/` 而不是 `src/`：这个体检工具本来就按仓库相对路径读文件，
    # 而"一次性脚本"恰恰是最容易没人盯的那一族（第 55 轮那次镜像 CONTROL-RED 的备份残渣就在 scripts/）。
    # M74：保留天数有没有第二处出处；M75：那一对的值是不是出自那只钟（归档棘轮的"来路"腿）；
    # M76：说不清来路的行有没有被拿今天冒充。
    # ⚠ M75 的**边界**（2026-09-29 实测，写给下一轮的我）：只把 `stamp_from == 'today'`
    #   **那一臂**换成墙钟，归档棘轮**不红** —— `_stamp_names` 收的是**整个函数**的名字集合，
    #   另一臂那次诚实的 `archive_stamp(...)` 就把 `stamped` 这个名字买了过去。
    #   两臂一起换才红 ⇒ 载荷必须两臂都换，别只改一臂然后说"这条有牙"。
    ('M74_the_backfill_hard_codes_its_retention', VP_BACKFILL,
     '    return ThreeBucketPolicy().deleted_viewpoint_days\n',
     '    return 30\n',
     VP_BACKFILL_TESTS, 'test_the_retention_window_is_the_policys_number_not_a_hard_coded_thirty'),
    ('M75_the_backfill_stamps_with_a_different_clock', VP_BACKFILL,
     '        if stamp_from == \'today\':\n'
     '            stamped = archive_stamp(retention_days=retention_days)\n'
     '        else:\n'
     '            stamped = archive_stamp(retention_days=retention_days, base=v.created_at)\n',
     '        if stamp_from == \'today\':\n'
     '            stamped = (datetime.now(), date.today())\n'
     '        else:\n'
     '            stamped = (v.created_at, date.today())\n',
     RULER_TESTS, 'test_archiving_a_prediction_always_stamps_with_the_shared_clock'),
    ('M76_the_backfill_invents_a_stamp_for_a_row_that_cannot_say', VP_BACKFILL,
     '            if not isinstance(v.created_at, datetime):\n',
     '            if False:\n',
     VP_BACKFILL_TESTS, 'test_a_row_that_cannot_say_when_it_arrived_is_refused_not_invented'),
    # 两种缺口并成一组 ⇒ 脚本那句"现状"就退化成一句"缺那一对时间戳"，
    # 对 400 行（已在清理候选里）是反话、对 18 行（永远进不了桶）是说轻了。
    ('M77_the_backfill_folds_the_two_gaps_into_one', VP_BACKFILL,
     '    no_stamp = [v for v in rows if v.deleted_at is None]\n',
     '    no_stamp = list(rows)\n',
     VP_BACKFILL_TESTS, 'test_the_two_gaps_are_not_one_gap_and_the_cleaner_only_ever_sees_one_of_them'),
    # 2026-09-30 生产那句假话的两条腿（"其余 343 行要到各自那个保留日之后"）。
    # M78：选中数**不读真尺子**，改回脚本自己按窗口算 ⇒ 生产那种"额度被前面的桶吃光"的形状
    #      会被报成"今天就有 411 行进了候选"，正是我上一版印出去的那句话。
    ('M78_the_receipt_answers_with_its_own_arithmetic', VP_BACKFILL,
     '    got = set(plan.candidate_ids[ThreeBucketRetentionService.BUCKET_DELETED_VP])\n',
     '    got = set(i["viewpoint_id"] for i in items if i["until"] <= today)\n',
     VP_BACKFILL_TESTS, 'test_a_budget_starved_row_is_never_called_a_calendar_case'),
    # M79：把"缺额度"那句摘掉 `if past_due` 那道门 ⇒ 对一行**只是还没到保留日**的行也说"缺的是
    #      额度不是日历"，与 M78 方向相反、同一族（两句话并成一句），各咬一次。
    ('M79_the_budget_sentence_blames_capacity_for_a_calendar_case', VP_BACKFILL,
     '        if past_due:\n',
     '        if True:\n',
     VP_BACKFILL_TESTS, 'test_a_row_that_really_has_not_arrived_is_still_described_as_a_calendar_case'),
    # 2026-09-30 第 78 轮那两条 MINOR 落地时补的三条。前两条各有明确病因，第三条是我自己在返修
    # 途中现读出来的：**分类式与那把真尺子差一天**（清理问 `deleted_at < combine(today-N, 00:00)`，
    # 而 `until = 归档日 + N` ⇒ 等价写法是 `until < today`；`<=` 把 `restore_before == today`
    # 那一行算成"窗口已过"，于是对它说"缺的是额度"——生产与镜像现读差的正是这一行：411 对 410）。
    ('M80_the_past_due_classifier_is_one_day_looser_than_the_ruler', VP_BACKFILL,
     "        past_due = [i for i in starved if i['until'] < today]\n",
     "        past_due = [i for i in starved if i['until'] <= today]\n",
     VP_BACKFILL_TESTS, 'test_a_deadline_that_arrives_today_is_not_yet_past_due_on_the_real_ruler'),
    # `apply_backfill` 里那两处防御分支：写之前问"这一行还在回收站吗""那一列已经有值了吗"。
    # 建计划与真写之间隔着备份与逐行回执，那一行随时可能被人还原/别人补过戳。摘掉任何一处
    # 都是把别人的写盖掉、或给一行已经活过来的行补上归档时刻（＝当场送进清理候选）。
    ('M80b_the_recycle_bin_guard_is_blind_at_write_time', VP_BACKFILL,
     '        if not v.is_deleted:\n',
     '        if False:\n',
     VP_BACKFILL_TESTS, 'test_a_row_that_left_the_recycle_bin_between_plan_and_write_gets_no_stamp'),
    ('M80c_the_already_stamped_guard_is_blind_at_write_time', VP_BACKFILL,
     '        if v.deleted_at is not None and v.restore_before is not None:\n',
     '        if False:\n',
     VP_BACKFILL_TESTS, 'test_a_row_someone_else_stamped_in_the_meantime_is_never_overwritten'),
    # ── 任务 #171「到期了、结论还没有、而现在挂着的那只问不出这段净值 ⇒ 换一只问得出的」──
    # 这一段的行为判据落在两个文件里：库里映射那一路由 `test_sector_remap.py` 负责，
    # 内置补标表答得出的那一路由 `test_sector_gap_fill.py` 负责（镜像现读的 6 条行里
    # `2303/2304/3076` 只有后者给得出，早期版本把备选只数成 `library_hits[1:]` 就漏了它们）。
    # M81 摘掉 pass-1 那个例外 ⇒ 第二根标签根本进不了补标计划表（预览说"这块要补一只"
    # 却一条都不动），M82 摘掉 pass-2 的合并 ⇒ 计划表给了标的也不许当备选。
    ('M81_the_second_label_never_reaches_the_gap_plan', MAINT,
     '                if answered and not _waiting_for_a_verdict(prediction):\n',
     '                if answered:\n',
     GAP_TESTS, 'test_a_stale_mapping_row_does_not_blind_the_second_sector_label'),
    ('M82_the_builtin_plan_is_never_an_alternate', MAINT,
     '                hits, alts = library_hits, library_hits[1:] + plan_hits\n',
     '                hits, alts = library_hits, library_hits[1:]\n',
     GAP_TESTS, 'test_a_stale_mapping_row_does_not_blind_the_second_sector_label'),
    # M83 把"只等结论的那些行才有资格被换"整条关掉 ⇒ 停在验不了的标的上、到期没有结论的行
    # 永远原样待着（老板那句"换成别的基金"落空），方向与 M84/M85 相反：那一格是**换过头**。
    ('M83_a_stuck_row_is_counted_as_unchanged', MAINT,
     '                if alts and _waiting_for_a_verdict(prediction):\n',
     '                if False and alts:\n',
     REMAP_TESTS, 'test_a_stuck_row_moves_to_the_second_label_when_the_first_one_cannot_be_judged'),
    # M84：回落那一路只允许在"这段净值不会再来了"（`cannot`）时动。换成恒真 ⇒ `unknown` 的
    # 两格病因（库里一笔净值都没有＝跑一次「更新基金」；起点说不清＝改那条预测的日期）
    # 也会被换成别的标的 —— 把补数据的活计说成换基金，正是第 69 轮那把尺子要拦的形状。
    ('M84_the_fallback_moves_rows_that_only_need_a_sync', MAINT,
     "            if kind == 'cannot':\n",
     '            if True:\n',
     REMAP_TESTS, 'test_no_nav_in_the_library_is_a_sync_job_not_a_reason_to_change_fund'),
    # M85：备选自己也得问得出这段窗口才许换。摘掉这一问 ⇒ 从一只停更的搬到另一只停更的，
    # 还清掉了原标的上本来就问不出的那些证据（这一格是回落那一路唯一会写错的方向）。
    ('M85_the_alternate_is_taken_without_being_asked', MAINT,
     '                    if calendar_gap(calendar, alt.fund_code,\n'
     '                                    prediction.prediction_date, prediction.target_date):\n'
     '                        continue\n',
     '                    pass\n',
     REMAP_TESTS, 'test_an_alternate_that_cannot_be_evidenced_either_is_not_a_way_out'),
    # ── 任务 #172「归一化把板块改成另一块板块 ⇒ 查映射那一腿被它骗走」──
    # 生产 2026-09-30 现读：144 行待改标里 **81 行**是 `金融 → 黄金`（`SECTOR_ALIASES`
    # 149 条里 22 条是单字别名，`normalize_sector_name` 第 5 步做的是 `if alias in sector`
    # 的子串匹配）。第 67 轮 MAJOR-3 那道"归一结果必须是原样标签的子串"的门当时只装在
    # `_gap_label`（补标那一路），查映射这一路没有 ⇒ 同一个词形归一、两条路两种待遇。
    # M86 = 把那道门摘掉（回到裸 `normalize_sector_name`）：`金融` 会命中 `黄金` 那行映射，
    # 那条预测被改标到 `518880` 那一行挂的标的上 ⇒ 负面对照点红。
    ('M86_the_normalizer_is_allowed_to_rename_the_sector', MAINT,
     '            normalized = self._gap_label(sector)\n',
     '            from src.constants.sector_fund_map import normalize_sector_name\n'
     '            normalized = normalize_sector_name(sector)\n',
     REMAP_TESTS, 'test_a_normalization_that_renames_the_sector_buys_no_mapping_row'),
    # M87 反方向：把归一那一腿整条不要了（恒等）。`黄金行情 → 黄金` 那种"摘掉后缀"的
    # 正常词形归一从此查不到库行 ⇒ 正面控制点红。
    # ⚠ 为什么必须有这一条：少了它，M86 的修法可以被"归一那一路干脆删掉"满足，
    # 而那会顺手打死第 66 轮 ⑨ 之前就一直在跑的前缀/后缀剥离（`RMAP白酒` 那一族）。
    ('M87_the_normalization_leg_never_runs_at_all', MAINT,
     '            normalized = self._gap_label(sector)\n',
     '            normalized = sector\n',
     REMAP_TESTS, 'test_an_affix_spelling_still_finds_the_row_it_normalized_to'),
    # M88（第 85 轮 A-5）：库里别名那一臂**必须收原样串**（第 66 轮 ① 的教训，当时只有一条
    # 判据 `test_alias_lets_synonym_sector_match` 替它作保、没有变异 ⇒ "有判据"与"判据有牙"是两件事）。
    # ⚠ 那条老判据第一次跑它是 **GREEN**（判据无效）。根因**不是"结构上无牙"**
    # （第 85 轮 MI-4 驳回我上一版那句"两种都不红"）：它取决于 `_DB_ALIASES_CACHE` 那份
    # **进程级、只填一次**的缓存处在哪个跑序。只读门现测（命令与两份输出逐字写在
    # `docs/模块总览/板块与基金匹配.md` 那一节末尾）：
    #   缓存含 `RMAP绿色电力→RMAP绿电` ⇒ 归一第 3 步命中 ⇒ 交回 `RMAP绿电`，
    #     它不是原标签的字面 ⇒ `_gap_label` 打回原样 ⇒ ③换不换键**等价**（老判据不红）；
    #   缓存**空** ⇒ 归一第 4 步被子串 `电力` 命中 ⇒ 交回 `电力`，**是**原标签的字面 ⇒ 放行
    #     ⇒ ③的键真变了、表里没有 `电力` 那一行 ⇒ **老判据在这一格会红**。
    # 载荷不变、**判据换成有牙的那一格**：`生物医药` 由硬编码 `SECTOR_ALIASES`（第 2 步，排在
    # 读库内别名之前）归成 `医药` ⇒ 两种缓存状态下都交回 `医药`（实测两行相同），牙不随跑序漂；
    # 映射表里没有 `医药` 这一行**由那条用例自己的断言证明**（第 85 轮 MI-3：上一版靠别的文件清表）。
    # 现读 A/B：干净 `[updated] 1 / fund_code RMAP01`、
    # 落载荷 `[updated] 0 / no_mapping 1 / fund_code 仍是 999999`。
    ('M88_the_alias_arm_is_asked_with_the_normalized_label', MAINT,
     '            if alias_targets.get(sector) in sector_map:\n'
     '                return sector_map[alias_targets[sector]]\n',
     '            if alias_targets.get(normalized) in sector_map:\n'
     '                return sector_map[alias_targets[normalized]]\n',
     REMAP_TESTS, 'test_the_library_alias_arm_is_asked_with_the_raw_label_not_the_normalized_one'),
]


def _drop_bytecode(full):
    """改完磁盘上的 `.py` 就把它的 `.pyc` 一起放下 —— **落载荷与还原两处都要**，缺一处就假。

    2026-09-29 实测撞到的：M48 那条变异的载荷与原行**字节数完全相同**（当时那两个计数名
    `kept_answer_unknown` / `kept_window_not_due` 都是 19 个字符），而 CPython 判
    "源码变没变"看的是 mtime + size 这一对（PEP 552 的时间戳 pyc 头只记**整秒**）。
    ⚠ 归因必须说准，否则下一轮会把它当"偶发、可忍"：**这不是撞运气** —— 备份
    `.mutbackup` 写在落载荷前几毫秒，而 `os.replace` 在 Windows 上保留**被移进来那个文件**
    的 mtime ⇒ 还原后源文件的 `(int(mtime), size)` 与载荷那一轮编出的 `.pyc` 头**逐字相等**，
    陈旧缓存**几乎必然**被吃（评审席的时序探针实测：还原后 import 仍回 MUTAT，清了才回 CLEAN）。
    后果：下一轮 CONTROL 在"干净代码"上量到的其实是**上一处的变异**
    （那次表现为 CONTROL-RED 整轮作废，退 4）。
    方向有两个，所以两处都得清：陈旧缓存既能让变异**失效**（假 GREEN），
    也能让它**残留**（假 RED）。删不掉就当看不见，不抛 —— 缓存不是判据的一部分。
    """
    d = os.path.join(os.path.dirname(full), '__pycache__')
    try:
        stem = os.path.splitext(os.path.basename(full))[0]
        for name in os.listdir(d):
            if name.startswith(stem + '.'):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    except OSError:
        pass


def _run_stamp():
    """北京时刻（体检日志要能与"哪一次跑的"对上，见 main 里那行 `# run @`）。

    ⚠ **不 import `src.*`**：这一份进程会在跑期间就地改写 `src/`，而 `src.services…` 一被导入
    就可能按当时的 `.env` 建起全局 engine（本仓第 33/42 轮那条老规矩）⇒ 这里只按固定偏移算钟，
    不碰应用代码。时刻只用于日志署名，不参与任何判定。
    """
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _git_head():
    """当前 commit（短 sha）；拿不到就回 `unknown`，不许拿静态串冒充。"""
    import subprocess
    try:
        out = subprocess.run(['git', '-C', ROOT, 'rev-parse', '--short=12', 'HEAD'],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or '').strip() or 'unknown'
    except Exception:
        return 'unknown'


def _worktree_state():
    """跑当时的**工作树**状态：`clean` 或 `dirty(N)`（第 67 轮复评 MINOR-11）。

    为什么这一格必须有：上一版日志只有 `git=<HEAD>`，而体检读的是**磁盘上的文件** ——
    M40 那一处的锚点在它声称的那个 commit 里根本不存在（`git show 48c95e7:… | grep -c` 回 0），
    那 42 处跑的其实是当时还没提交的工作树。只看 `git=` 会把"跑在哪一版"读成"跑在 HEAD 上"，
    于是这份凭据指向一版从没存在过的代码。`dirty(N)` 就是那句实话：**这份日志不是 HEAD 的凭据**。
    """
    import subprocess
    try:
        out = subprocess.run(['git', '-C', ROOT, 'status', '--porcelain', '--',
                              'src', 'web', 'tests'],
                             capture_output=True, text=True, timeout=30)
        n = len([line for line in (out.stdout or '').splitlines() if line.strip()])
        return 'clean' if out.returncode == 0 and n == 0 else (
            'dirty(%d)' % n if out.returncode == 0 else 'unknown')
    except Exception:
        return 'unknown'


def _child_env():
    env = dict(os.environ)
    # 放行标记：这一份 pytest 是我自己起的子会话，别被 conftest 那把体检锁拦死
    # （不带这个标记 ⇒ 每次跑批都"子会话起手就退、父进程把它记成红"，全是假红）。
    env[mutation_lock.ENV_PID] = str(os.getpid())
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def _run_one(test_file, test_name):
    return subprocess.run(
        [sys.executable, '-m', 'pytest', test_file, '-q', '-k', test_name,
         '--no-header', '-p', 'no:cacheprovider'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=_child_env())


def _run_file(test_file):
    return subprocess.run(
        [sys.executable, '-m', 'pytest', test_file, '-q', '--no-header',
         '-p', 'no:cacheprovider'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=_child_env())


def _control(test_files):
    """干净代码上这些判据文件必须**先全绿**（第 55 轮 M-3 补上的那条腿）。

    第 33 轮 A-MAJOR-4 在前端那份里立的规矩，逻辑侧这份一直没接：
    基线本身红着的时候，"摘掉判据 ⇒ 用例变红"量的不是判据有效性，而是那个本来就存在的
    故障 —— 而它印出来的也是 `RED`、退码也是 1，读的人分不出来。
    """
    bad = []
    for f in sorted(test_files):
        r = _run_file(f)
        if r.returncode != 0:
            bad.append((f, r.returncode, (r.stdout or '') + (r.stderr or '')))
    return bad


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
    # 运行头（第 59 轮 MINOR-4）：这份日志以前**逐字节可复制** —— 同样 30 处全 RED 时，
    # round58 归档的那份与 round57 的 md5 相同（`19acd5b8…`），"我真的重跑过"这句话没有凭据。
    # 现在每次跑都落一行时刻 + 当时的 commit + 解释器 ⇒ 两次的日志不可能相同，也拦得住拿旧日志冒充。
    print('# run @ %s  git=%s  worktree=%s  python=%s  共 %d 处变异 / %d 个判据文件'
          % (_run_stamp(), _git_head(), _worktree_state(), sys.version.split()[0],
             len(todo), len({m[4] for m in todo})))
    try:
        bad = _control({m[4] for m in todo})
        if bad:
            for f, code, out in bad:
                print('%-58s ⇒ CONTROL-RED（退码 %s）' % (f, code))
                for line in [x for x in out.splitlines() if x.strip()][-4:]:
                    print('      | %s' % line)
            print('[abort] 干净代码上这些判据文件就是红的 ⇒ 下面每一处 RED 都不作数，'
                  '先修基线再跑体检')
            return 4
        print('%-58s ⇒ CONTROL-GREEN（%d 个判据文件在干净代码上全绿）'
              % ('CONTROL', len({m[4] for m in todo})))

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
            _drop_bytecode(full)        # 载荷必须真的被重新编译，否则量的是上一版

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
            _drop_bytecode(full)
    finally:
        for full, backup in backups:
            if os.path.exists(backup):
                os.replace(backup, full)
                _drop_bytecode(full)
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
