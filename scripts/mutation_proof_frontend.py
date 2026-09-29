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
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

# 本机控制台默认 cp936，而这份工具的每一行回执都带 `⇒`：输出重定向到文件时 python 仍按 locale
# 编码 ⇒ 会 `UnicodeEncodeError` 崩在体检锁里面（第 57 轮在隔壁那支上实测过同一签名）。
# ⚠ 这里原来是 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, …)` —— 两支工具对同一件事
# 两把尺子，而那一句**在导入时就把 stdout 换掉了**：任何用例想 import 这份工具问它自己的问题
# （第 64 轮：注册表里的判据名指向哪儿）都会顺手动掉整个 pytest 会话的输出。
# 与 `mutation_proof_lifecycle.py` 并成同一条：能 reconfigure 就 reconfigure，已被换成别的
# 对象（捕获、StringIO）就跳过；`line_buffering` 保留（被强杀时前面那几行才落得出去）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except (AttributeError, ValueError):     # 已被换成别的对象（pytest 捕获、io.StringIO）
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 按**文件路径**加载这把锁，而不是 `from src.utils import mutation_lock`。
# 后者会执行 `src/__init__.py`（`from src.fund import fund_api, fund_data_manager`），
# 一路拉起 `src.models.database` ⇒ 在这个"只改写 web/ 文件"的进程里，按 `.env`
# 建出一个绑生产 Supabase 的全局 engine（第 45 轮 A-M5 把这条链照出来：本仓所有 src 包
# 的 `__init__.py` 都用相对 import，旧的 import 图看不见）。这个锁是纯标准库，够用。
_lock_spec = importlib.util.spec_from_file_location(
    'mutation_lock', ROOT / 'src' / 'utils' / 'mutation_lock.py')
mutation_lock = importlib.util.module_from_spec(_lock_spec)
_lock_spec.loader.exec_module(mutation_lock)


def _refuse_if_the_orm_is_already_built():
    """这个进程不该按 `.env` 建出库连接 ⇒ 起体检前先问一句 ORM 有没有被拉起来。

    ⚠ 这一问**原来写在模块顶层**（导入即抛），于是这份工具在自己那个仓里无法被任何用例
    `import` 起来问它自己的问题（第 64 轮：注册表里的判据名指向哪个文件，只有它自己答得出）。
    挪到 `main()` 起手：仍然排在第一个字节被改写之前，作用一字不减。
    """
    assert 'src.models.database' not in sys.modules, \
        '这个进程已经导入了 ORM ⇒ 会按 .env 建 engine，正是要避免的那种事'

HTML = 'web/index.html'
JS = 'web/prediction-manager.js'
POST = 'web/post-manager.js'
VP = 'web/viewpoint-manager.js'
T = 'tests/unit/test_frontend_cold_start.py'
WIRING = 'tests/unit/test_frontend_wiring.py'
FUND = 'tests/unit/test_frontend_fund_update.py'
# 判据不止一个文件：接线闸（`test_frontend_wiring.py`）与「更新所有基金」那条行为判据
# （`test_frontend_fund_update.py`，第 64 轮 M-2 从文本断言改成跑真实调用链）也得能被自己的变异打红。
WIRING_TESTS = ('test_every_option_the_manager_reads_is_actually_injected',
                'test_everything_the_page_destructures_is_actually_exported')
FUND_TESTS = ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',)
JUDGE_FILES = {T: (), WIRING: WIRING_TESTS, FUND: FUND_TESTS}


def _judge_file(test):
    """判据住在哪个文件就跑哪个文件。

    以前这里是"不是接线闸就送去 cold_start"那一个三元表达式 —— 加第三个判据文件时它会
    安静地把变异送到错的文件（退码 4 ⇒ HARNESS-FAIL，不是绿，但至少不会被当成"判据有效"）。
    现在按名单查，重名直接拒绝。
    """
    hits = [f for f, names in JUDGE_FILES.items() if names and test.startswith(names)]
    assert len(hits) <= 1, '判据名单重叠：%s 同时属于 %s ⇒ 变异会被送到错的文件' % (test, hits)
    return hits[0] if hits else T


def _judge_defs():
    """每个判据文件里真有哪些 `def test_` —— 由文件自己回答，不抄名单。

    为什么要有这一问：注册表里写一个**不存在**的判据名时，旧写法照样把它送去某个文件跑，
    子 pytest 报"没匹配到用例"（退码 4）⇒ HARNESS-FAIL，看起来像"工具坏了"，
    而不是"这条变异指向错了"。现在先问一句，答不出就点名为 JUDGE-MISS。
    """
    return {f: set(re.findall(r'^def (test_\w+)', (ROOT / f).read_text(encoding='utf-8'), re.M))
            for f in JUDGE_FILES}


def _judge_lookup(test, judges):
    """注册表里这条判据 id 落在哪个文件、那个文件里**真有没有**这条 `def test_`。

    为什么单独成一条函数（第 64 轮）：这一问原来是 `main()` 循环里的一行，没有任何用例能
    走到它 ⇒ 我把"比整串"写错成 JUDGE-MISS 误伤两条**真在跑**的接线闸时，全套件一声不响。
    参数化后缀（`test_xxx[createViewpointManager]`）在注册表里有、文件里的 `def` 没有，
    所以比的是中括号**前面**那一段；文件仍由 `_judge_file` 决定 —— 名字在别的文件里有，
    这一处也不算接上（按名字跨文件放行＝把变异送到错的判据上）。
    """
    tfile = _judge_file(test)
    return tfile, re.sub(r'\[.*$', '', test) in judges.get(tfile, ())

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
     # 锚点是**当前形状**（第 42 轮体检两条 ANCHOR-MISS 照出来的）：翻页条后来从
     # `postMeta.total || 0` 换成了 `numOrDash(...)`，锚点不跟着换就等于什么都没打进去。
     HTML, '<template v-if="viewErrors.posts">条数没取到，表里是上一次取到的</template>'
           "<template v-else>共 {{ viewErrors.posts ? '—' : numOrDash(postMeta.total) }} 条</template>",
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
     # 锚点跟着 `setup()` 的导出名单走：第 42 轮加了 `bloggersStale`，锚点不换就永远 ANCHOR-MISS
     HTML, 'bloggersError, bloggersStale, statsError, statVal, numOrDash, viewErrors, emptyText,',
     'bloggersError, bloggersStale, statsError, statVal, viewErrors, emptyText,', False),
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
           '<template v-else>共 {{ numOrDash(predictionMeta.total) }} 条</template>',
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
     '<div class="filter-caliber-note">口径：「待验证到期」= 已到目标日、仍在验证窗口内，今天点验证就是这一批；「结构性不可验」= 验证器已按区间问过、这一轮判不出结论（净值补不上来，或目标日那天没有独立净值行），今天点验证也问不出来，到重问日自动回到「待验证到期」；「未到期」= 还没到目标日；「待验证」= 还没有结论的全部（含「待验证到期」「结构性不可验」「未到期」这三档，另加不参与验证的观望行与没填目标日的行），别把它当成"今天能验了"。</div>',
     '<div class="filter-caliber-note" title="口径：「待验证到期」= 已到目标日"></div>', False),
    # 第 54 轮 A-9 / B-7：把"含"写回**等式**必须红 —— 三个子桶都排除观望行与缺目标日的行，
    # 而 pending 不排除，所以那个加法式子只在两库今天恰好是 0 时才平。
    ('test_the_three_prediction_queues_explain_themselves_without_hover',
     'queue_caliber_back_to_an_equation_it_cannot_keep', HTML,
     '还没有结论的全部（含「待验证到期」「结构性不可验」「未到期」这三档，另加不参与验证的观望行与没填目标日的行）',
     '还没有结论的全部（＝待验证到期＋结构性不可验＋未到期）', False),
    # 任务 #8：「结构性不可验」这一档必须**有数、有行内说明**。
    # 摘掉按钮里的数 ⇒ 老板看到"到期 0 条"会以为全都验完了（那 15 条只是不再白跑）；
    # 摘掉行内标签 ⇒ 被压住的预测看起来和普通待验证一模一样，没人知道它在等哪天。
    ('test_the_structurally_unverifiable_queue_is_counted_and_explained',
     'unverifiable_button_counts_a_zero_it_did_not_read', HTML,
     "结构性不可验 ({{ viewErrors.predictions ? '—' : numOrDash(predictionMeta.facets.unverifiable) }})",
     "结构性不可验 ({{ viewErrors.predictions ? '—' : numOrDash(0) }})", False),
    ('test_the_structurally_unverifiable_queue_is_counted_and_explained',
     'unverifiable_row_stops_saying_when_it_asks_again', HTML,
     """<span v-if="p.lifecycle === 'unverifiable'" class="text-xs text-tertiary">· 问过仍判不出，{{ p.next_verify_date }} 自动重问</span>""",
     """<span v-if="p.lifecycle === '__never_matches__'" class="text-xs text-tertiary">· 问过仍判不出，{{ p.next_verify_date }} 自动重问</span>""", False),
    # 回收站那条原因同样要有变异：把它换成一个永不成立的条件，行就只剩"已归档"三个字
    ('test_the_recycle_bin_says_why_each_row_was_archived',
     'archive_reason_stops_being_read', HTML,
     'v-if="p.is_deleted && p.delete_reason"',
     'v-if="p.is_deleted && p.reason_never_set"', False),
    # 基金页那一行的"源端停更"：把整句拿掉，接口还在给这句话、屏幕上却没有读者
    ('test_a_stopped_fund_explains_itself_on_its_own_row',
     'stopped_fund_note_never_reaches_the_screen', HTML,
     '<div v-if="f.nav_stop_note" class="text-xs text-tertiary">{{ f.nav_stop_note }}</div>',
     '', False),
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
     r'<div v-if="bloggersError" class="text-xs"[^\n]*\n(?:[^\n]*\n){3}[^\n]*</div>\n',
     '', True),
    ('test_fetch_bloggers_counts_the_rows_it_leaves_on_screen',
     'stale_rows_never_counted', HTML,
     'bloggersStale.value = bloggers.value.length;', 'bloggersStale.value = 0;', False),
    ('test_delete_blogger_endings_are_driven_by_state_that_is_reachable',
     'refresh_death_blamed_on_the_delete', HTML,
     r"const notRefreshed = \[bloggersError\.value, statsError\.value,\n"
     r"[^\n]*\n[^\n]*\.filter\(Boolean\)\.join\('；'\);\n",
     "const notRefreshed = '';\n", True),
    ('test_no_new_class_name_is_used_without_being_defined',
     'bloggers_notice_class_stops_being_defined', HTML,
     '<div v-if="bloggersError" class="text-xs"',
     '<div v-if="bloggersError" class="notice-inline error"', False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'nav_staleness_never_warns', HTML,
     'if (r.nav_lag_stale) text +=', 'if (false) text +=', False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'nav_staleness_warns_for_the_wrong_reason', HTML,
     'if (r.nav_lag_stale) text +=', 'if (r.nav_lag_days > 0) text +=', False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'nav_future_rows_go_silent', HTML,
     'if (r.nav_future_rows > 0)', 'if (false)', False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'missing_nav_reported_as_a_date_anyway', HTML,
     "if (!r.nav_as_of) return '库里没有任何不晚于今天的净值行 ⇒ 准确率没有输入，先跑基金更新';",
     "if (!r.nav_as_of) return '净值截至（未记录）';", False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'old_backend_blamed_on_an_empty_database', HTML,
     "if (!('nav_as_of' in r)) return '这个版本的接口没有净值新鲜度 ⇒ 线上可能是旧构建';",
     "if (!('nav_as_of' in r)) return '库里没有任何不晚于今天的净值行';", False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'one_fresh_fund_speaks_for_the_book', HTML,
     "else if (r.nav_used_stale_funds > 0) text += '（全表最晚的一行是新的，别被它骗）'",
     "else if (false) text += '（全表最晚的一行是新的，别被它骗）'", False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'stale_coverage_count_goes_silent', HTML,
     "if (r.nav_used_stale_funds > 0) {",
     "if (false) {", False),
    # 第 50 轮 A-MINOR-1 新接的两根线也要有会红的判据：中位日期与"过没过半"那一档。
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'median_date_buys_itself_the_wrong_ruler', HTML,
     "'，引用面中位停在 ' + (r.nav_used_as_of || '未记录');",
     "'，引用面中位停在 ' + r.nav_used_stale_before;", False),
    ('test_the_nav_freshness_note_says_what_the_data_is_not_the_date_we_ran',
     'majority_staleness_never_escalates', HTML,
     "r.nav_used_stale_majority ? ' ⇒ 过半标的停更",
     "false ? ' ⇒ 过半标的停更", False),
    # 第 63 轮 #132「打开网站就补」的三把判据各配一处变异（判据没配变异的一律不算数）。
    ('test_opening_the_page_decides_the_catch_up_from_the_numbers_it_has',
     'catch_up_treats_a_missing_number_as_all_clear', HTML,
     "if (navLagStale === true) return",
     "if (navLagStale !== false) return", False),
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'a_failed_catch_up_keeps_the_day_locked', HTML,
     "let funds = null, verify = null, stepError = '', unfinished = '';",
     "localStorage.setItem(CATCH_UP_KEY, day);\n"
     "                    let funds = null, verify = null, stepError = '', unfinished = '';", False),
    ('test_the_catch_up_only_borrows_the_two_endpoints_that_already_exist',
     'the_first_screen_never_asks', HTML,
     r'\n[ \t]*void maybeCatchUpOnOpen\(\);',
     "", True),
    ('test_first_load_fetches_all_go_through_the_wake_retry',
     'unwrap_catch_up_predictions_fetch', HTML,
     "withWakeRetry(() => axios.get('/api/predictions', { params: { page: 1, page_size: 1 } }))",
     "axios.get('/api/predictions', { params: { page: 1, page_size: 1 } })", False),
    ('test_the_catch_up_only_borrows_the_two_endpoints_that_already_exist',
     'the_page_starts_comparing_lag_days_itself', HTML,
     "stale = ev.data && ev.data.data ? ev.data.data.nav_lag_stale : null;",
     "stale = ev.data && ev.data.data ? (ev.data.data.nav_lag_days >= 3) : null;", False),
    # 第 63 轮真浏览器量到的那一格：`update-all` 是后台任务，POST 立刻回"已启动"。
    # 不等它跑完就发验证 ⇒ 那批到期预测还是拿旧净值判的，白跑一轮（判据看的是调用流水）。
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_catch_up_verifies_without_waiting_for_the_nav', HTML,
     # ⚠ 第 66 返修：旧锚点 `if (fin.done) {` 随那份独立实现一起没了 ⇒ 这一处报的是
     # ANCHOR-MISS（"这条判据有没有效"当场没答上来），改成问共用那把尺子的调用点：
     # "没等到结果"也照样发验证，正是这一条要拦的形状。
     "if (v.ok) {",
     "if (v.ok || v.kind === 'unfinished') {", False),
    # 第 64 轮 M-1 的第一半：不看 POST 自己答了什么 ⇒ "正在进行中"也被当成"本轮发起了"
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_catch_up_proves_it_started_the_run_too', HTML,
     "if (funds.data && funds.data.success === true) {",
     "if (true) {", False),
    # 第 64 轮 M-1 的第二半：状态里换成别的那一轮之后，那份回执不是我们的
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_catch_up_adopts_another_runs_receipt', HTML,
     "if (s.started_at !== since) {",
     "if (false) {", False),
    # 第 64 轮 M-3：并发门。设在第一次 await 之前才管得住两个几乎同时打开的页签
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_concurrency_gate_never_closes', HTML,
     "if (catchUp.running) return;",
     "if (false) return;", False),
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_concurrency_gate_is_set_too_late', HTML,
     "catchUp.running = true;\n                    try {",
     "try {", False),
    # 问进度那一笔也得过唤醒门：Render 在补跑中途睡着时不该把整轮判成"中断"（第 64 轮风险 1）
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_progress_poll_skips_the_wake_gate', HTML,
     "const res = await withWakeRetry(() => axios.get('/api/funds/update-status'));",
     "const res = await axios.get('/api/funds/update-status');", False),
    # 第 64 轮 m-5：重新打开时那句"今天补过了"要看得见，不许静默
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'a_silent_second_open_same_day', HTML,
     "if (!catchUp.note && !catchUp.error) {",
     "if (false) {", False),
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'a_half_done_catch_up_is_reported_as_a_whole_one', HTML,
     "+ '；这次没有发起验证，下一次打开会自动接着补。';",
     "+ '；验证也跟着做完了。';", False),
    # `update-all` 只说"任务已启动"，真回执（更新了几只、失败几只）在 `update-status.last_result`
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_finished_nav_receipt_is_thrown_away', HTML,
     "funds = { data: fin.result };",
     "", False),
    # 接口那句自带换行，而这一格是普通 span ⇒ 不并句就会在屏幕上糊成一片
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_receipt_keeps_its_line_breaks', HTML,
     "String(d.message || '回执没给数').split('\\n')",
     "String(d.message || '回执没给数')", False),
    # 第 65 轮 MAJOR-1 + MAJOR-2：净值那一轮"跑完了但自己报失败"这一格，补跑当时既没判也不红
    # ⚠ 第 66 返修：两条腿同判之后这一支的锚点换成了共用那把尺子的调用点（旧锚点
    # `if (fin.result && fin.result.success === false) {` 随那份独立实现一起没了 ⇒ 会 ANCHOR-MISS）
    ('test_a_catch_up_runs_once_per_beijing_day_and_a_failure_frees_it_again',
     'the_failed_nav_round_still_verifies', HTML,
     "if (v.ok) {",
     "if (v.ok || v.kind === 'failed') {", False),
    # 任务 #154：那句"最多等 N 分钟"是从两个常数算出来的，抄回字面量必须红
    ('test_the_catch_up_only_borrows_the_two_endpoints_that_already_exist',
     'the_in_progress_note_hard_codes_the_wait', HTML,
     "+ fundPollMinutes() + ' 分钟，服务在唤醒时要更久），等它跑完再发起验证…';",
     "+ '约几分钟…';", False),
    # ── 第 64 轮 M-2：两条路共用一份轮询，这个按钮自己那条腿也得有变异盯着 ──
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'the_button_reports_the_process_not_the_result', HTML,
     "alert(v.message || '更新完成');",
     "alert('任务已启动，正在后台更新…');", False),
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'the_button_keeps_the_global_lock_when_the_poll_ends', HTML,
     "analyzing.value = false;\n                    const v = navRoundVerdict(fin);",
     "const v = navRoundVerdict(fin);", False),
    # 第 66 返修 MAJOR-1 的第二半：按钮这一腿以前对"跑完了但它自己说失败"零判据
    # （把下面那道门删掉，第 65 轮那 54 条一声不响）。现在同判由两条腿各一处变异钉住。
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'the_button_throws_away_the_shared_verdict', HTML,
     "if (!v.ok) {",
     "if (false && !v.ok) {", False),
    # 第 66 轮复评 MI-5：`success` 这个键在、值是 `null` ⇒ 只判 `undefined` 会把它读成成功。
    # 这一处变异必须被 `nullish` 那一格点红（上一版那格样品不存在，所以它谁也拦不住）。
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'a_null_success_is_read_as_all_clear', HTML,
     "if (!r || typeof r.success !== 'boolean') {",
     "if (!r || typeof r.success === 'undefined') {", False),
    # 更新已经起来了 ⇒ 问不到进度不是"更新失败"（第 36 轮 #53 那一族换了位置复发）
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'the_progress_leg_blames_the_update', HTML,
     "alert('更新已经在后台跑着，只是进度没问到（'",
     "alert('更新失败（'", False),
    ('test_the_fund_button_waits_for_the_result_and_shares_the_one_poll',
     'the_refresh_leg_blames_the_update', HTML,
     "alert('更新完成了，但基金列表没刷出来 ⇒ 手动刷新一次就能看到');",
     "alert('更新失败: 列表没取到');", False),
    # 任务 #158 §A/§B/§F + 第 72 轮 M-1/M-2：清理页「空间回收」那一步的几种结局，
    # 每一种配一处能把自己打红的变异。判据跑的是页面里 reclaimSpace / reclaimResult /
    # cleanupData 的真实函数体（node 执行），不是 grep 文本 ⇒ 下面每一处载荷只翻一个字面
    # 判法，不引入新形状。
    ('test_the_reclaim_button_reads_its_own_receipt_and_shows_which_ending_happened',
     'the_reclaim_failure_is_reported_as_done', HTML,
     "const ok = res.data?.success === true;",
     "const ok = true;", False),
    ('test_the_delete_receipt_says_what_happened_to_the_disk_space',
     'the_reclaim_failed_ending_is_folded_into_the_finished_one', HTML,
     "if (reclaim.success === false) return { ok: false, text: '空间回收：没跑成 —— ' + (reclaim.error || '数据库没给出原因') };",
     "if (reclaim.success === false) return { ok: true, text: '空间回收：跑完了，本次没测得可释放的空间' };", False),
    ('test_the_delete_confirmation_promises_only_what_the_backend_will_try',
     'the_confirm_dialog_blames_disk_on_delete', HTML,
     "这次会顺带尝试回收磁盘空间：一条都没删掉时不回收，回收没跑成也不影响已经删掉的数据（结果会在下方「空间回收结果」里说明）。确定执行吗？",
     "删除后会自动回收磁盘空间。确定执行吗？", False),
    ('test_the_delete_receipt_says_what_happened_to_the_disk_space',
     'the_page_shows_the_raw_skip_key', HTML,
     "if (reclaim.skipped) return { ok: false, text: '空间回收：没跑 —— ' + (reclaim.reason_text || '数据库没给出原因') };",
     "if (reclaim.skipped) return { ok: false, text: '空间回收：没跑 —— ' + (reclaim.reason || '数据库没给出原因') };", False),
    ('test_the_delete_receipt_says_what_happened_to_the_disk_space',
     'the_unflagged_reclaim_receipt_is_forgiven', HTML,
     "if (!reclaim.success) return { ok: false, text: '空间回收：回执里没写成没成，不敢算已完成' };",
     "if (!reclaim.success) return { ok: true, text: '空间回收：跑完了，本次没测得可释放的空间' };", False),
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


def _run_stamp():
    """北京时刻（日志署名，不参与任何判定）。

    第 59 轮 MINOR-4 给隔壁 `mutation_proof_lifecycle.py` 补过同一件事，理由在这儿一样成立：
    没有运行头，"这一份日志是我这次真跑的"就只剩 md5 一句话。⚠ **不 import `src.*`** ——
    这个进程会就地改写 `web/`，按 `.env` 建起全局 engine 正是本仓反复拦的那件事。
    与隔壁那份**各留一份实现**（都是六行格式化，不是判据；两支体检工具互不 import 是既有边界，
    把 `mutation_proof_lifecycle` 拉进来只为一个时间戳，等于让它那份 30 处变异载荷进这个进程）。
    """
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _git_head():
    """当时的 commit（短 sha）；拿不到就回 `unknown`，不许拿静态串冒充。"""
    try:
        out = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short=12', 'HEAD'],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or '').strip() or 'unknown'
    except Exception:
        return 'unknown'


def _worktree_state():
    """跑当时的工作树状态（`clean` / `dirty(N)` / `unknown`）—— 与逻辑侧那支**各留一份**，
    互不 import 是既有边界（第 66 轮 ⑥(3) 那条注释），不是判据。

    为什么两支都要（第 67 轮复评 MINOR-11）：`git=` 只说"最近一次提交是哪个"，
    而体检读的是磁盘上的文件 —— 只报 `git=` 会让人以为这份日志是那一次提交的凭据，
    其实跑的是从没存在过的中间态。
    """
    try:
        out = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--',
                              'src', 'web', 'tests'],
                             capture_output=True, text=True, timeout=30)
        n = len([line for line in (out.stdout or '').splitlines() if line.strip()])
        return 'clean' if out.returncode == 0 and n == 0 else (
            'dirty(%d)' % n if out.returncode == 0 else 'unknown')
    except Exception:
        return 'unknown'


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
    # 运行头排在它前面（第 59 轮 MINOR-4 给隔壁那支补过，本批对齐）：归档的日志要能自证是哪一次、
    # 哪一版跑的 ⇒ 拿上一批那份冒充这一批，时刻与 git 都对不上。
    print('# run @ %s  git=%s  worktree=%s  python=%s  共 %d 处变异 / %d 个判据文件'
          % (_run_stamp(), _git_head(), _worktree_state(), sys.version.split()[0],
             len(todo), len({_judge_file(m[0]) for m in todo})))
    print('本批改写到的文件：%s' % '、'.join(sorted({m[2] for m in todo})))
    if list_only:
        for i, (test, name, path, *_rest) in enumerate(todo, 1):
            print('%2d. %-34s -> %s' % (i, name, test))
        print('共 %d 处变异，覆盖 %d 条判据' % (len(todo), len({m[0] for m in todo})))
        return []
    # 这一问排在**抢锁与改写第一个字节之前**（原来它在模块顶层 ⇒ 谁 import 这份工具谁炸）。
    _refuse_if_the_orm_is_already_built()
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
    ctrl = subprocess.run([sys.executable, '-m', 'pytest', T, WIRING, FUND, '-q', '--no-header',
                           '-p', 'no:cacheprovider'],
                          cwd=str(ROOT), capture_output=True, env=env,
                          text=True, encoding='utf-8', errors='replace')
    if ctrl.returncode != 0:
        print('CONTROL-RED：干净代码上跑判据本身就失败，本轮体检结论一律不作数：\n%s'
              % (ctrl.stdout + ctrl.stderr)[-1200:])
        guard.__exit__(None, None, None)      # 别让早退把锁留到进程退出才放（第 33 轮 A-MINOR-10）
        return ['control-run']
    print('CONTROL-GREEN（干净代码上判据通过，下面的红才有意义）')
    # 每个判据文件里真有哪些 `def test_`，由文件自己回答（不抄名单）。为什么要这一问、
    # 为什么比的是中括号**前面**那一段 —— 都写在 `_judge_defs` / `_judge_lookup` 里，
    # 那两条现在有 `tests/unit/test_mutation_lock.py` 的用例钉着（第 64 轮：这一问原来是
    # 循环里的一行，我把它写成"比整串"，误伤两条真在跑的接线闸而全套件一声不响）。
    judges = _judge_defs()
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
            tfile, known = _judge_lookup(test, judges)
            if not known:
                print('%-36s JUDGE-MISS（判据 %s 不在 %s 里，这一处不作数）' % (name, test, tfile))
                failures.append(name + ':judge-missing')
                continue
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
