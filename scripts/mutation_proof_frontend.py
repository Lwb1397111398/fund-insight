# -*- coding: utf-8 -*-
"""把 `tests/unit/test_frontend_cold_start.py` 的每条判据各自退回"修复前的形状"，证明它会红。

为什么要有这个文件（第 29 轮两份复评共同点破）：那批判据是扫 HTML 文本的，
其中两条被证明**结构上不可能响**（一条比对字面 `「」`，而渲染出的空格子来自 `{{ }}` 插值；
一条用 `<th[^>]*>` 把属性吃掉，于是 `not any('title=' in ...)` 永远为真）。
文本判据只有配上"能把它打红的变异"才算数，所以变异不能只在我脑子里跑一遍就丢掉。

用法：
    python scripts/mutation_proof_frontend.py            # 全跑，任何一条绿就退码 1
    python scripts/mutation_proof_frontend.py --list     # 只看清单

安全：只改 `web/index.html` 与 `web/prediction-manager.js` 两个文件，跑完无条件按字节还原
（含异常路径）；不碰数据库、不联网。
"""
import argparse
import io
import re
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

ROOT = Path(__file__).resolve().parents[1]
HTML = 'web/index.html'
JS = 'web/prediction-manager.js'
T = 'tests/unit/test_frontend_cold_start.py'

# (判据函数, 变异名, 文件, 找, 换成, 是否正则)
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
     HTML, r"const statVal = \(v\) => statsError\.value \? '—' : \(v \|\| 0\);",
     "const statVal = (v) => (v || 0);", True),
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
]


def _apply(pristine, path, finding, replacement, is_regex):
    """变异**永远从干净底本出发**：早先版本读磁盘上的当前内容，于是第 N 处变异是叠在
    第 N-1 处之上的 —— 报出来的"红"可能根本不是这一处造成的（同一把锚点还会第二次失配）。"""
    text = pristine[path]
    if is_regex:
        new, n = re.subn(finding, replacement, text, count=1, flags=re.S)
    else:
        n = text.count(finding)
        new = text.replace(finding, replacement, 1)
    return new, n


def main(list_only=False):
    if list_only:
        for i, (test, name, path, *_rest) in enumerate(MUTATIONS, 1):
            print('%2d. %-34s -> %s' % (i, name, test))
        print('共 %d 处变异，覆盖 %d 条判据' % (len(MUTATIONS), len({m[0] for m in MUTATIONS})))
        return []
    pristine = {p: (ROOT / p).read_text(encoding='utf-8')
                for p in {m[2] for m in MUTATIONS}}
    failures = []
    try:
        for test, name, path, finding, repl, is_regex in MUTATIONS:
            mutated, n = _apply(pristine, path, finding, repl, is_regex)
            if n < 1:
                print('%-36s ANCHOR-MISS（变异锚点没命中，判据本身可疑）' % name)
                failures.append(name)
                continue
            (ROOT / path).write_text(mutated, encoding='utf-8')
            r = subprocess.run([sys.executable, '-m', 'pytest', '%s::%s' % (T, test),
                                '-q', '--no-header', '-p', 'no:cacheprovider'],
                               cwd=str(ROOT), capture_output=True,
                               text=True, encoding='utf-8', errors='replace')
            red = r.returncode != 0
            print('%-36s %-8s %s' % (name, path.split('/')[-1],
                                      'RED（判据有效）' if red else 'GREEN（判据无效！）'))
            if not red:
                failures.append(name)
    finally:
        for p, text in pristine.items():
            (ROOT / p).write_text(text, encoding='utf-8')
            if (ROOT / p).read_text(encoding='utf-8') != text:
                print('!! 还原后字节不一致：%s —— 手工检查' % p)
                failures.append('restore:%s' % p)
        print('已还原 %s（逐文件回读比对一致）' % '、'.join(sorted(pristine)))
    return failures


if __name__ == '__main__':
    bad = main('--list' in sys.argv)
    sys.exit(1 if bad else 0)
