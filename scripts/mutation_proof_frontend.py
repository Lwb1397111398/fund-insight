# -*- coding: utf-8 -*-
"""把 `tests/unit/test_frontend_cold_start.py` 的每条判据各自退回"修复前的形状"，证明它会红。

为什么要有这个文件（第 29 轮两份复评共同点破）：那批判据是扫 HTML 文本的，
其中两条被证明**结构上不可能响**（一条比对字面 `「」`，而渲染出的空格子来自 `{{ }}` 插值；
一条用 `<th[^>]*>` 把属性吃掉，于是 `not any('title=' in ...)` 永远为真）。
文本判据只有配上"能把它打红的变异"才算数，所以变异不能只在我脑子里跑一遍就丢掉。

用法：
    python scripts/mutation_proof_frontend.py            # 全跑，任何一条绿就退码 1
    python scripts/mutation_proof_frontend.py --list     # 只看清单（末行打印处数与覆盖的判据数）

    注：docstring 里**不写**处数（第 32 轮 B 抓到那句"全跑（28 处）"早就过时）。
    要引用数量就跑 `--list`，别抄这里。

安全：**只改 `MUTATIONS` 里点到的那些 `web/` 文件**（第 36 轮 A-MINOR-2：这句原先手抄成
"只改 index.html / post-manager.js / prediction-manager.js"，而那时已有 5 处落在
`viewpoint-manager.js` 上 ⇒ 会过时的名单一律不写，改跑起来自己打印），
每处变异都**从干净底本**生成、写盘后回读核对（落了盘、且确实与底本不同）才跑 pytest，
跑完无条件写回底本并逐文件回读比对。
**不能与 `pytest tests/` 并发跑**（第 30 轮 B 实测：并发时会假报 12 条 GREEN + 3 条锚点失配，
还会留下未还原的文件）。这句话现在有代码拦着（第 32 轮补）：体检启动时抢
`src/utils/mutation_lock.py` 的 OS 级文件锁，抢不到直接拒绝；`tests/conftest.py` 那边
看到锁被持有就 `pytest.exit`。锁是操作系统管理的，进程被强杀也会自己放开。
"""
import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.utils import mutation_lock  # noqa: E402

HTML = 'web/index.html'
JS = 'web/prediction-manager.js'
POST = 'web/post-manager.js'
VP = 'web/viewpoint-manager.js'
T = 'tests/unit/test_frontend_cold_start.py'
WIRING = 'tests/unit/test_frontend_wiring.py'
# 判据不止一个文件：接线闸（`test_frontend_wiring.py`）也得能被自己的变异打红。
WIRING_TESTS = ('test_every_option_the_manager_reads_is_actually_injected',
                'test_everything_the_page_destructures_is_actually_exported')

# (判据函数, 变异名, 文件, 找, 换成, 是否正则)
def _drop_isServiceDown(match):
    # 正则变异用：从 `createViewpointManager({…})` 那一段里只去掉 `isServiceDown,` 那一行，
    # 其余原样留着（第 34 轮 A-MAJOR-2 就是这个注入被漏掉）。
    return re.sub(r'\n\s*isServiceDown,', '', match.group(0), count=1)


def _js(*lines):
    """跨行 JS 片段的拼装器：变异锚点必须能写出真实的缩进与换行。"""
    return chr(10).join(lines)

MUTATIONS = [
    ('test_first_load_fetches_all_go_through_the_wake_retry', 'unwrap_bloggers_fetch',
     HTML, "withWakeRetry(() => axios.get('/api/bloggers'))", "axios.get('/api/bloggers')", False),
    ('test_first_load_fetches_all_go_through_the_wake_retry', 'drop_injection_into_manager',
     HTML, r'[^\n]*\n[^\n]*\n[^\n]*withWakeRetry,\n', '\n', True),
    ('test_first_load_fetches_all_go_through_the_wake_retry', 'unwire_prediction_manager',
     JS, "wake(() => axios.get('/api/predictions/verify-all/status'))",
     "axios.get('/api/predictions/verify-all/status')", False),
    ('test_only_an_auth_rejection_may_clear_the_saved_password', 'auth_clears_on_any_failure',
     HTML, 'if (!isAuthRejection(e)) {', 'if (false) {', False),
    ('test_only_an_auth_rejection_may_clear_the_saved_password', 'service_down_ignores_5xx',
     HTML, r"\n\s*\|\| e\.response\.status === 502 \|\| e\.response\.status === 503 \|\| e\.response\.status === 504",
     '', True),
    ('test_the_wake_wait_is_shared_and_always_releases_the_flag', 'never_waits_for_wake',
     HTML, 'if (!(await waitUntilAwake())) throw e;', 'throw e;', False),
    ('test_the_wake_wait_is_shared_and_always_releases_the_flag', 'one_wait_per_caller',
     HTML, 'if (wakeWait) return wakeWait;', '', False),
    ('test_the_wake_wait_is_shared_and_always_releases_the_flag', 'flag_not_reset_in_finally',
     HTML, 'finally { serviceWaking.value = false; wakeWait = null; }', 'finally { wakeWait = null; }', False),
    ('test_a_missing_number_is_not_rendered_as_zero', 'statval_blurs_error',
     HTML, "const statVal = (v) => (statsError.value || v === undefined || v === null) ? '—' : v;",
     "const statVal = (v) => (v || 0);", False),
    ('test_a_missing_number_is_not_rendered_as_zero', 'card_back_to_bare_zero',
     HTML, '{{ statVal(stats.overall?.total_bloggers) }}',
     '{{ stats.overall?.total_bloggers || 0 }}', False),
    ('test_the_empty_state_cannot_lie_while_a_fetch_is_still_pending', 'empty_state_blames_the_database',
     HTML, r'<div v-else class="empty-state">.*?</div>',
     '<div v-else class="empty-state">\n                        <template v-if="bloggersError">{{ bloggersError }}</template>\n'
     '                        <template v-else>暂无博主数据</template>\n                    </div>', True),
    ('test_the_empty_state_cannot_lie_while_a_fetch_is_still_pending', 'error_cleared_on_entry',
     HTML, 'const fetchBloggers = async () => {\n                    try {',
     "const fetchBloggers = async () => {\n                    bloggersError.value = '';\n                    try {", False),
    ('test_realigned_note_cannot_render_an_unfilled_slot', 'core_guard_loosened',
     HTML, '<template v-if="m.realigned.core">', '<template v-if="m.realigned.core || m.realigned.kind">', False),
    ('test_realigned_note_cannot_render_an_unfilled_slot', 'drop_replaced_branch',
     HTML, r'[^\n]*<template v-else-if="m\.realigned\.replaced">[^\n]*\n', '', True),
    ('test_blogger_table_calibers_are_readable_without_hover', 'caliber_back_to_title',
     HTML, '<th>准确率（现算命中率）</th>', '<th title="现算命中率">准确率</th>', False),
    ('test_blogger_table_calibers_are_readable_without_hover', 'any_header_title_is_a_lie',
     HTML, '<th>排名</th>', '<th title="按命中率排">排名</th>', False),
    ('test_audit_filter_explains_itself_in_text_not_only_a_title', 'drop_filter_legend',
     HTML, r'<div v-if="identityFilter !== \'all\'" class="text-xs"[^\n]*>\n(?:[^\n]*\n){4}[^\n]*</div>',
     '', True),
    ('test_take_data_failure_says_so_instead_of_looking_like_an_empty_database', 'evidence_swallows_again',
     HTML, r"const fetchEvidence = async \(\) => \{.*?\n                \};",
     "const fetchEvidence = async () => { try { const res = await withWakeRetry(() => axios.get('/api/stats/evidence'));"
     " if (res.data.success) evidenceReport.value = res.data.data; } catch (e) { console.error(e); } };", True),
    # 下面三处打在**行为判据**上（node 真跑页面里那份源码），文本判据对它们是瞎的
    ('test_the_wake_retry_behaves_the_way_the_page_needs_it', 'never_waits_for_wake',
     HTML, 'if (!(await waitUntilAwake())) throw e;', 'throw e;', False),
    ('test_the_wake_retry_behaves_the_way_the_page_needs_it', 'gateway_not_service_down',
     HTML, r"\n\s*\|\| e\.response\.status === 502 \|\| e\.response\.status === 503 \|\| e\.response\.status === 504",
     '', True),
    ('test_the_wake_retry_behaves_the_way_the_page_needs_it', 'retries_on_401_too',
     HTML, 'if (!isServiceDown(e)) throw e;', '', False),
    # 第 30 轮 A 的探针：只测助手函数时这些都活得很好，现在由 `checkAuth` 那条行为判据接住
    ('test_check_auth_and_the_login_gate_behave_per_status_code', 'auth_rejection_widened_to_any_status',
     HTML, r"!!\(e && e\.response && \(e\.response\.status === 401 \|\| e\.response\.status === 403\)\)",
     '!!(e && e.response)', True),
    ('test_check_auth_and_the_login_gate_behave_per_status_code', 'waking_flag_never_set',
     HTML, '                    serviceWaking.value = true;\n                    wakeWait = (async () => {',
     '                    wakeWait = (async () => {', False),
    ('test_check_auth_and_the_login_gate_behave_per_status_code', 'pretends_already_awake',
     HTML, 'if (wakeWait) return wakeWait;', 'if (!wakeWait) { wakeWait = Promise.resolve(true); } return wakeWait;', False),
    ('test_check_auth_and_the_login_gate_behave_per_status_code', 'no_unreachable_modal',
     HTML, '                                serviceUnreachable.value = true;', '                                serviceUnreachable.value = false;', False),
    ('test_a_missing_number_is_not_rendered_as_zero', 'retention_card_back_to_zero',
     HTML, "{{ retentionPreview ? (retentionPreview.total_rows_removed || retentionPreview.total || 0) : '—' }}",
     '{{ retentionPreview?.total_rows_removed || 0 }}', False),
    ('test_a_lost_job_handle_needs_a_404_not_any_error', 'job_handle_dropped_on_any_error',
     'web/post-manager.js', 'error.response && error.response.status === 404', 'error.response', False),
    ('test_a_lost_job_handle_needs_a_404_not_any_error', 'retry_forever',
     'web/post-manager.js', 'attempts < MAX_POLL_FAILURES', 'true', False),
    ('test_everything_the_template_reads_is_actually_exported', 'unexport_serviceWaited',
     HTML, 'serviceWaking, serviceUnreachable, serviceProblem, serviceWaited, retryConnect,',
     'serviceWaking, serviceUnreachable, serviceProblem, retryConnect,', False),
    ('test_every_list_page_shares_the_same_honesty_rule', 'posts_blame_the_database',
     HTML, "{{ emptyText('posts') }}", '暂无帖子数据', False),
    ('test_the_weighted_column_shows_what_it_is_counted_over', 'weighted_base_hidden',
     HTML, '<span v-if="b.total_predictions !== b.hit_verified" style="color:#bfbfbf; font-size:11px;">（基数 {{ b.total_predictions ?? 0 }}）</span>',
     '', False),
    ('test_first_login_says_the_service_is_waking', 'first_login_silent_again',
     HTML, r'\s*<!-- 第一次输口令[^>]*-->\s*<p v-if="serviceWaking"[^>]*>\s*正在等待服务唤醒[^<]*</p>',
     '', True),
    ('test_every_list_page_shares_the_same_honesty_rule', 'mapping_page_swallows_again',
     HTML, "viewErrors.mappings = '板块映射拉取失败：' + (isServiceDown(e) ? '服务连不上（可能在唤醒）' : '接口报错');",
     "console.error('加载板块映射失败:', e);", False),
    ('test_a_missing_number_is_not_rendered_as_zero', 'statval_ignores_missing_field',
     HTML, "(statsError.value || v === undefined || v === null)", "(statsError.value)", False),
    ('test_the_empty_state_cannot_lie_while_a_fetch_is_still_pending', 'empty_state_reversed',
     HTML, r'<template v-if="serviceWaking">正在等待服务唤醒[^<]*</template>',
     '<template v-if="true">暂无数据</template>', True),
    # ---- 第 32 轮：五处"报 0 / 报没有"的假事实，和轮询耗尽后的那把锁 ----
    ('test_every_list_page_shares_the_same_honesty_rule', 'pagination_reports_zero_on_failure',
     HTML, '<template v-if="viewErrors.posts">条数没取到，表里是上一次取到的</template><template v-else>共 {{ postMeta.total || 0 }} 条</template>',
     '共 {{ postMeta.total || 0 }} 条', False),
    ('test_no_page_claims_a_number_it_never_measured', 'insight_card_back_to_zero',
     HTML, '{{ numOrDash(viewpointInsights.direction_total) }}',
     '{{ viewpointInsights.direction_total || 0 }}', False),
    ('test_no_page_claims_a_number_it_never_measured', 'advice_history_says_none',
     HTML, "{{ adviceError || '暂无历史建议' }}", '暂无历史建议', False),
    ('test_no_page_claims_a_number_it_never_measured', 'alias_tab_says_none',
     HTML, "{{ aliasError || '暂无自定义别名' }}", '暂无自定义别名', False),
    ('test_no_page_claims_a_number_it_never_measured', 'mapping_body_outside_the_guard',
     HTML, "{{ mappingSearchKeyword ? '未找到匹配的板块映射' : emptyText('mappings') }}",
     '暂无数据', False),
    ('test_no_page_claims_a_number_it_never_measured', 'advice_rejected_silently',
     HTML, "alert('这次没有生成建议：' + (res.data.message || '接口未给出原因'));",
     'void 0;', False),
    ('test_polling_gives_up_loudly_instead_of_locking_the_ui', 'exhaustion_keeps_the_global_lock',
     'web/post-manager.js',
     '                    postAnalysisRunning.value = false;\n                    analyzing.value = false;\n                    if (options.onPollStalled) options.onPollStalled(\'帖子批量分析\');',
     '', False),
    ('test_polling_gives_up_loudly_instead_of_locking_the_ui', 'viewpoint_exhaustion_silent',
     'web/viewpoint-manager.js',
     "                    analyzing.value = false;\n                    if (options.onPollStalled) options.onPollStalled('观点汇总');",
     '', False),
    ('test_polling_gives_up_loudly_instead_of_locking_the_ui', 'no_stall_banner',
     HTML, r'<div v-if="stallText" class="text-xs"[^\n]*</div>', '', True),
    ('test_failure_state_is_reported_and_cleared_by_the_fetch_itself', 'posts_success_false_silent',
     POST, "                report('帖子列表没取到：' + (res.data.message || '接口未给出原因'));",
     '                    void 0;', False),
    ('test_failure_state_is_reported_and_cleared_by_the_fetch_itself', 'posts_never_clears',
     POST, "                    report('');", '                    void 0;', False),
    ('test_failure_state_is_reported_and_cleared_by_the_fetch_itself', 'predictions_swallow_rejection',
     JS, "                report('预测列表拉取失败：' + (options.isServiceDown && options.isServiceDown(error)\n"
         "                    ? '服务连不上（可能在唤醒）' : '接口报错'));",
     '                void 0;', False),
    ('test_everything_the_template_reads_is_actually_exported', 'unexport_numOrDash',
     HTML, 'bloggersError, statsError, statVal, numOrDash, viewErrors, emptyText,',
     'bloggersError, statsError, statVal, viewErrors, emptyText,', False),
    # ---- 第 33 轮：200 + success:false 的剩余盲区、删除按钮的预览前提、TOP 弹窗口径 ----
    ('test_the_fund_view_says_so_when_the_api_says_no', 'funds_success_false_silent_again',
     HTML, "                            fundError.value = '基金列表没取到：' + (res.data.message || '接口未给出原因');",
     "                            console.error('基金接口返回失败');", False),
    ('test_the_fund_view_says_so_when_the_api_says_no', 'fund_tab_counts_blur_failure',
     HTML, "{{ fundError || fundLoading ? '—' : fundsWithPredictions.length }}",
     '{{ fundsWithPredictions.length }}', False),
    ('test_the_fund_view_says_so_when_the_api_says_no', 'page_scope_note_gone',
     HTML, r'\n\s*<div v-if="!fundError" class="text-xs" style="color: #8c8c8c; margin-top: 4px;">括号里是[^\n]*</div>', '', True),
    ('test_a_destructive_button_cannot_outlive_its_own_preview', 'retention_failure_keeps_delete_armed',
     HTML, "                            retentionPreview.value = null;\n                            cleanupEnabled.value = false;\n                            retentionPreviewError.value = '三桶预览没取到",
     "                            retentionPreviewError.value = '三桶预览没取到", False),
    ('test_a_destructive_button_cannot_outlive_its_own_preview', 'cleanup_success_false_silent',
     HTML, "                        } else {\n                            cleanupPreview.value = null;\n                            cleanupEnabled.value = false;\n                            cleanupPreviewError.value = '预览没取到：' + (res.data.message || '接口未给出原因');\n                        }",
     '                        }', False),
    ('test_the_config_modal_says_which_tab_failed', 'llm_tab_blank_again',
     HTML, r'\n\s*<div v-if="configTab === \'llm\' && configError"[\s\S]*?</div>\n', '\n', True),
    ('test_the_config_modal_says_which_tab_failed', 'test_data_tab_blank_again',
     HTML, r'\n\s*<div v-if="testDataError" class="empty-state"[\s\S]*?</div>\n', '\n', True),
    ('test_the_config_modal_says_which_tab_failed', 'load_config_swallows_rejection',
     HTML, "} else { configError.value = '配置没取到：' + (res.data.message || '接口未给出原因'); }",
     ' }', False),
    ('test_the_top_blogger_modal_puts_its_calibers_in_text_not_hover', 'top_header_caliber_hidden',
     HTML, '<th>命中率（判对 / 已验证）</th>', '<th>命中率</th>', False),
    ('test_the_top_blogger_modal_puts_its_calibers_in_text_not_hover', 'top_verified_column_swaps_denominator',
     HTML, '<td>{{ numOrDash(b.hit_verified) }}</td>',
     '<td>{{ b.hit_verified != null ? b.hit_verified : (b.total_predictions || 0) }}</td>', False),
    ('test_the_top_blogger_modal_puts_its_calibers_in_text_not_hover', 'metric_note_dropped',
     HTML, '<template v-if="topNote">接口自己的口径：{{ topNote }}</template>', '', False),
    ('test_every_list_page_shares_the_same_honesty_rule', 'mapping_success_false_silent',
     HTML, "} else { viewErrors.mappings = '板块映射没取到：' + (res.data.message || '接口未给出原因'); } } catch (e) {",
     ' } } catch (e) {', False),
    # ---- 第 34 轮批次：洞察卡的"没取到"、预览失败的解释、停摆横幅要能收 ----
    ('test_the_insight_cards_cannot_report_zero_before_the_insights_arrive', 'insights_never_marked_loaded',
     VP, '                    insightsLoaded.value = true;\n', '', False),
    ('test_the_insight_cards_cannot_report_zero_before_the_insights_arrive', 'insights_rejection_silent',
     VP, "                } else {\n                    forget();\n                    insightsError.value = '观点洞察没取到：' + (response.data.message || '接口未给出原因');\n                }",
     '                }', False),
    ('test_the_insight_cards_cannot_report_zero_before_the_insights_arrive', 'card_blames_the_database_again',
     HTML, "{{ insightsLoaded && viewpointInsights.pending_summary ? numOrDash(",
     '{{ viewpointInsights.pending_summary ? numOrDash(', False),
    ('test_a_failed_preview_explains_why_the_clean_up_buttons_are_held', 'no_word_about_held_buttons',
     HTML, r'\n\s*<span v-if="retentionPreviewError \|\| cleanupPreviewError" class="text-xs text-danger">[^\n]*</span>',
     '', True),
    ('test_the_stall_banner_is_taken_down_when_polling_recovers', 'banner_never_cleared',
     HTML, "                    onPollRecovered: (label) => { delete taskStalled[label]; },\n", '', False),
    ('test_the_stall_banner_is_taken_down_when_polling_recovers', 'post_poll_never_recovers',
     POST, r'\n\s*if \(options\.onPollRecovered\) options\.onPollRecovered\([^\n]*\);', '', True),
    # ---- 第 34 轮浏览器实测（把服务停掉）抓到的那一族：失败时卡上还挂着上一轮的真数 ----
    ('test_a_failed_insights_call_takes_the_four_cards_down_with_it', 'soft_failure_keeps_stale_cards',
     VP, "                    forget();\n                    insightsError.value = '观点洞察没取到：",
     "                    insightsError.value = '观点洞察没取到：", False),
    ('test_a_failed_insights_call_takes_the_four_cards_down_with_it', 'rejection_keeps_stale_cards',
     VP, "                forget();\n                insightsError.value = '观点洞察拉取失败：",
     "                insightsError.value = '观点洞察拉取失败：", False),
    ('test_a_failed_insights_call_takes_the_four_cards_down_with_it', 'success_without_payload_counts_as_loaded',
     VP, 'if (response.data.success && response.data.data) {', 'if (response.data.success) {', False),
    # ---- 第 34 轮浏览器实测（停服务点页面）照出来的另一族：旧数据冒充新数据 ----
    # （帖子那一腿已有 `pagination_reports_zero_on_failure`，这里补预测与观点两条）
    ('test_every_list_page_shares_the_same_honesty_rule', 'predictions_pager_claims_fresh_rows',
     HTML, '<template v-if="viewErrors.predictions">条数没取到，表里是上一次取到的</template>'
           '<template v-else>共 {{ predictionMeta.total || 0 }} 条</template>',
     '共 {{ predictionMeta.total || 0 }} 条', False),
    ('test_every_list_page_shares_the_same_honesty_rule', 'viewpoints_pager_has_no_failure_branch',
     HTML, '<template v-if="viewErrors.viewpoints">条数没取到，表里是上一次取到的</template>'
           '<template v-else>共 {{ viewpointMeta.total }} 条</template>',
     '共 {{ viewpointMeta.total }} 条', False),
    # ---- 接线闸自己的两条：把第 34 轮的两种漏法现场复现一遍 ----
    ('test_every_option_the_manager_reads_is_actually_injected[createViewpointManager]',
     'viewpoint_manager_never_gets_isServiceDown',
     HTML, r'window\.createViewpointManager\(\{[\s\S]*?\n                \}\);',
     _drop_isServiceDown, True),
    ('test_everything_the_page_destructures_is_actually_exported[createViewpointManager]',
     'manager_stops_exporting_insights_flag',
     VP, 'viewpoints, viewpointMeta, viewpointFilters, viewpointInsights, insightsLoaded, insightsError, '
        'summaryStatsLoaded, summaryStatsError, viewpointTask,',
     'viewpoints, viewpointMeta, viewpointFilters, viewpointInsights, summaryStatsLoaded, viewpointTask,',
     False),
    # ---- 第 35 轮 A-M1/M2/M3：把"写成功却说失败""预览失败还留着可执行按钮"各造回去一遍 ----
    ('test_a_write_that_succeeded_is_never_reported_as_a_failure', 'save_prediction_blames_the_write',
     JS, _js('try { await fetchPredictions(); }',
                       '                catch (refreshError) { alert(\'预测已保存，只是列表没刷新出来 —— 刷新页面即可\'); }'),
     _js('                await fetchPredictions();'), False),
    ('test_a_write_that_succeeded_is_never_reported_as_a_failure', 'cancel_job_blames_the_write',
     POST, _js('try { await refreshRelated(); }',
                       '                catch (refreshError) { alert(\'任务已取消，只是列表没刷新出来 —— 刷新页面即可\'); }'),
     _js('                await refreshRelated();'), False),
    ('test_a_write_that_succeeded_is_never_reported_as_a_failure', 'viewpoint_delete_blames_the_write',
     VP, _js('                // 永久删除已经执行完了，刷新失败不能说「删除失败」（那会让老板再点一次删另一条）',
             '                try {',
             '                    await Promise.all([fetchViewpoints(), fetchInsights()]);',
             '                    if (options.onStatsChanged) await options.onStatsChanged();',
             '                } catch (refreshError) { alert(\'观点已删除，只是列表没刷新出来 —— 刷新页面即可\'); }'),
     _js('                await Promise.all([fetchViewpoints(), fetchInsights()]);',
         '                if (options.onStatsChanged) await options.onStatsChanged();'), False),
    ('test_a_failed_preview_leaves_nothing_to_confirm', 'preview_refusal_still_arms_execute',
     JS, _js('if (!response || !response.data || response.data.success !== true) {',
                       "                    maintenanceError.value = '预览被拒绝：' +",
                       "                        ((response && response.data && response.data.message) || '接口没回 success:true');",
                       '                } else {',
                       '                    maintenancePreview.value = { type, message: response.data.message, data: response.data.data || {} };'),
     _js('                maintenancePreview.value = { type, message: response.data.message, data: response.data.data || {} };'), False),
    ('test_a_failed_preview_leaves_nothing_to_confirm', 'stale_preview_survives_the_retry',
     JS, _js('maintenancePreview.value = null;',
                       '            maintenanceError.value = \'\';'),
     _js('            maintenanceError.value = \'\';'), False),
    # ---- 第 35 轮 A 的 MINOR：翻页只回滚了抛错那一腿，软失败时页码还挂着 ----
    ('test_a_button_must_not_claim_the_opposite_of_what_happened', 'post_pager_ignores_soft_failure',
     POST, '            try { if (await fetchPosts() === false) postFilters.page = back; }',
     '            try { await fetchPosts(); }', False),
    ('test_a_button_must_not_claim_the_opposite_of_what_happened', 'prediction_pager_ignores_soft_failure',
     JS, '            try { if (await fetchPredictions() === false) predictionFilters.page = back; }',
     '            try { await fetchPredictions(); }', False),
    ('test_a_button_must_not_claim_the_opposite_of_what_happened', 'viewpoint_pager_ignores_soft_failure',
     VP, '            try { if (await fetchViewpoints() === false) viewpointFilters.page = back; }',
     '            try { await fetchViewpoints(); }', False),
    # ---- 第 36 轮 A-MAJOR-1/2/3/4 + MINOR-3：这四条判据自己也要有"能把它打红"的变异 ----
    # A-MAJOR-2：闸只认"结构上包了 try"，那把 catch 里的话换成「保存失败」它就放行 —— 而那正是本轮要消灭的假话。
    ('test_a_write_that_succeeded_is_never_reported_as_a_failure', 'catch_body_blames_the_write',
     VP, "                } catch (refreshError) { alert('观点已删除，只是列表没刷新出来 —— 刷新页面即可'); }",
     "                } catch (refreshError) { alert('删除失败：' + errorMessage(refreshError)); }", False),
    # A-MAJOR-3 + #53：`pollCleanupTask` 不在旧的刷新腿白名单里 ⇒ 旧闸对它瞎；现在它是条腿。
    ('test_a_write_that_succeeded_is_never_reported_as_a_failure', 'cleanup_leg_blames_the_write',
     HTML, _js('                            let task;',
               '                            try {',
               '                                task = await pollCleanupTask(taskId);',
               '                            } catch (pollError) {',
               '                                // 清理请求服务端已经接了：这里说"清理失败"老板就会再点一次 = 二次删除',
               "                                alert('清理任务已发起（任务号 ' + taskId + '），只是进度没取到 ——'",
               "                                      + ' 刷新页面就能看到结果，请不要重复点击');",
               '                                return;',
               '                            }'),
     '                            const task = await pollCleanupTask(taskId);', False),
    # A-MAJOR-1：把"没敢断定"退回光秃秃一句结论；以及"success 但没 data"又算取到了。
    ('test_the_summary_button_does_not_claim_there_is_nothing_to_summarize',
     'summary_stats_blames_nothing',
     VP, """                alert(summaryStatsLoaded.value ? '没有待汇总的观点'
                      : ('没敢断定"没有待汇总的观点"：' + (summaryStatsError.value || '汇总统计没取到')));""",
     "                alert('没有待汇总的观点');", False),
    ('test_the_summary_button_does_not_claim_there_is_nothing_to_summarize',
     'summary_stats_accepts_empty_data',
     VP, '                if (res.data.success && res.data.data) {',
     '                if (res.data.success) {', False),
    # A-MAJOR-4：模板那两条祖先链断言各打一处。
    ('test_the_execute_button_cannot_outlive_its_own_preview', 'execute_button_escapes_the_preview',
     HTML, '                        <div v-if="maintenancePreview" class="maintenance-result">',
     '                        <div v-if="showPredictionMaintenance" class="maintenance-result">', False),
    ('test_the_execute_button_cannot_outlive_its_own_preview', 'error_line_drops_the_way_out',
     HTML, ' —— 没有可信的预览，"确认执行"不会出现。<button class="action-btn small" @click="previewPredictionMaintenance(\'rollback\')">重新预览</button>',
     '。', False),
    # A-MINOR-3：`success` 字段缺失时不许武装红色按钮（判据是 `!== true`，退回 `=== false` 就该红）。
    ('test_a_failed_preview_leaves_nothing_to_confirm', 'preview_arms_on_missing_success',
     JS, '                if (!response || !response.data || response.data.success !== true) {',
     '                if (!response || !response.data || response.data.success === false) {', False),
    # 第 28 轮 B：TOP 弹窗那行说明（口径进正文、不许只活在 title）
    ('test_the_top_modal_says_who_is_excluded', 'top_modal_hides_its_caliber',
     HTML, '<div v-else class="empty-state">没有博主上榜：这个榜只收<strong>至少 5 条已验证结论</strong>的博主（少于 5 条的命中率没有参考意义）</div>',
     '<div v-else class="empty-state">暂无数据</div>', False),
    # 第 38 轮 A-MAJOR-1：帖子页 4 张迷你卡 / 预测页 8 个按钮括号数的 `—` 守卫与初值
    ('test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have',
     'post_mini_cards_lose_their_guard', HTML, "viewErrors.posts ? '—' : ", '', False),
    ('test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have',
     'prediction_facet_buttons_lose_their_guard', HTML, "viewErrors.predictions ? '—' : ", '', False),
    ('test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have',
     'prediction_total_init_back_to_zero', JS, 'total: null, has_more', 'total: 0, has_more', False),
    ('test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have',
     'prediction_facets_init_back_to_zero', JS, 'all: null,', 'all: 0,', False),
    ('test_the_two_list_pages_stop_reporting_zero_for_numbers_they_do_not_have',
     'post_total_init_back_to_zero', POST, 'total: null, skip', 'total: 0, skip', False),
    # 判据自己的正则也被钉：把 span 卡改回 div 形状不会被抓，但**去掉 `class="value"`** 会
    ('test_a_missing_number_is_not_rendered_as_zero',
     'post_mini_card_stops_being_a_value_card', HTML,
     '<span class="value">{{ viewErrors.posts', '<span class="metric">{{ viewErrors.posts', False),
    # 第 39 轮 A-MINOR-8：那条"口径进正文"的判据以前没配变异（commit message 里 6 处全指向别的）。
    # 把正文那行灰字退回 title-only，就是上一轮那句假话的原形状 ⇒ 必须红。
    ('test_the_three_prediction_queues_explain_themselves_without_hover',
     'queue_caliber_moves_back_into_title', HTML,
     '<div class="filter-caliber-note">口径：「待验证到期」= 已到目标日、仍在验证窗口内，今天点验证就是这一批；「未到期」= 还没到目标日；「待验证」= 未到期与观望之和，别把它当成"可以验了"。</div>',
     '<div class="filter-caliber-note" title="口径：「待验证到期」= 已到目标日"></div>', False),
    # 第 40 轮 A-M1：evidence 取失败必须把上一轮的区间放下（不清旧报告 = 那句红字永远渲染不出来）
    ('test_the_evidence_line_does_not_keep_yesterdays_report_on_a_failed_refresh',
     'evidence_keeps_stale_report', HTML,
     r"evidenceReport\.value = null;\s+evidenceError\.value = res\.data\.message",
     "                            evidenceError.value = res.data.message", True),
    ('test_the_evidence_line_does_not_keep_yesterdays_report_on_a_failed_refresh',
     'evidence_catch_keeps_stale_report', HTML,
     r"evidenceReport\.value = null;\s+evidenceError\.value = isServiceDown",
     "                        evidenceError.value = isServiceDown(e)", True),
    # 第 40 轮 A-m6：四个体检按钮的失败守卫（清掉守卫 = 继续摆上一轮的计数）
    ('test_the_audit_counters_hang_up_when_the_list_was_not_fetched',
     'audit_counters_drop_their_guard', HTML,
     '<button v-if="!viewErrors.mappings && identityStats.',
     '<button v-if="identityStats.', False),
    ('test_the_three_prediction_queues_explain_themselves_without_hover',
     'caliber_note_loses_its_style_class', HTML,
     'class="filter-caliber-note"', 'class="caliber-note"', False),
    ('test_an_error_notice_must_be_reachable_while_its_list_is_still_on_screen',
     'bloggers_notice_moves_back_into_the_empty_branch', HTML,
     r'<div v-if="bloggersError" class="notice-inline error"[^\n]*\n(?:[^\n]*\n){2}[^\n]*</div>\n',
     '', True),
    ('test_fetch_bloggers_counts_the_rows_it_leaves_on_screen',
     'stale_rows_never_counted', HTML,
     'bloggersStale.value = bloggers.value.length;', 'bloggersStale.value = 0;', False),
    ('test_delete_blogger_tells_four_different_endings_apart',
     'refresh_death_blamed_on_the_delete', HTML,
     r'// 博主已经删掉了，刷新失败不能说「删除失败」（addBlogger 第 34 轮就有这条腿）\n'
     r'(?:[^\n]*\n){6}[^\n]*\n',
     'await fetchBloggers(); await fetchStats();\n', True),
]


def _apply(pristine, path, finding, replacement, is_regex):
    """变异**永远从干净底本出发**：早先版本读磁盘上的当前内容，于是第 N 处变异是叠在
    第 N-1 处之上的 —— 报出来的"红"可能根本不是这一处造成的（同一把锚点还会第二次失配）。

    改写时**全部命中都改**（第 36 轮 A-MINOR-1）：旧写法 `count=1` 只改第一处，
    于是三条 `*_pager_ignores_soft_failure` 名字说"两条腿"、实际只打了 prev 那一腿，
    `empty_state_blames_the_database` 更是 7 处里改 1 处 —— 报"红"了，但红得说不清是哪一处。
    """
    text = pristine[path]
    if is_regex:
        new, n = re.subn(finding, replacement, text, flags=re.S)
    else:
        n = text.count(finding)
        new = text.replace(finding, replacement)
    return new, n


def main(list_only=False, only=None):
    todo = [m for m in MUTATIONS if not only or only in m[1]]
    if only and not todo:
        # 第 39 轮 A-MAJOR-2：`--only` 打错字时以前是"0 处变异、CONTROL 全绿、退码 0"
        # ＝**满分通过一次什么都没测的体检**。整套"文本判据必须配变异"的证据链悬在这个开关上。
        print('[abort] --only %r 一处变异都没匹配上 ⇒ 拒绝按"通过"收场' % only)
        return ['only-matched-nothing']
    if only and len(todo) < len(MUTATIONS):
        # 第 40 轮 A-m2：子串匹配还可能"只命中一小撮却像跑完了一套"（`--only _titl` 就中了 3/100）。
        # 不改判定，只把分母打出来 —— 按退码判断的人至少看得见"这次只覆盖 N/M"。
        print('[提示] --only %r 只匹配 %d/%d 处、判据 %d 条 ⇒ 这不是全套体检'
              % (only, len(todo), len(MUTATIONS), len({m[0] for m in todo})))
    # 名单自己算：docstring 里不抄文件名（第 36 轮 A-MINOR-2 就是抄漏了 viewpoint-manager.js）
    print('本批改写到的文件：%s' % '、'.join(sorted({m[2] for m in todo})))
    if list_only:
        for i, (test, name, path, *_rest) in enumerate(todo, 1):
            print('%2d. %-34s -> %s' % (i, name, test))
        print('共 %d 处变异，覆盖 %d 条判据' % (len(todo), len({m[0] for m in todo})))
        return []
    if not mutation_lock.harness_may_start(ROOT):
        print('另一个 pytest 会话正在跑（%s 被持有）—— 体检会就地改写 web/，'
              '两边并发时报出来的红绿都不作数。等它跑完再启动。'
              % mutation_lock.SESSION_NAME)
        return ['pytest-session-holds-the-lock']
    try:
        guard = mutation_lock.held_exclusively(ROOT)
        guard.__enter__()
    except RuntimeError as exc:
        print(str(exc))
        return ['another-harness-holds-the-lock']
    env = dict(os.environ)
    env[mutation_lock.ENV_PID] = str(os.getpid())
    env['PYTHONIOENCODING'] = 'utf-8'   # 子进程要按 UTF-8 出，否则中文机器上解码成替换字符
    failures = []
    # 对照组：干净代码上这一整份判据必须**全绿**。没有这一步，"每条变异都红了"可能是假的 ——
    # 子 pytest 只要起手就失败（conftest 报错、锁把子会话拦死、解释器不对），
    # 每一处都会报 RED，体检反而满分通过。
    ctrl = subprocess.run([sys.executable, '-m', 'pytest', T, WIRING, '-q', '--no-header',
                           '-p', 'no:cacheprovider'],
                          cwd=str(ROOT), capture_output=True, env=env,
                          text=True, encoding='utf-8', errors='replace')
    if ctrl.returncode != 0:
        print('CONTROL-RED：干净代码上跑判据本身就失败，本轮体检结论一律不作数：\n%s'
              % (ctrl.stdout + ctrl.stderr)[-1200:])
        guard.__exit__(None, None, None)      # 别让早退把锁留到进程退出才放（第 33 轮 A-MINOR-10）
        return ['control-run']
    print('CONTROL-GREEN（干净代码上判据通过，下面的红才有意义）')
    pristine = {p: (ROOT / p).read_text(encoding='utf-8')
                for p in {m[2] for m in todo}}
    try:
        for test, name, path, finding, repl, is_regex in todo:
            mutated, n = _apply(pristine, path, finding, repl, is_regex)
            if n < 1:
                print('%-36s ANCHOR-MISS（变异锚点没命中，判据本身可疑）' % name)
                failures.append(name)
                continue
            (ROOT / path).write_text(mutated, encoding='utf-8')
            # 落盘核对：第 30 轮两份复评都指出"写了不等于改到了"——编辑器/进程可能把文件
            # 盖回去，那时 pytest 跑的是干净代码，报出来的"绿"是假的。
            on_disk = (ROOT / path).read_text(encoding='utf-8')
            if on_disk != mutated:
                print('%-36s NOT-LANDED（写盘后被别的进程改回去了，这一处的结论不作数）' % name)
                failures.append(name + ':not-landed')
                continue
            if on_disk == pristine[path]:
                print('%-36s NO-OP（替换后与底本相同 = 变异没生效）' % name)
                failures.append(name + ':no-op')
                continue
            tfile = WIRING if test.startswith(WIRING_TESTS) else T
            r = subprocess.run([sys.executable, '-m', 'pytest', '%s::%s' % (tfile, test),
                                '-q', '--no-header', '-p', 'no:cacheprovider'],
                               cwd=str(ROOT), capture_output=True, env=env,
                               text=True, encoding='utf-8', errors='replace')
            out = (r.stdout or '') + (r.stderr or '')
            # "红"必须是**跑完并且断言失败**的红。pytest 的退码有讲究：
            # 0 全过 / 1 有失败 / 2 被中断 / 3 内部错 / 4 用法错（参数或 test id 写错）/ 5 没收集到用例。
            # 上一版只 grep 字符串，`--bogus-flag` 那种用法错的输出里也带 "error:" ⇒ 记成"判据有效"
            # （第 33 轮 A-MAJOR-4 / 第 34 轮 A-MAJOR-5：分类器本身零覆盖）。
            if r.returncode == 1 and ' failed' in out:
                verdict, bad = 'RED（判据有效）', False
            elif r.returncode == 0:
                verdict, bad = 'GREEN（判据无效！）', True
            else:
                verdict, bad = 'HARNESS-FAIL（子进程没跑到断言，退码 %s）' % r.returncode, True
            print('%-36s %-8s %s' % (name, path.split('/')[-1], verdict))
            if bad:
                failures.append(name)
    finally:
        for p, text in pristine.items():
            (ROOT / p).write_text(text, encoding='utf-8')
            if (ROOT / p).read_text(encoding='utf-8') != text:
                print('!! 还原后字节不一致：%s —— 手工检查' % p)
                failures.append('restore:%s' % p)
        print('已还原 %s（逐文件回读比对一致）' % '、'.join(sorted(pristine)))
        guard.__exit__(None, None, None)
    return failures


if __name__ == '__main__':
    # 真用 argparse（第 39 轮 A：文件顶部 import 了它却从没调用 ⇒ 一个"我有 CLI 校验"的假信号，
    # 结果 `--help` 被当普通参数、整套体检就地开跑改写 web/；未知参数也一律静默接受）。
    parser = argparse.ArgumentParser(description='前端判据变异体检（会就地改写 web/，跑完还原）')
    parser.add_argument('--list', action='store_true', help='只列变异清单与覆盖的判据数，不动文件')
    parser.add_argument('--only', metavar='子串', help='只跑名字里含该子串的变异；匹配不到即失败')
    args = parser.parse_args()
    bad = main(args.list, args.only)
    raise SystemExit(1 if bad else 0)
