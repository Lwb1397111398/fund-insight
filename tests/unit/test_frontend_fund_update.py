"""「更新所有基金」那个按钮等的轮询，与自动补跑用的是**同一把**（第 64 轮 M-2）。

这条文件以前有四条**文本**断言，钉的是基金页自己那份 `setInterval` 轮询：`pollFundUpdateStatus`
存在、认 `last_result === null`、上限 30 分钟、按 `finished_at` 去重弹窗。自动补跑（任务 #132）
当时另写了一份 5 秒 × 150 的循环 ⇒ **同一个问题两把尺子**，而新的那份根本不认"回执丢了"，
上限也因此有了第二个出处。第 64 轮把两条路并成一份 `waitFundUpdateToFinish`：
"跑完要说结果、回执丢了要明说、换成了别的那一轮不许认领、数到上限要放开 `analyzing`"
这四件事从此一次跑真调用链、两条腿一起验。文本断言只留一条还有意义的（不许退回长同步请求）。
"""
from pathlib import Path

import pytest

from tests.unit.test_frontend_cold_start import (
    NODE, _const, _decl, _html, _run_chain_js)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_fund_update_uses_status_polling_instead_of_long_request_timeout():
    """基金全量更新走"起后台任务 + 问进度"，不许退回一个长时间挂着的同步请求。"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "/api/funds/update-status" in content
    assert "pollFundUpdateStatus" in content
    assert "timeout: 300000" not in content


@pytest.mark.skipif(not NODE, reason='本机没有 node')
def test_the_fund_button_waits_for_the_result_and_shares_the_one_poll():
    """按钮这一腿的六种结局 + "轮询只许有一份"（跑的是页面里那份真实调用链）。"""
    html = _html()
    max_tries = int(_const(html, 'FUND_POLL_MAX_TRIES').split('=')[1].strip().rstrip(';'))
    prelude = '\n'.join([
        "const alerts = [];",
        "const alert = (m) => alerts.push(String(m));",
        "const ref = (v) => ({ value: v });",
        "const analyzing = ref(false);",
        "const ourRun = '本轮那一跑';",
        "let navLeft = 0, reportedRun = ourRun, resultNull = false, locked = false, resultOverride = null;",
        "let throwPoll = false, throwList = false;",
        "let fundsRefreshed = 0, wakes = 0;",
        # 定时器接管掉：数到上限那一格也能秒级跑完
        "const setTimeout = (fn) => { fn(); return 0; };",
        "const withWakeRetry = async (fn) => { wakes += 1; return fn(); };",
        "const got = [];",
        "const axios = {",
        "  get: async () => { got.push('/api/funds/update-status');",
        "    if (throwPoll) throw new Error('连不上服务');",
        "    const running = navLeft-- > 0;",
        "    return { data: { data: { in_progress: running, started_at: reportedRun,",
        "      last_result: (running || resultNull) ? null",
        "        : (resultOverride || { success: true, message: '更新完成：更新 156 只' }) } } }; },",
        "  post: async (url) => { got.push('POST ' + url);",
        "    if (locked) return { data: { success: false, message: '基金更新正在进行中，请稍后再试' } };",
        "    return { data: { success: true, message: '任务已启动', data: { started_at: ourRun } } }; },",
        "};",
        "const fetchFunds = async () => { fundsRefreshed += 1; if (throwList) throw new Error('列表没取到'); };",
        _const(html, 'FUND_POLL_INTERVAL_MS'), _const(html, 'FUND_POLL_MAX_TRIES'),
        _decl(html, 'fundPollMinutes = () =>'),
        _decl(html, 'waitFundUpdateToFinish = async (since) =>'),
        _decl(html, 'navRoundVerdict = (fin) =>'),
        _decl(html, 'pollFundUpdateStatus = async (since) =>'),
        _decl(html, 'updateAllFunds = async () =>'),
    ])
    out = _run_chain_js("""
(async () => {
  const run = async (left, runId, nul, isLocked, badPoll, badList, override) => {
    alerts.length = 0; got.length = 0; fundsRefreshed = 0; wakes = 0;
    navLeft = left; reportedRun = runId; resultNull = nul; locked = !!isLocked;
    throwPoll = !!badPoll; throwList = !!badList;
    resultOverride = override || null;
    analyzing.value = true;
    await updateAllFunds();
    return { alerts: alerts.slice(), analyzing: analyzing.value,
             refreshed: fundsRefreshed, wakes,
             polls: got.filter((g) => g === '/api/funds/update-status').length };
  };
  const done = await run(0, ourRun, false);                    // 一上来就答"跑完了"
  const slow = await run(2, ourRun, false);                    // 问三次才完
  const foreign = await run(0, '别人那一跑', false);             // 状态换成了别的那一轮
  const lost = await run(0, ourRun, true);                     // 跑完了但接口不再给回执（容器重启）
  const stuck = await run(999, ourRun, false);                 // 数到上限
  const busy = await run(0, ourRun, false, true);              // POST 自己说"正在进行中"
  const pollDead = await run(0, ourRun, false, false, true);   // 更新起来了，进度问不到
  const listDead = await run(0, ourRun, false, false, false, true);  // 更新成了，列表没刷出来
  // 第 66 轮 MAJOR-1 的第二半：跑完了、但它自己说失败。补跑那一侧第 65 轮已有格，
  // 按钮这一侧当时零判据（把那句 if 整行删掉，54 条判据一声不响）。这两格是同判的凭据。
  const failed = await run(0, ourRun, false, false, false, false,
                           { success: false, message: '更新失败：源端这页没答' });
  // 回执里压根没有「成没成」这一项 ⇒ 不许失败开放读成成功
  const unreadable = await run(0, ourRun, false, false, false, false, { message: '只给了话没给结论' });
  // 第 66 轮复评 MI-5：键**在**、值是 `null`（后端没算出来时 JSON 里就是这种形状）。
  // 上一版只判 `undefined` ⇒ 这一格被读成成功、还去刷列表。两格必须同一待遇。
  const nullish = await run(0, ourRun, false, false, false, false,
                            { success: null, message: '回执把 null 当结果交回来了' });
  process.stdout.write(JSON.stringify({ done, slow, foreign, lost, stuck, busy, pollDead, listDead,
                                        failed, unreadable, nullish }));
})();
""", prelude)
    assert out['done']['alerts'] == ['更新完成：更新 156 只'], \
        '按钮报的还是"任务已启动"那一类过程话（第 64 轮 m-2 的另一半）：%s' % out['done']['alerts']
    assert out['done']['analyzing'] is False and out['done']['refreshed'] == 1, \
        '跑完没放开全局锁、或没刷新基金列表：%s' % out['done']
    assert out['done']['wakes'] == 1, '问进度这一笔没过唤醒门 ⇒ 冷启动时整轮判成中断'
    assert out['slow']['polls'] == 3 and out['slow']['refreshed'] == 1, \
        '没等它跑完就开口（只问了 %d 次）：%s' % (out['slow']['polls'], out['slow'])
    assert out['foreign']['refreshed'] == 0 \
        and '更新完成：更新 156 只' not in out['foreign']['alerts'], \
        '别人那一轮的回执被当成结果弹了出来：%s' % out['foreign']['alerts']
    assert any('本轮' in a for a in out['foreign']['alerts']) and out['foreign']['analyzing'] is False, \
        '认不出结果那一格没放开锁（13 个按钮永久灰掉）：%s' % out['foreign']
    assert any('丢了' in a for a in out['lost']['alerts']) and out['lost']['analyzing'] is False, \
        '那一格只会说"没等到结果"，说不出是接口不再给回执（老的那份轮询认这一格）：%s' \
        % out['lost']['alerts']
    assert any('内没结束' in a for a in out['stuck']['alerts']), \
        '超时那一格没说出等了多久（那句话里的分钟数从页面上那两个常数算出来）：%s' % out['stuck']['alerts']
    assert out['stuck']['analyzing'] is False, '超时没放开锁 ⇒ 第 32 轮 A-M1 那一族又回来了'
    assert out['stuck']['polls'] == max_tries, \
        '按钮这一腿数到的是另一个上限（实测 %d 次 vs 页面 %d 次）⇒ 上限有了第二个出处' \
        % (out['stuck']['polls'], max_tries)
    assert out['busy']['alerts'] == ['基金更新正在进行中，请稍后再试'] \
        and out['busy']['polls'] == 0 and out['busy']['refreshed'] == 0 \
        and out['busy']['analyzing'] is False, \
        'POST 说没起来却还是去轮询/报结果了：%s' % out['busy']

    # 第 36 轮 #53 那一族换了个位置复发：更新**已经起来了**，之后那两条腿（问进度、刷列表）
    # 失败时不许把整件事说成"更新失败" —— 那会骗老板再按一次按钮，而按下去只会得到"正在进行中"。
    assert out['pollDead']['analyzing'] is False \
        and any('进度没问到' in a for a in out['pollDead']['alerts']) \
        and not any('更新失败' in a for a in out['pollDead']['alerts']), \
        '问进度这一腿惊动了"更新失败"那句话：%s' % out['pollDead']['alerts']
    assert out['listDead']['alerts'][0] == '更新完成：更新 156 只' \
        and any('列表没刷出来' in a for a in out['listDead']['alerts'][1:]) \
        and not any('更新失败' in a for a in out['listDead']['alerts']), \
        '刷列表这一腿把已成功的更新说成了失败：%s' % out['listDead']['alerts']

    # 跑完但失败 / 读不出成败：不许报「更新完成」、不许刷列表、必须放开那把压着 13 个按钮的锁
    assert out['failed']['alerts'] == ['更新失败：源端这页没答'], out['failed']['alerts']
    assert out['failed']['refreshed'] == 0, '失败那一轮还去刷了列表（把没成功的更新报成了事）'
    assert out['failed']['analyzing'] is False, '失败那一轮没放开全局锁 ⇒ 13 个按钮永久灰掉'
    assert out['unreadable']['refreshed'] == 0 and out['unreadable']['analyzing'] is False, out['unreadable']
    assert any(('成没成' in x) or ('认不出' in x) for x in out['unreadable']['alerts']), \
        '回执里没有 success 这一项时被当成了成功（失败开放）：%s' % out['unreadable']['alerts']
    assert out['nullish']['refreshed'] == 0 and out['nullish']['analyzing'] is False, out['nullish']
    assert any(('成没成' in x) or ('认不出' in x) for x in out['nullish']['alerts']), \
        'success 是 `null`（键在、值没算出来）时被读成成功：%s' % out['nullish']['alerts']
    for cell in ('failed', 'unreadable', 'nullish'):
        assert '更新完成' not in ' '.join(out[cell]['alerts']), '%s 那一格里冒出更新完成：%s' % (cell, out[cell])

    # 两份循环合一处：页面里只有一份"问净值进度"的实现
    # 这一问只许有一处实现：定义 1 处 + 按钮 1 处 + 补跑 1 处（第 66 轮：两条腿共用一把尺子）
    assert html.count('navRoundVerdict') == 3, \
        '「这一轮算不算成功」出现了 %d 处（应为定义 1 + 两条腿各 1）⇒ 判法又多了一份' % html.count('navRoundVerdict')
    assert html.count('waitFundUpdateToFinish') == 3, \
        '引用点对不上（定义 1 处 + 按钮 1 处 + 补跑 1 处），现 %d 处' % html.count('waitFundUpdateToFinish')
    assert html.count('/api/funds/update-status') == 1, \
        '页面里"问净值进度"出现了 %d 处 ⇒ 又有了一份自己的轮询' % html.count('/api/funds/update-status')
    assert 'setInterval' not in html[html.index('const pollFundUpdateStatus'):
                                    html.index('const generateAdvice')], \
        '基金页又长出一份自己的轮询 ⇒ 同一个问题两把尺子'
    for gone in ('FUND_UPDATE_POLL_TIMEOUT_MS', 'lastFundUpdateFinishedAt', 'clearFundUpdatePoll',
                 'fundUpdatePollTimer'):
        assert gone not in html, '旧那一份轮询的 %s 还留着 ⇒ 上限与处理就有了第二个出处' % gone
