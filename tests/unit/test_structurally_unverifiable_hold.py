# -*- coding: utf-8 -*-
"""「结构性不可验」这把重问锁的判据（任务 #8）。

要钉的三件事，缺一不可：
1. **只有真问过、且数据源答"这段给不出"**才配锁 —— 别的失败（点数不够、没档案）
   明天可能就自愈，锁了它等于亲手把一条可验的预测藏起来；
2. 锁**会自己到期**，到点这条预测回到到期队列再问一次（不是终态、不是软删）；
3. 页面上**减出来的两个数要能加回去**（`due` + `unverifiable` ＝ 全部到期未判），
   否则"到期 0 条"会被读成"全都验完了"。

第 2 点还有一条**支撑它的不变式**（用例 4）：`next_verify_date` 的两个创建出口都
把它夹在目标日之前 ⇒ "已到期且这个日期晚于今天"只可能由验证器真问过之后写下。
哪天有人让排期越过目标日，"结构性不可验"就会开始冤枉人，所以这条得钉住。
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.models.database import Blogger, Post, Prediction
from src.services import prediction_lifecycle as lc
from src.services.prediction_lifecycle import (
    DUE_UNVERIFIED, UNVERIFIABLE, classify, filter_due_for_verify,
    filter_unverifiable, unverifiable_retry_days,
)
from src.fund import backfill_proofs
# "这条分支会不会被走到"这件事，全仓只许有一把尺子（第 56 轮 M-3）：
# 守卫侧那份从第 45 轮起就在做常量折叠 + 自己算常量比较，这里不再搓第二份。
from tests.unit.test_script_db_guards import _is_dead_test


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# "今天"取**北京那把钟**，不是 `date.today()`（第 58 轮 m-3）：这一档的判据里 `TODAY` 既用来造样品、
# 又用来对 `classify(as_of=…)` 的答案 —— 生产容器在 UTC，北京 00:00~08:00 那八小时里 `date.today()`
# 比 `current_as_of()` 小一天，于是同一份样品在两个时区对上的是**两个不同的参照日**。
# 本机在 +8 看不见这件事（这正是第 52 轮 B-3 给验证路径换北京钟的同一条理由）。
TODAY = lc.current_as_of()


def _seed(db, *, target, next_verify=None, fund_code='HOLD01', pred_type='up'):
    blogger = db.query(Blogger).filter(Blogger.name == '重问锁博主').first()
    if not blogger:
        blogger = Blogger(name='重问锁博主', platform='wechat')
        db.add(blogger)
        db.flush()
    post = Post(blogger_id=blogger.id, title='hold', content='hold',
                post_date=target - timedelta(days=7))
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id,
                   fund_code=fund_code, fund_name='重问锁基金', sector='测试',
                   prediction_type=pred_type, prediction_date=post.post_date,
                   prediction_period='1周', target_date=target, status='pending',
                   next_verify_date=next_verify, is_expired=False)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _service(db, monkeypatch, availability):
    """把验证器打到"只剩数据充分性判断"这一层：不外呼、不碰净值。"""
    import importlib
    from src.services.prediction_verify_service import PredictionVerifyService

    api = importlib.import_module('src.fund.fund_api')
    monkeypatch.setattr(api.fund_data_manager, 'backfill_history_range',
                        lambda *a, **k: False, raising=True)
    svc = PredictionVerifyService(db)
    monkeypatch.setattr(type(svc), 'match_fund_for_prediction',
                        lambda self, p: (p.fund_code, p.fund_name))
    monkeypatch.setattr(type(svc), '_check_fund_data_availability',
                        lambda self, **kw: dict(availability))
    return svc


def test_a_structural_verdict_holds_the_row_out_of_todays_queue(test_db, monkeypatch):
    """问过、数据源答"这段没有"⇒ 今天起不入队，且**一个结论都没写**。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history',
        'message': '目标日附近这段历史净值不足'})

    result = svc.verify_prediction(p.id)

    assert result['success'] is False
    held = TODAY + timedelta(days=unverifiable_retry_days())
    assert result['held_until'] == held.isoformat()
    test_db.refresh(p)
    assert p.next_verify_date == held
    # 它不是结论：判对/判错、分数、状态全都不许动
    assert p.is_correct is None and p.verify_score in (0, None)

    as_of = TODAY
    assert classify(p, as_of=as_of) == UNVERIFIABLE
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=as_of)}
    assert {x.id for x in filter_unverifiable(test_db, as_of=as_of)} == {p.id}
    assert '结构性不可验' in (lc.due_skip_reason(p, as_of=as_of) or '')
    # 这句话不许自指（第 53 轮 A-1）：被压住的原因是两档，而行上没有 reason 列，
    # "见上方「上次验证未成功原因」"既指不到、那一栏印的也正是这句本身
    reason_line = lc.due_skip_reason(p, as_of=as_of)
    assert '见上方' not in reason_line and '哪两种原因' not in reason_line


def test_a_failure_that_was_never_asked_about_does_not_hold_the_row(test_db, monkeypatch):
    """反面对照：同样是"验不了"，`insufficient_points`（还没问过源端）不许被锁。

    没有这一条，上面那条用例对"逢失败就锁"的写法结构上不会红 —— 而那会把
    明天就能自愈的预测永久藏出到期队列。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'insufficient_points',
        'message': '基金数据不足，请更新基金数据后再验证'})

    result = svc.verify_prediction(p.id)

    assert result['held_until'] is None
    test_db.refresh(p)
    assert p.next_verify_date is None
    assert classify(p, as_of=TODAY) == DUE_UNVERIFIED
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_the_lock_expires_and_the_prediction_asks_again(test_db, monkeypatch):
    """到重问日它必须自己回到到期队列 —— 这是"锁"和"终态"的唯一区别。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history',
        'message': '目标日附近这段历史净值不足'}).verify_prediction(p.id)
    test_db.refresh(p)
    held = p.next_verify_date

    # 锁住的是"重问日之前"：那一天之前不入队，到了那天自己回队（边界在 SQL 与
    # classify 两处都是 `hold > as_of`，写反一天就等于这批永远不回队）
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=held - timedelta(days=1))}
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=held)}
    assert classify(p, as_of=held) == DUE_UNVERIFIED
    assert filter_unverifiable(test_db, as_of=held) == []
    # 锁的天数跟着凭据 TTL 走，不是拍出来的常数
    assert held == TODAY + timedelta(days=int(backfill_proofs.EMPTY_TTL_DAYS) + 1)


def test_the_two_structural_tiers_wait_for_their_own_clock(test_db, monkeypatch):
    """两档各等各的时钟（第 54 轮 A-5 的账）：有凭据的跟凭据 TTL，没凭据的跟净值回补范围。

    镜像上 5 行 `same_nav_endpoint` 以前跟的是 3 天那一档 ⇒ 每三天弹回「待验证到期」
    被问一次、再弹回去，**永远没有终局** —— 而这一档依据的事实（那段窗口里只有那一条净值）
    不会因为过了三天就变。改变它只有一条路：新净值行落进这段窗口，而常规同步只往回拉
    `config.NAV_HISTORY_LOOKBACK_DAYS` 天 ⇒ 间隔就取那个数 +1，出处只有一个。
    """
    from src.core.config import config

    # 两档必须是**两个不同的数**，否则"分档"是装饰
    ttl_days = unverifiable_retry_days('no_source_history')
    endpoint_days = unverifiable_retry_days('same_nav_endpoint')
    assert ttl_days == int(backfill_proofs.EMPTY_TTL_DAYS) + 1, '有凭据那档不再跟凭据 TTL ⇒ 凭据过期前就重问'
    assert endpoint_days == int(config.NAV_HISTORY_LOOKBACK_DAYS) + 1, \
        '退化端点这一档的间隔不是从"同步能回补多久"算出来的 ⇒ 又变回拍的常数'
    assert endpoint_days > ttl_days, '两档同数 ⇒ 上面那条分档白分，5 行照旧每三天弹一次'

    p = _seed(test_db, target=TODAY - timedelta(days=60), fund_code='LOOK01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['held_until'] == (TODAY + timedelta(days=endpoint_days)).isoformat()
    assert p.is_correct is None and p.is_deleted is False, '拉长重问间隔不是把它关掉'
    # 到点仍要自己回队 —— 这一档不是终态，与上一批的边界一样
    held = p.next_verify_date
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=held - timedelta(days=1))}
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=held)}


def test_the_page_repeats_the_reask_day_from_the_row_itself(test_db, monkeypatch):
    """页面灰字那句"『日期』自动重问"不许自己算一份间隔 —— 只许读行上那一根。

    间隔从今天起分两档（3 天 / 31 天），页面上只要写死任一个数就会有一半行说错话；
    而 `{{ p.next_verify_date }}` 这种"把接口给的日期原样印出来"才是对的形状。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=60), fund_code='WORD01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})

    result = svc.verify_prediction(p.id)
    test_db.refresh(p)
    assert result['held_until'] == p.next_verify_date.isoformat(), \
        '回执报的重问日与行上排的不是一天 ⇒ 页面与接口各说一份'

    html = (PROJECT_ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    line = [l for l in html.splitlines() if '自动重问' in l]
    assert line, '页面那句"自动重问"不见了 ⇒ 这一档的话没人说了'
    assert 'p.next_verify_date' in line[0], '重问日不再读行上那一根'
    for b in ('+3', '+ 3', 'three', '31'):
        assert b not in line[0], '页面上写死了间隔（%s）⇒ 两档节奏一分叉它就说错话' % b


def _src_trees(*, skip=()):
    """把 `src/` 下所有 .py 解析成 (相对路径, ast, 源码)。解析不了的文件要报出来。"""
    import ast as _ast
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    out = []
    for path in sorted((root / 'src').rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        src = path.read_text(encoding='utf-8')
        out.append((rel, _ast.parse(src), src))
    return out


def _literal_backfill_windows(trees):
    """谁把"同步往回拉多少天"又写死了一遍 —— 返回违规点 `文件:行号 名字`。

    三种形状都算：调用点上的 `days=<字面量>`、函数签名里 `days=<字面量>` 的默认值，
    以及**按位置**把天数塞进 `days` 槽（第 56 轮 M-1：上一版只认前两种，而
    `update_fund_history(code, 30, db=None)` 与 `update_fund_history(code, days=30)`
    是同一件事的两种拼写 —— 评审注入前者，1226 条一声不响）。
    位置这一腿不猜：先从**同一批被扫的树**里量出每个咽喉的 `days` 在第几个槽（绑定方法
    去掉 `self`），再回头看调用点第 N 个位置实参是不是整数字面量。
    只看 `update_fund_history` / `_update_fund_history` 这两个"每日同步真的往回拉净值"的
    咽喉 ⇒ `get_fund_history(code, days=1)`（只问最新一笔）那种另作一用的不在这条账上。
    """
    import ast as _ast

    names = ('update_fund_history', '_update_fund_history')
    slots = {}                                  # 函数名 → {调用形态: days 的位置序号}
    for _rel, tree, _src in trees:
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if node.name not in names:
                continue
            pos = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)]
            if 'days' not in pos:
                continue
            i = pos.index('days')
            # 定义里带 `self`/`cls` ⇒ 调用方不传那一个；静态方法/普通函数不扣
            bound_only = pos[:1] in (['self'], ['cls']) and not any(
                isinstance(d, _ast.Name) and d.id in ('staticmethod', 'classmethod')
                for d in node.decorator_list)
            slots.setdefault(node.name, {})[bound_only] = i - 1 if bound_only else i

    hits = []
    for rel, tree, _src in trees:
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                is_bound = isinstance(node.func, _ast.Attribute)
                fn = node.func.attr if is_bound else getattr(node.func, 'id', '')
                if fn not in names:
                    continue
                if node.keywords:
                    for kw in node.keywords:
                        if kw.arg == 'days' and isinstance(kw.value, _ast.Constant) \
                                and isinstance(kw.value.value, int):
                            hits.append('%s:%s %s(days=%s)'
                                        % (rel, node.lineno, fn, kw.value.value))
                slot = slots.get(fn, {}).get(is_bound)
                if slot is None and len(node.args) > 1:
                    # 量不到槽位（那个咽喉的 `def` 不在这批被扫的树里 —— 例如它哪天挪进第三方包，
                    # 或 `_src_trees(skip=…)` 跳掉了它）⇒ **不许按调用形状猜**（第 57 轮 M-2：
                    # 上一版兜底写死 `1 if is_bound else 2`，于是裸函数 `update_fund_history(code, 30)`
                    # 整个漏掉，而它印出来的仍然是"零违规"）。改成：任何一个整数字面量位置实参都点名。
                    for i, a in enumerate(node.args):
                        if isinstance(a, _ast.Constant) and isinstance(a.value, int):
                            hits.append('%s:%s %s(第 %d 个位置实参=%s) ⇒ 量不到 days 的槽位，'
                                        '按"可能是天数"报' % (rel, node.lineno, fn, i, a.value))
                elif slot is not None and len(node.args) > slot:
                    a = node.args[slot]
                    if isinstance(a, _ast.Constant) and isinstance(a.value, int):
                        hits.append('%s:%s %s(第 %d 个位置实参=%s) ⇒ 位置参数绕过了 days 关键字'
                                    % (rel, node.lineno, fn, slot, a.value))
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                    and node.name in names:
                args = node.args
                defaults = args.defaults
                posnames = args.args + args.posonlyargs + args.kwonlyargs
                offset = len(posnames) - len(defaults)
                for i, d in enumerate(defaults):
                    if isinstance(d, _ast.Constant) and isinstance(d.value, int) \
                            and posnames[offset + i].arg == 'days':
                        hits.append('%s:%s def %s(days=%s 默认值)'
                                    % (rel, node.lineno, node.name, d.value))
    return hits


def test_the_sync_lookback_is_not_hard_coded_at_any_call_site():
    """那句"重问间隔跟着常规同步的回补范围走"要有牙（第 55 轮 M-1）。

    上一版这条判据只 AST 读 `FundAPI.get_fund_history` 的**签名默认值** —— 结构性看不见
    真跑同步的那 8 处 `days=30`（复核 `git grep -n "update_fund_history" 4ef48ce -- src/ | grep days`
    ⇒ 两个默认值 + 五处调用实参 + 一处 demo）⇒ 键改成 47 时间隔变 48、同步照旧只拉 30 天，
    文档那句因果当场是假的。
    """
    violations = _literal_backfill_windows(_src_trees())
    assert violations == [], \
        '每日同步的回补范围又出现写死的天数 ⇒ 它和重问间隔只是"今天恰好相同"：\n' + \
        '\n'.join(violations)

    # 反向对照：这个扫描器必须真的抓得到那种写法（否则上面那条空断言是装饰）
    import ast as _ast
    fake = ('def update_fund_history(self, fund_code, days=30, db=None):\n'
            '    api.get_fund_history(fund_code, days)\n'
            'def caller(mgr):\n'
            '    mgr.update_fund_history("000001", days=30, db=None)\n'
            'def caller2(mgr):\n'
            '    mgr.update_fund_history("000001", 30, db=None)\n')
    caught = _literal_backfill_windows([('fake/sync.py', _ast.parse(fake), fake)])
    assert len(caught) == 3 and all('fake/sync.py' in c for c in caught), \
        '扫描器抓不到"签名默认值 + 关键字实参 + 位置实参"这三种写法 ⇒ 上面那条零违规是空判：%s' \
        % caught
    # 第 56 轮 M-1 的本体：位置参数那一腿单独验一次，**并且它不许顺手把正常写法拦成违规**
    positional_only = ('def update_fund_history(self, fund_code, days=None, db=None):\n'
                       '    return days\n'
                       'def ok_keyword(mgr, code, n):\n'
                       '    mgr.update_fund_history(code, days=n)\n'
                       'def ok_omitted(mgr, code):\n'
                       '    mgr.update_fund_history(code, db=None)\n'
                       'def ok_positional_variable(mgr, code, n):\n'
                       '    mgr.update_fund_history(code, n)\n'
                       'def bad_positional(mgr, code):\n'
                       '    mgr.update_fund_history(code, 45)\n')
    trees = [('fake/pos.py', _ast.parse(positional_only), positional_only)]
    hits = _literal_backfill_windows(trees)
    assert len(hits) == 1 and '45' in hits[0] and '位置' in hits[0], \
        '位置传天数要么拦不住、要么把正常写法一起拦了（实测 %r）⇒ M-1 那一半没修上' % hits
    # 第 57 轮 M-2：那批树里**量不到 def** 时不许按调用形状猜槽位（旧兜底 `1 if is_bound else 2`
    # 让裸函数 `update_fund_history(code, 30)` 整个漏掉，而它照样印"零违规"）
    no_def = ('def bound(mgr, code):\n    mgr.update_fund_history(code, 30)\n'
              'def bare(fn, code):\n    update_fund_history(code, 30)\n')
    hits2 = _literal_backfill_windows([('fake/nodef.py', _ast.parse(no_def), no_def)])
    assert len(hits2) == 2 and all('量不到 days 的槽位' in h for h in hits2), \
        '量不到槽位时要么整个漏、要么静默按猜的数走（实测 %r）' % hits2
    # 对照：同一个调用传的是**变量**时不许点名（否则这条兜底把正常代码全打成违规）
    ok_var = 'def bare(fn, code, n):\n    update_fund_history(code, n)\n'
    assert _literal_backfill_windows([('fake/v.py', _ast.parse(ok_var), ok_var)]) == [], \
        '兜底那一腿过宽：位置传变量也被拦 ⇒ 这道闸会把自己建成墙'
    # 过宽对照：另作一用的 `get_fund_history(days=1)`（只问最新一笔）不许被算进来
    benign = ('def probe(api, code):\n'
              '    return api.get_fund_history(code, days=1)\n')
    assert _literal_backfill_windows([('fake/probe.py', _ast.parse(benign), benign)]) == [], \
        '把"只取最新一笔"的显式天数也拦了 ⇒ 这道闸会把自己建成墙'


def test_the_backfill_window_is_read_at_call_time_not_at_import(monkeypatch):
    """`nav_backfill_days()` 必须**在调用时**读那个键。

    签名默认值（`days: int = config.X`）在 import 那一刻就算死了 ⇒ 改键不动它，
    而判据用 monkeypatch 也量不出来（它看的还是那份算好的默认值）。
    这一条同时是对上面那句"跟着回补范围走"的外部真值检查。
    """
    from src.core.config import config
    from src.services.prediction_lifecycle import nav_backfill_days

    monkeypatch.setattr(config, 'NAV_HISTORY_LOOKBACK_DAYS', 47, raising=False)
    assert nav_backfill_days() == 47, '取数处不在调用时读键 ⇒ 那个键其实是装饰'
    assert nav_backfill_days(7) == 7, '显式给的天数必须照收（回放脚本按区间补拉要用它）'
    # 真走一遍写入路径：`update_fund_history` 交给源端的天数必须就是这个数
    import importlib
    api = importlib.import_module('src.fund.fund_api')
    seen = {}

    def spy(fund_code, days):
        seen['days'] = days
        return []

    monkeypatch.setattr(api.fund_data_manager.api, 'get_fund_history', spy, raising=True)
    assert api.fund_data_manager.update_fund_history('000001') == 0
    assert seen['days'] == 47, \
        '同步实际往回拉的天数不跟着键动（实测 %s）⇒ "重问间隔＝回补范围 +1"是巧合' % seen.get('days')


def test_a_nav_backfill_releases_the_hold_and_the_row_goes_back_to_due(test_db, monkeypatch):
    """净值真的补进这段窗口 ⇒ 当场撤锁、回到「待验证到期」，不用等满一个间隔（第 55 轮 M-2）。

    §2e 给老板指的三条出路里"补拉成功当场解锁"这一条**当时不存在**：
    `release_unverifiable_hold` 只有验证器一个调用方，而被锁的行在 `filter_due_for_verify`
    就被减出队列 ⇒ 没人再问它一次，第二天就补到的净值也要白等 31 天。
    这里跑的是真 sqlite + 真验证器 + 真解除函数，没有 Mock。
    """
    from src.models.database import FundHistory

    target = TODAY - timedelta(days=3)
    p = _seed(test_db, target=target, fund_code='UNLOCK01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})
    svc.verify_prediction(p.id)
    test_db.refresh(p)
    assert lc.classify(p, as_of=TODAY) == UNVERIFIABLE, '前提没立住：这一行没被锁'
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}

    # 同步补到了这段窗口的净值（两条点、终点就是目标日 ⇒ 验证器判得出来）
    test_db.add(FundHistory(fund_code='UNLOCK01', fund_name='重问锁基金',
                            nav_date=p.prediction_date, nav=1.0, day_growth=0.1))
    test_db.add(FundHistory(fund_code='UNLOCK01', fund_name='重问锁基金',
                            nav_date=target, nav=1.2, day_growth=0.2))
    test_db.commit()

    released = lc.release_holds_after_nav_update(
        test_db, 'UNLOCK01', [p.prediction_date, target])
    test_db.commit()
    test_db.refresh(p)

    assert released == [p.id], '补到了净值却没撤锁 ⇒ §2e 那句"当场解锁"仍然是空头话'
    assert p.next_verify_date is None
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}, \
        '撤了锁却没回到到期队列 ⇒ 页面照样看不见它'
    assert p.is_correct is None, '撤锁不是下结论'


def test_the_daily_sync_writer_is_the_thing_that_unlocks(test_db, monkeypatch):
    """端到端（第 56 轮 M-2 的第二半）：从**同步那条路**打进去，而不是直接调解除函数。

    上面三条各自验过"传对参数就解锁"，但没有一条穿过 `update_fund_history` ——
    于是"接线"这件事的真实形状（同步有没有真的把**新落的那几天**递出来）在绿灯里没人问过：
    评审把实参换成 `[]`（＝第一道闸当场失效），那三条判据 85 passed 一声不响。
    """
    import importlib

    from src.models.database import FundHistory

    target = TODAY - timedelta(days=4)
    p = _seed(test_db, target=target, fund_code='E2E01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})
    svc.verify_prediction(p.id)
    test_db.refresh(p)
    assert lc.classify(p, as_of=TODAY) == UNVERIFIABLE, '前提没立住：这一行没被锁'

    api = importlib.import_module('src.fund.fund_api')
    # 源端这两次分别答"补到窗口里"与"只补到窗口外"，看锁的两种结局
    monkeypatch.setattr(api.fund_data_manager.api, 'get_fund_history',
                        lambda code, days=None: [
                            {'date': p.prediction_date, 'nav': 1.0, 'growth': 0.1},
                            {'date': target, 'nav': 1.2, 'growth': 0.2}],
                        raising=True)
    assert api.fund_data_manager.update_fund_history('E2E01', db=test_db) == 2
    test_db.commit()
    test_db.refresh(p)
    assert p.next_verify_date is None, '同步补到了窗口里的净值，锁却还压着 ⇒ 那条出路又点不到了'
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}

    # 对照：把锁再压回去，这次源端只给**窗口之外**的日子 ⇒ 同步照常入库，但锁不许动
    lc.apply_unverifiable_hold(p, verdict_reason='same_nav_endpoint', as_of=TODAY)
    test_db.commit()
    test_db.refresh(p)
    hold = p.next_verify_date
    assert hold is not None and hold > target
    outside = target + timedelta(days=2)
    monkeypatch.setattr(api.fund_data_manager.api, 'get_fund_history',
                        lambda code, days=None: [
                            {'date': outside, 'nav': 1.5, 'growth': 0.3}], raising=True)
    assert api.fund_data_manager.update_fund_history('E2E01', db=test_db) == 1
    test_db.commit()
    test_db.refresh(p)
    assert p.next_verify_date == hold, '补的是这段窗口之外的行也撤了锁 ⇒ 每天同步都会把它弹回队列'
    assert test_db.query(FundHistory).filter_by(fund_code='E2E01', nav_date=outside).first(), \
        '净值行也没落库 ⇒ 这条对照验的其实是"同步整个没跑"，不是那道窗口闸'


def test_the_fund_sync_writer_leg_also_unlocks(test_db, monkeypatch):
    """第二条往净值表插行的腿（`FundSyncManager._update_fund_history`）也要有**行为**判据。

    第 57 轮 M-1：上一批那条端到端只穿了 `fund_api` 一条腿 ⇒ 把同步器这条腿的解锁
    搬进 `def _release()`、再把唯一调用点压进 `if 1 == 0:`，1228 条全绿、
    而"页面点一次更新基金就当场解锁"这句话在这条路上**当场是假的**（评审实测）。
    """
    import importlib

    from src.models.database import FundHistory

    target = TODAY - timedelta(days=5)
    p = _seed(test_db, target=target, fund_code='E2E02')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})
    svc.verify_prediction(p.id)
    test_db.refresh(p)
    assert lc.classify(p, as_of=TODAY) == UNVERIFIABLE, '前提没立住：这一行没被锁'

    api_module = importlib.import_module('src.fund.fund_api')
    sync_module = importlib.import_module('src.fund.fund_sync_manager')
    monkeypatch.setattr(api_module.fund_api, 'get_fund_history',
                        lambda code, days=None: [
                            {'date': p.prediction_date, 'nav': 1.0, 'growth': 0.1},
                            {'date': target, 'nav': 1.2, 'growth': 0.2}], raising=True)
    answered = sync_module.FundSyncManager()._update_fund_history(
        test_db, 'E2E02', '重问锁基金')
    test_db.commit()
    test_db.refresh(p)
    assert answered == 2, '这条腿自己没跑通（历史接口答了 %s 条）⇒ 下面那条断言会是空判' % answered
    assert p.next_verify_date is None, '同步器这条腿补到净值不解锁 ⇒ §2e 那条出路又变成点不到的了'
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}
    assert test_db.query(FundHistory).filter_by(fund_code='E2E02').count() == 2


def test_a_backfill_that_still_cannot_evidence_the_window_keeps_the_hold(test_db, monkeypatch):
    """反面对照（防上一条被写成"补了就无条件撤"）：新行落进来但**还是判不出来** ⇒ 不许撤。

    无条件撤锁的结果比不撤更坏：这条预测当天就弹回「待验证到期」，
    下一次验证又判出同一个结构性结论、再锁一次 ⇒ 老板要清零的那一档天天回潮。
    """
    from src.models.database import FundHistory

    target = TODAY - timedelta(days=3)
    p = _seed(test_db, target=target, fund_code='KEEP01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})
    svc.verify_prediction(p.id)
    test_db.refresh(p)
    hold = p.next_verify_date
    assert hold is not None and hold > target

    # 只补进窗口一条点 < VERIFY_MIN_DATA_POINTS ⇒ 验证器仍然判不出来
    test_db.add(FundHistory(fund_code='KEEP01', fund_name='重问锁基金',
                            nav_date=p.prediction_date, nav=1.0, day_growth=0.1))
    test_db.commit()

    assert lc.release_holds_after_nav_update(test_db, 'KEEP01',
                                             [p.prediction_date]) == []
    test_db.refresh(p)
    assert p.next_verify_date == hold, '还是问得不出答案却撤了锁 ⇒ 这把锁变成一天一次的噪音'

    # 另一格：没被锁过的行（正常排期）一个字都不许动
    q = _seed(test_db, target=target, fund_code='KEEP02',
              next_verify=target - timedelta(days=1))
    test_db.add(FundHistory(fund_code='KEEP02', fund_name='重问锁基金',
                            nav_date=target, nav=1.1, day_growth=0.1))
    test_db.commit()
    assert lc.release_holds_after_nav_update(test_db, 'KEEP02', [target]) == []
    test_db.refresh(q)
    assert q.next_verify_date == target - timedelta(days=1), \
        '它把"创建时排的那根日期"也清了 ⇒ 这条路在改的不是重问锁'


def test_a_backfill_outside_the_held_window_does_not_release_the_hold(test_db, monkeypatch):
    """补的行**不落在这条预测的窗口里** ⇒ 不许撤锁（第二道闸，与上一条同一个库形状）。

    少了这道闸会复现第 54 轮 A-5 刚修掉的那件事：每天同步在别处补到一行，
    就把这段永远问不出来的窗口撤回「待验证到期」⇒ 下一次验证再锁一次，
    节奏从"每三十天一次真有机会改变的询问"变回"每天弹一次"。
    """
    from src.models.database import FundHistory

    target = TODAY - timedelta(days=3)
    p = _seed(test_db, target=target, fund_code='GATE01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})
    svc.verify_prediction(p.id)
    test_db.refresh(p)
    hold = p.next_verify_date
    assert hold is not None and hold > target

    # 库里现在给得出这段窗口（两个点、终点就是目标日）⇒ 只有"补的是哪几天"能拦住撤锁
    test_db.add(FundHistory(fund_code='GATE01', fund_name='重问锁基金',
                            nav_date=p.prediction_date, nav=1.0, day_growth=0.1))
    test_db.add(FundHistory(fund_code='GATE01', fund_name='重问锁基金',
                            nav_date=target, nav=1.2, day_growth=0.2))
    test_db.commit()

    assert lc.release_holds_after_nav_update(
        test_db, 'GATE01', [target + timedelta(days=1)]) == [], \
        '补的是目标日之后的另一段 ⇒ 这把锁问的那件事一个字都没变'
    test_db.refresh(p)
    assert p.next_verify_date == hold

    assert lc.release_holds_after_nav_update(test_db, 'GATE01', [target]) == [p.id], \
        '同样两个点，补的正是窗口里那天就该撤 ⇒ 上面那条不是"永远不撤"'


# 每一条"往 `fund_history` 落行"的路都必须在这里登记**处置**：
#   `'releases'` ⇒ 函数体里必须真的接 `release_holds_after_nav_commit`；
#   其它字符串   ⇒ 为什么不接的**依据**（登记了却不解释、或解释了却已不存在，都红）。
NAV_WRITE_SITES = {
    ('src/fund/fund_api.py', 'update_fund_history'): 'releases',
    ('src/fund/fund_sync_manager.py', '_update_fund_history'): 'releases',
    ('src/fund/fund_api.py', 'backfill_history_range'):
        '它跑在 `verify_prediction` 内部：同一次验证在两行之后才读 `previous_hold`，'
        '在这里撤锁等于把"问过两次"的证据自己清掉 ⇒ 该行永远停在"第一次问出来"，'
        '第 52 轮那条"锁 ⇒ 关"的升级链会断。补到的净值同一次就被 '
        '`_check_fund_data_availability` 重问，不需要提前撤。',
    ('src/services/fund_service.py', 'add_history'):
        '`src/` 与 `scripts/` 零调用方（死路）⇒ 按仓库规矩不给死路写绿灯判据；'
        '谁把它接上活路，就必须同时把这条改成 `releases` 并接上解锁。',
    ('src/services/data_portability_service.py', 'import_data'):
        '整库导入（`/api/config/import`）是一条**换库**的路，不是"补了几行净值"：'
        '它按 `TABLE_SPECS` 泛型建行、`replace` 模式还先把整表删掉 ⇒ 这里没有'
        '"新落的那几天"这个量可递，拿它去撤锁等于把"问过两次"的证据清掉。'
        '导完之后该做的是一次「更新基金」+「验证全部」（那两条会各自接解锁），'
        '而不是在导入事务里顺手解几把锁。第 56 轮 m-3 把它登记进来，'
        '是因为上一版扫描只认点名构造 `FundHistory(...)` 的写法，对它结构性失明。',
}


def _never_runs(test):
    """这个条件恒假吗 —— **共用守卫那把尺子**，不再本地搓第二份（第 56 轮 M-3）。

    上一版这里是一份三行的简化实现（`literal_eval` + 只认 `and` 里压恒假臂），于是
    `while False:`、`if ins and 1 == 0:`、恒假三目 三种形状全被判成"已接线"——
    而 `literal_eval` 根本不算比较，`1 == 0` 求不出来就当成活的。
    守卫侧那份（`test_script_db_guards._is_dead_test`）从第 45 轮起就在做常量折叠 +
    自己算常量比较，两条判据问的是**同一件事** ⇒ 只许有一份。
    """
    return _is_dead_test(test)


def _empty_container(node):
    """这个表达式是不是"明摆着的一个空东西"（`[] () {} set() list() None 0 ''`）。"""
    import ast as _ast

    if isinstance(node, _ast.Constant):
        return not node.value if node.value is not None else True   # None / 0 / '' 都算空
    if isinstance(node, (_ast.List, _ast.Tuple, _ast.Set, _ast.Dict)) and not node.elts:
        return True
    return (isinstance(node, _ast.Call) and not node.args and not node.keywords
            and getattr(node.func, 'id', '') in ('list', 'set', 'tuple', 'dict'))


def _bound_value(nodes, name):
    """这些节点里"唯一一次把值绑给 `name`"的那个数（第 59 轮 M-3 把三种绑法并成一处）。

    上一版只认 `ast.Assign` ⇒ `d: list = []`（带标注）与 `(d := [])`（海象）两种日常写法
    完全隐身：掏空的容器换个绑法就重新变成"递到了"。
    来路不止一次 ⇒ 交回 `None`（看不清就算递到了，这条闸拦的是"明着掏空"）。
    """
    import ast as _ast

    found = []
    for n in nodes:
        if isinstance(n, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id == name for t in n.targets):
            found.append(n.value)
        elif isinstance(n, _ast.AnnAssign) and isinstance(n.target, _ast.Name) \
                and n.target.id == name and n.value is not None:
            found.append(n.value)
        elif isinstance(n, _ast.NamedExpr) and isinstance(n.target, _ast.Name) \
                and n.target.id == name:
            found.append(n.value)
    return found[0] if len(found) == 1 else None


def _is_accumulated(nodes, name):
    """这些节点里有没有对 `name` 的**累加**（`append/extend/add/insert/update`）。"""
    import ast as _ast

    return any(isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute)
               and isinstance(c.func.value, _ast.Name) and c.func.value.id == name
               and c.func.attr in ('append', 'extend', 'add', 'insert', 'update')
               for c in nodes)


def _passes_the_new_dates(call, fn=None, live=None):
    """那次调用有没有真的把"新落的那几天"递进去（第 56 轮 M-2，第 57 轮补一跳回溯）。

    `release_holds_after_nav_commit(db, code)` —— 少递第三个实参 —— 在代码里长得和
    正确写法一模一样，行为却退化成"这段窗口一个字没变也把锁撤了"（`changed_dates=None`
    ⇒ 两道闸里的第一道直接跳过）。写死的空容器同理。
    **一跳回溯**：`d = []; release_holds_after_nav_commit(db, code, d)` 与直接写 `[]`
    是同一件事（第 57 轮 M-1 的第④格：上一版只认字面量，换个变量名就放行）。
    只回溯"这个函数里唯一一次 `d = …`"那种简单赋值 —— 来路看不清时**算递到了**，
    因为这条闸拦的是"明着掏空"，不是"我看不出你递了什么"（拦后者会把正常写法打成没接）。
    **第 58 轮 M-2**：这一腿找"唯一一次赋值"与"有没有累加"必须走**活路径**（`live`），
    与调用点那一腿共用同一套剪枝。上一版这两半用两套尺子：调用点已经改成"只看活路径"，
    这里还在 `ast.walk` 整棵树 ⇒ 一行诱饵就买通整条判据 ——
    `d = []` + `def _never(): d.append(1)` + 递 `d`，运行时那个 def 从不被叫、`d` 永远是空的，
    而判据看见"有 append"就点头（实测 True）。
    **第 59 轮 M-3**：那"一次赋值"必须认全**三种绑法**（`d = []` / `d: list = []` / `(d := [])`），
    否则掏空的东西换个绑法就又是"递到了"（探针实测前两格回 True）。
    """
    import ast as _ast

    dates = call.args[2] if len(call.args) > 2 else next(
        (k.value for k in call.keywords or [] if k.arg == 'changed_dates'), None)
    if dates is None or _empty_container(dates):
        return False
    if isinstance(dates, _ast.Name) and (live is not None or fn is not None):
        nodes = live if live is not None else list(_ast.walk(fn))
        assigned = _bound_value(nodes, dates.id)
        # 关键分界：`inserted = []` 然后一路 `inserted.append(...)` 是**正常累加**（真代码就是这个形状），
        # 而 `d = []` 之后一个字没加就递进去才是"明着掏空"。少了这一句，回溯会把每条正常同步
        # 都判成没接 —— 过宽的闸活不过一轮就会被整条关掉（本仓第 47 轮那条教训）。
        if assigned is not None and _empty_container(assigned) \
                and not _is_accumulated(nodes, dates.id):
            return False
    return True


def _match_supported():
    """这台解释器认不认 `match` 语法（3.10 起）。不认时那两格样品**喂不进去** ⇒ 跳过它，
    而不是让用例因为 `SyntaxError` 红在"工具坏了"上（与 `ast.TryStar` 需要 3.11 同一族）。
    """
    import ast as _ast

    try:
        _ast.parse('match _x:\n    case _: pass\n')
        return True
    except SyntaxError:
        return False


def _call_names(node):
    """这次调用"叫的是谁"：`f(...)` ⇒ `f`，`a.b(...)` ⇒ `b`（叶子名，用来配内层 `def` 的名字）。"""
    import ast as _ast

    f = node.func
    if isinstance(f, _ast.Name):
        return {f.id}
    if isinstance(f, _ast.Attribute):
        return {f.attr}
    return set()


def _terminates(stmt):
    """这条语句一执行，同一套件里它后面的那些就再也走不到（第 60 轮 M-1 第⑦格）。"""
    import ast as _ast

    return isinstance(stmt, (_ast.Return, _ast.Raise, _ast.Break, _ast.Continue))


def _prune_suite(stmts):
    """一个语句套件剪到**第一条无条件转移**为止（含那一条本身）。

    与 `while 恒假`、`for 空容器` 同量级的"明摆着进不去"：`return`/`raise`/`break`/`continue`
    之后还写着一次解锁调用，运行时永远执行不到（探针实测上一版判"已接线"）。
    只剪**同一个套件** ⇒ `try:` 里 return 不会把 `finally:` 一起剪掉（那是另一个套件）。
    """
    out = []
    for s in stmts:
        out.append(s)
        if _terminates(s):
            break
    return out


def _kids(node):
    """子节点，但每个语句套件都先过一遍 `_prune_suite`。"""
    import ast as _ast

    out = []
    for _name, value in _ast.iter_fields(node):
        if isinstance(value, list):
            items = list(value)
            out.extend(_prune_suite(items)
                       if items and all(isinstance(x, _ast.stmt) for x in items) else items)
        elif isinstance(value, _ast.AST):
            out.append(value)
    return out


def _never_iterated(node, empty_names):
    """这个 `for` 语句 / 推导式的生成器是不是"明摆着一次都不迭代"（第 60 轮 M-1 第①②③格）。

    第 58/59 轮把这条规则给了 **`For` 语句**，但推导式（`ListComp`/`SetComp`/`DictComp`/
    `GeneratorExp`）里那个 `for` 是 `ast.comprehension` 节点、不是 `For` 语句 ⇒
    **同一语义的两种形状被同一把尺子两种待遇**（这正是本批 ③ 主账的第三个方向）。
    """
    import ast as _ast

    it = node.iter
    return _empty_container(it) or (isinstance(it, _ast.Name) and it.id in empty_names)


def _live_nodes(root, fn, dead, empty_names=frozenset()):
    """按"这条语句真会执行"剪过的遍历 —— 可达性只算**一跳**是会漏的（第 57 轮 M-1）。

    `dead` 是 `_dead_inner_defs` 交回的那份：`{'names': {…}, 'node_ids': {…}}`
    （名字 = 从不被叫的内层 `def` 与"属性位绑的 lambda"；节点 id = 从不被叫的 `lambda`）。

    剪枝规则与判"接没接"用的是**同一套**（两边各搓一份就是两把尺子）：
    - 恒假 `if` 的主体不进（`orelse` 照进）、恒假三目只走另一臂、`while 恒假` 的循环体不进；
    - **`for … in 空容器字面量` 的循环体不进**（第 58 轮 m-1：`for _ in ():` 与 `while False:` 同一种死法，
      而 `For` 上一版压根不在剪枝表里）；
      **第 59 轮 M-3 补同一族的第二半**：迭代的是"这个函数里唯一一次绑成空容器、此后没有任何累加"
      的那个**名字**（`d = []; for _ in d: release(...)`）也不进 —— 只认字面量等于换了个变量名就放行；
    - **推导式同理**（第 60 轮 M-1）：`[release(...) for _ in []]` 与
      `d = []; {release(...) for _ in d}` 一次都不叫 ⇒ 迭代对象本身照样求值， elt / 条件不进；
    - **`match` 的恒假 guard**那一档不进（`case _ if False:` 与 `if False:` 同一种死法）；
    - **无条件 `return`/`raise`/`break`/`continue` 之后的同一套件语句不进**；
    - `except` 那一支不进（出事了才走的路径不算正常接线）；
    - 名字落在 `dead['names']` 里的内层 `def`、节点落在 `dead['node_ids']` 里的 `lambda` 整棵不进。
    **边界**：`for` 与推导式只认"空字面量"与"唯一一次绑成空容器且无累加的名字"这两种**可证**不进入；
    `range(0)`、空生成器表达式这些要靠数据流才看得出来，这一版不猜（与 `_proves_sqlite` 同尺度）。
    """
    import ast as _ast

    names, node_ids = dead['names'], dead['node_ids']
    yield root
    if isinstance(root, _ast.If):
        arms = list(root.orelse) if _never_runs(root.test) \
            else _prune_suite(root.body) + list(root.orelse)
    elif isinstance(root, _ast.IfExp):
        arms = [root.orelse] if _never_runs(root.test) else [root.body, root.orelse]
    elif isinstance(root, _ast.While) and _never_runs(root.test):
        arms = list(root.orelse)                      # 循环体不进，`else` 照进
    elif isinstance(root, (_ast.For, _ast.AsyncFor)) and _never_iterated(root, empty_names):
        arms = list(root.orelse)                      # 一次都不进体，`else` 照进
    elif isinstance(root, (_ast.ListComp, _ast.SetComp, _ast.DictComp, _ast.GeneratorExp)):
        gens = root.generators
        # 只要有一档生成器"明摆着一次都不迭代"，整条推导式就一个值也不产出；
        # 但**迭代对象本身会求值**（`[f() for _ in g()]` 里的 `g()` 真会跑），所以那一半照进。
        arms = [g.iter for g in gens] if any(
            _never_iterated(g, empty_names) for g in gens) else list(_ast.iter_child_nodes(root))
    elif type(root).__name__ == 'Match':        # 3.10 起才有；按类名认，不在 3.9 上取属性
        arms = [root.subject] + [c for case in root.cases
                                 if not (case.guard is not None and _never_runs(case.guard))
                                 for c in _kids(case)]
    elif isinstance(root, (_ast.Try, _ast.TryStar)):
        arms = _prune_suite(root.body) + list(root.finalbody or [])
    elif isinstance(root, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda)):
        if isinstance(root, _ast.Lambda):
            if id(root) in node_ids:
                return                                # 绑在没人叫的名字上 ⇒ 这棵是死路
            arms = list(_ast.iter_child_nodes(root))
        else:
            if root is not fn and getattr(root, 'name', '') in names:
                return                                # 从不被叫的内层 def ⇒ 整棵是死路
            arms = _kids(root)
    else:
        arms = _kids(root)
    for arm in arms:
        yield from _live_nodes(arm, fn, dead, empty_names)


def _empty_container_names(fn, nodes):
    """这个函数里"唯一一次绑成空容器、此后没有任何累加"的那些名字（第 59 轮 M-3 第⑤格）。

    与 `_passes_the_new_dates` 的参数那一腿**共用** `_bound_value` / `_is_accumulated`，
    不再搓第二把尺子。绑过不止一次 ⇒ 不认（看不清就当它进得去，这条闸拦"明着不执行"）。
    """
    import ast as _ast

    out = set()
    for n in nodes:
        iters = [n.iter] if isinstance(n, _ast.For) else \
            [g.iter for g in n.generators] if isinstance(
                n, (_ast.ListComp, _ast.SetComp, _ast.DictComp, _ast.GeneratorExp)) else []
        for it in iters:                              # `For` 语句与推导式**同一把尺子**（第 60 轮 M-1）
            if not isinstance(it, _ast.Name):
                continue
            name = it.id
            bound = _bound_value(nodes, name)
            if bound is not None and _empty_container(bound) and not _is_accumulated(nodes, name):
                out.add(name)
    return out


def _const_dispatch_key(value):
    """字面量下标 ⇒ 这次派发"叫的是哪一个"：`hooks['r']` → `@r`、`hooks[0]` → `#0`。

    第 60 轮 M-1 的第⑥格：λ 塞进**列表**字面量、靠 `hooks[0]()` 叫，而上一版只给字典的
    字符串键开了一档（`@r`）⇒ 换个容器、换个下标类型就又是"看着接了"。
    布尔不当键用（`hooks[True]()` 与 `hooks[1]()` 在 Python 里是同一个位置，猜不得）。
    """
    import ast as _ast

    if isinstance(value, _ast.Constant) and not isinstance(value.value, bool) \
            and isinstance(value.value, (str, int)):
        return ('@' if isinstance(value.value, str) else '#') + str(value.value)
    return None


def _call_keys(node):
    """这次调用"叫的是谁"的**全部键**：函数名、属性叶子名、以及 `d['键']()` / `d[0]()` 那种
    容器派发（第 59 轮 M-3 的第④格：lambda 塞进字典字面量里、靠 `hooks['r']()` 叫，
    上一版 `_call_names` 对 `Subscript` 交回空集 ⇒ 那棵 λ 的体照常进遍历）。
    容器派发那一档用 `@键` / `#下标` 前缀，避免与真函数名撞车。
    """
    import ast as _ast

    out = set(_call_names(node))
    f = node.func
    if isinstance(f, _ast.Subscript):
        key = _const_dispatch_key(f.slice)
        if key:
            out.add(key)
    return out


def _dead_inner_defs(fn):
    """哪些内层 `def` / `lambda` 从这个函数的**活路径**上永远叫不到（迭代到不动点）。

    为什么要迭代：剪掉一个死 def 之后，只有它才会去叫的那个 def 也一起死了 ——
    上一版只算一层（`_called_names` 走 `ast.walk` 整棵树），于是
    "把解锁搬进 `def _release()`、唯一调用点压在 `if 1 == 0:` 里"照样判"已接线"。
    第 58 轮 m-1 再补两种同族死法（探针实测两种都判"已接线"）：
    ① **只有递归会叫自己的那个 def** —— 外面一个调用点都没有，运行时永远进不去，
       所以"这个 def 里有一次解锁"不等于接了线 ⇒ 认调用点时要**扣掉它自己体内那些**；
    ② `_r = lambda: release(...)` 绑在名字上而没人 `_r()` —— 与死 def 同一件事，
       以前 `Lambda` 的体照常进遍历。
    第 59 轮 M-3 补了两种位置（探针实测当时仍判"已接线"）：
    ③ `C.r = lambda: release(...)`（绑在**属性位**上，靠 `x.r()` 叫）；
    ④ `{'r': lambda: release(...)}`（塞进**字典字面量**，靠 `hooks['r']()` 叫）。
    **第 60 轮 M-1 把绑法这一族往 `_bound_value` 那一档对齐**（上一批刚给参数那一腿认下
    `AnnAssign`/`NamedExpr`，这一腿还停在三种绑法 ⇒ **同一把尺子两腿两种待遇**，本批主账的第四面）：
    ⑤ `r: callable = lambda: …`（带标注）；⑥ `(r := lambda: …)`（海象）；
    ⑦ `[lambda: …]` / `(lambda: …,)`（列表/元组字面量，靠 `hooks[0]()` 叫）；
    ⑧ `hooks['r'] = lambda: …` / `hooks[0] = lambda: …`（**下标位**上的赋值）。
    ⇒ 每种绑法各按自己"被叫得上"的键去对（名字看叶子名、`@键`/`#下标` 看容器派发），
    对不上就是死路；诚实写法（真去 `C.r()` / `hooks['r']()` / `hooks[0]()`）仍然算接上，见那些反面样品。
    **边界（不许说成"补全了"）**：下标是**变量或表达式**时（`hooks[i] = λ`、`hooks[key()]()`）
    认不出派发键 ⇒ 那一棵按"没人叫"处理不了，也不硬猜（与 `for` 只认空字面量同一尺度）；
    属性链上的 λ（`a.b.r = λ`）只认叶子名 `r`，与调用点那一腿同一个宽松度。
    """
    import ast as _ast

    inner = [d for d in _ast.walk(fn)
             if isinstance(d, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and d is not fn]
    lambdas = {}

    def _bind(key, lam):
        if key is not None:
            lambdas.setdefault(key, []).append(lam)      # 同一个键挂两棵 λ：都按这一个键判

    for n in _ast.walk(fn):
        if isinstance(n, (_ast.Assign, _ast.AnnAssign)) and isinstance(n.value, _ast.Lambda):
            for t in (n.targets if isinstance(n, _ast.Assign) else [n.target]):
                if isinstance(t, _ast.Name):
                    _bind(t.id, n.value)                 # `_r = λ` / ⑤ `r: callable = λ`
                elif isinstance(t, _ast.Attribute):
                    _bind(t.attr, n.value)               # ③ `C.r = λ`
                elif isinstance(t, _ast.Subscript):
                    _bind(_const_dispatch_key(t.slice), n.value)   # ⑧ 下标位上的赋值
        elif isinstance(n, _ast.NamedExpr) and isinstance(n.value, _ast.Lambda) \
                and isinstance(n.target, _ast.Name):
            _bind(n.target.id, n.value)                  # ⑥ `(r := λ)`
        elif isinstance(n, (_ast.List, _ast.Tuple, _ast.Set)):
            for i, elt in enumerate(n.elts):
                if isinstance(elt, _ast.Lambda):
                    _bind('#%d' % i, elt)                # ⑦ 列表/元组字面量里的 λ
        elif isinstance(n, _ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(v, _ast.Lambda):
                    _bind(_const_dispatch_key(k), v)     # ④ `{'r': λ}` / `{0: λ}`
    dead_names, dead_ids = set(), set()
    for _ in range(len(inner) + sum(len(v) for v in lambdas.values()) + 1):
        live_calls = [c for c in _live_nodes(fn, fn, {'names': dead_names, 'node_ids': dead_ids})
                      if isinstance(c, _ast.Call)]
        nxt_names, nxt_ids = set(), set()
        for d in inner:
            if not d.name:
                continue
            inside = {id(x) for x in _ast.walk(d)}    # 只有"它叫它自己"不算有人叫
            if not any(d.name in _call_names(c) and id(c) not in inside for c in live_calls):
                nxt_names.add(d.name)
        for name, lams in lambdas.items():
            # `@键` / `#下标` 那种是容器派发（`hooks['r']()` / `hooks[0]()`），
            # 其余按名字/属性叶子名对 ⇒ 两种都走 `_call_keys`
            if not any(name in _call_keys(c) for c in live_calls):
                for lam in lams:
                    nxt_ids.add(id(lam))
        if nxt_names == dead_names and nxt_ids == dead_ids:
            break
        dead_names, dead_ids = nxt_names, nxt_ids
    return {'names': dead_names, 'node_ids': dead_ids}


def _releases_live(fn):
    """函数体里有没有一次**真会执行**、且**参数也真递到**的 `release_holds_after_nav_commit`。

    "接线"要过的两道（第 55 轮 M19 + 第 56 轮 M-2/M-3 + 第 57 轮 M-1 + 第 58 轮 M-2 各教了一层）：
    ① 那次调用得在活路径上 —— 恒假分支、恒假三目那一支、`while 恒假`、空 `for` 的体、`except` 里、
      以及**从活路径叫不到的内层 `def`（一层或多层、含只认递归的）与 `lambda`**都不算；
    ② "新落的那几天"得真递进去（见 `_passes_the_new_dates`），而且**判断它递没递也走同一套活路径**
      —— 两半用两套尺子的话，`def _never(): d.append(1)` 这种诱饵就把整条判据买通了。
    """
    import ast as _ast

    dead = _dead_inner_defs(fn)
    # 两趟：第一趟先按"叫不到的 def / λ"剪出活节点，再从活节点里量出"哪些名字明摆着是空的"
    # （`d = []; for _ in d: …` 那一格），第二趟把这份名单喂进剪枝。反过来一次算不成，
    # 因为"这个名字是不是空容器"本身要看它有没有被活路径累加过。
    first = list(_live_nodes(fn, fn, dead))
    live = list(_live_nodes(fn, fn, dead, _empty_container_names(fn, first)))
    for node in live:
        if isinstance(node, _ast.Call) and \
                (getattr(node.func, 'attr', None) or getattr(node.func, 'id', '')) == \
                'release_holds_after_nav_commit' and _passes_the_new_dates(node, fn, live):
            return True
    return False


def _writes_nav_rows(fn):
    """这个函数往 `fund_history` 灌行吗？两种形状都要认（第 56 轮 m-3）。

    ① 点名构造 `FundHistory(...)`；
    ② **泛型建行**：整库导入那条路（`TABLE_SPECS` 里的 `spec.model(**cleaned)`）——
      它一个字都没写 `FundHistory`，只认①的那把尺子对它结构性失明，于是"每条写净值的路
      都要有处置"那句话当场是半句。
    边界说清：`src/services/base.py` 那种通用 CRUD 基类（`self.model(**obj_in)`）不在②里
    —— 它不引用 `TABLE_SPECS`，今天也没有任何 `fund_history` 的 CRUD 走它；
    哪天有人给它接上净值表，这条棘轮会因为②而必须重新看它。
    """
    import ast as _ast

    for n in _ast.walk(fn):
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) \
                and n.func.id == 'FundHistory':
            return True
    names = {getattr(n, 'id', '') for n in _ast.walk(fn) if isinstance(n, _ast.Name)}
    if 'TABLE_SPECS' not in names:
        return False
    return any((getattr(c.func, 'attr', '') or getattr(c.func, 'id', '')) in ('add', 'add_all')
               for c in _ast.walk(fn) if isinstance(c, _ast.Call))


def _nav_writers(trees):
    """`{(文件, 函数)}` → 这个函数体里有没有接 `release_holds_after_nav_commit`。

    判"它在写净值"用的是最硬的那个形状：函数体里构造了 `FundHistory(...)`，
    或走 `TABLE_SPECS` 的泛型建行（见 `_writes_nav_rows`）。
    """
    import ast as _ast

    out = {}
    for rel, tree, _src in trees:
        for fn in _ast.walk(tree):
            if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if not _writes_nav_rows(fn):
                continue
            out[(rel, fn.name)] = _releases_live(fn)
    return out


def test_the_nav_unlock_path_is_wired_into_every_nav_writer():
    """写了净值就要问"那把锁还需要吗" —— 接线与登记表都要有牙（第 55 轮 M-2）。

    判的不是"某处调用过一次"，而是**每一个往 `fund_history` 插行的函数**都得有处置：
    以后新开一条同步写入路、忘了接 ⇒ 那条路上的预测补到净值也永远解不开锁，
    而这类漏接在页面上完全看不出来（它表现为"什么都不发生"）。
    """
    found = _nav_writers(_src_trees())
    assert set(found) == set(NAV_WRITE_SITES), (
        '写净值的路与登记表对不上（多出来的一律没登记 ⇒ 新增一处不登记就红）：\n'
        '  实际: %s\n  登记: %s' % (sorted(map(':'.join, found)),
                                    sorted(map(':'.join, NAV_WRITE_SITES))))
    for key, disposition in NAV_WRITE_SITES.items():
        if disposition == 'releases':
            assert found[key] is True, \
                '%s:%s 登记成"接了解锁"，函数体里却没有那一次调用 ⇒ 撤谎' % key
        else:
            assert found[key] is False, \
                '%s:%s 给了不接锁的依据，代码却已经接上了 ⇒ 依据过期，改登记' % key
            assert len(disposition) >= 20, '%s 那条只登了个名字，没写依据' % (':'.join(key),)

    # 控制断言：现造一处"插了行没接锁"与一处"接了锁却没登记"，两样都必须被点名
    import ast as _ast
    bare = ('def update_fund_history(self, code, db, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n')
    wired = ('def update_fund_history(self, code, db, inserted):\n'
             '    db.add(FundHistory(fund_code=code))\n'
             '    release_holds_after_nav_commit(db, code, inserted, where="每日同步")\n')
    scanned = _nav_writers([('src/fund/new_writer.py', _ast.parse(bare), bare)])
    assert scanned == {('src/fund/new_writer.py', 'update_fund_history'): False}
    assert set(scanned) - set(NAV_WRITE_SITES), '扫描器看不见新造的那条写净值路 ⇒ 上面是空判'
    fixed = _nav_writers([('src/fund/fund_api.py', _ast.parse(wired), wired)])
    assert fixed[('src/fund/fund_api.py', 'update_fund_history')] is True, \
        '接了锁也认不出来 ⇒ 那条"登记成 releases 必须真接"的断言是反的'

    # 五种"看着接了、其实没接"的形状，一种都不许放过（第 55 轮 M19 + 第 56 轮 M-2/M-3）
    evasions = {
        '写进恒假分支': ('def f(self, db, code, inserted):\n'
                        '    db.add(FundHistory(fund_code=code))\n'
                        '    if inserted and False:\n'
                        '        release_holds_after_nav_commit(db, code, inserted)\n'),
        '写进 while False': ('def f(self, db, code, inserted):\n'
                             '    db.add(FundHistory(fund_code=code))\n'
                             '    while False:\n'
                             '        release_holds_after_nav_commit(db, code, inserted)\n'),
        '恒假比较（literal_eval 算不出来的那种）': (
            'def f(self, db, code, ins):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    if ins and 1 == 0:\n'
            '        release_holds_after_nav_commit(db, code, ins)\n'),
        '藏在从不被调的内层 def': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _maybe():\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'
            '    return 0\n'),
        '恒假三目那一支': ('def f(self, db, code, inserted):\n'
                          '    db.add(FundHistory(fund_code=code))\n'
                          '    x = release_holds_after_nav_commit(db, code, inserted) '
                          'if False else None\n'),
        '只写在 except 里': ('def f(self, db, code, inserted):\n'
                            '    db.add(FundHistory(fund_code=code))\n'
                            '    try:\n        pass\n'
                            '    except Exception:\n'
                            '        release_holds_after_nav_commit(db, code, inserted)\n'),
        '新落的那几天掏空': ('def f(self, db, code, inserted):\n'
                            '    db.add(FundHistory(fund_code=code))\n'
                            '    release_holds_after_nav_commit(db, code, [])\n'),
        '干脆不递那三天': ('def f(self, db, code, inserted):\n'
                          '    db.add(FundHistory(fund_code=code))\n'
                          '    release_holds_after_nav_commit(db, code)\n'),
        # 第 57 轮 M-1 的三格：可达性只算一跳时，这三种都判"已接线"
        '搬进内层 def、调用点压在恒假分支': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _release():\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'
            '    if 1 == 0:\n        _release()\n'),
        '搬进内层 def、只有 except 会叫它': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _release():\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'
            '    try:\n        pass\n'
            '    except Exception:\n        _release()\n'),
        '先赋一个空列表再递进去': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        '两层内层 def，最外面那个从不被叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _a():\n        def _b():\n'
            '            release_holds_after_nav_commit(db, code, inserted)\n'
            '        return _b\n    return 0\n'),
        '恒假的析取': ('def f(self, db, code, inserted):\n'
                      '    db.add(FundHistory(fund_code=code))\n'
                      '    if False or False:\n'
                      '        release_holds_after_nav_commit(db, code, inserted)\n'),
        # 第 58 轮 m-1 的三格：这三种"从不被叫"上一版全判"已接线"
        'for 一个空容器字面量': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    for _ in ():\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'),
        'lambda 绑在名字上而没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    _r = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return 0\n'),
        '只有递归会叫自己的 def': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _r(n):\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'
            '        return _r(n - 1)\n'),
        # 第 58 轮 M-2：这一格与上面"先赋一个空列表再递进去"只差**一行诱饵** ——
        # 上一版"有没有累加"那半走整棵树、与"调用点可达"那半用的是两套尺子，于是这一格判"接了"。
        '空列表 + 诱饵累加藏在死 def 里': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    def _never():\n        d.append(1)\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        # 第 59 轮 M-3 的五格：掏空的东西**换个绑法**、λ **换个位置**、空容器**换个名字迭代**，
        # 上一版四种判"接了"（探针实测），第五格是"迭代那个空名字本身"。
        '带类型标注的空列表再递进去': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d: list = []\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        '海象绑的空列表再递进去': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    (d := [])\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        'lambda 绑在属性位而没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    class C:\n        pass\n'
            '    C.r = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return 0\n'),
        'lambda 塞进字典字面量而没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            "    hooks = {'r': lambda: release_holds_after_nav_commit(db, code, inserted)}\n"
            '    return 0\n'),
        '迭代那个本身为空的名字': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    for _x in d:\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'),
        # 第 60 轮 M-1 的八格：前两批把"不可达"给了 **`For` 语句**，可推导式里那个 `for` 是
        # `ast.comprehension` 节点不是 `For` ⇒ 同一语义换个形状就全部失效；λ 那一腿同样落后于
        # `_bound_value`（换绑法隐身）；而"无条件 return/raise 之后那一行"压根不在剪枝表里。
        '推导式 over 空字面量': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    [_x for _x in [] if release_holds_after_nav_commit(db, code, inserted)]\n'),
        '推导式 over 可证为空的名字': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    [_x for _x in d if release_holds_after_nav_commit(db, code, inserted)]\n'),
        '集合推导式同族': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    {release_holds_after_nav_commit(db, code, inserted) for _ in ()}\n'),
        '生成器表达式同族': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    list(release_holds_after_nav_commit(db, code, inserted) for _ in [])\n'),
        '带标注绑的 lambda 没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    r: callable = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return 0\n'),
        '海象绑的 lambda 没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    (r := lambda: release_holds_after_nav_commit(db, code, inserted))\n'
            '    return 0\n'),
        'lambda 塞进列表字面量而没人叫': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    hooks = [lambda: release_holds_after_nav_commit(db, code, inserted)]\n'
            '    return 0\n'),
        'return 之后的那一行': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    return 0\n'
            '    release_holds_after_nav_commit(db, code, inserted)\n'),
        'raise 之后的那一行': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    raise ValueError()\n'
            '    release_holds_after_nav_commit(db, code, inserted)\n'),
        'match 的恒假 guard': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    match code:\n'
            '        case _ if False:\n'
            '            release_holds_after_nav_commit(db, code, inserted)\n'),
    }
    if not _match_supported():
        evasions.pop('match 的恒假 guard')          # 这台解释器没有 match 语法，喂不进去
    for label, src in evasions.items():
        assert _nav_writers([('src/fund/fund_api.py', _ast.parse(src), src)]) == {
            ('src/fund/fund_api.py', 'f'): False}, \
            '"%s" 这一种仍然被判成"已接解锁"⇒ 把同步器改成永不解锁没人发现' % label
    # 对照：正确形状与"确实被调到的内层 def"必须算接上（不许把上面修成一道墙）
    honest = ('def f(self, db, code, inserted):\n'
              '    db.add(FundHistory(fund_code=code))\n'
              '    def _maybe():\n'
              '        release_holds_after_nav_commit(db, code, inserted)\n'
              '    return _maybe()\n')
    assert _nav_writers([('src/fund/fund_api.py', _ast.parse(honest), honest)]) == {
        ('src/fund/fund_api.py', 'f'): True}, '内层 def 被真调用了还不算接线 ⇒ 这道闸过宽'
    # 反面对照（这一格是真代码的形状）：`inserted = []` 之后一路 `.append(...)` 再递进去
    # 必须算"递到了" —— 回溯只拦"明着掏空"，不许把正常累加打成没接（过宽的闸活不过一轮）
    accumulate = ('def f(self, db, code):\n'
                  '    inserted = []\n'
                  '    db.add(FundHistory(fund_code=code))\n'
                  '    inserted.append(code)\n'
                  '    release_holds_after_nav_commit(db, code, inserted)\n')
    assert _nav_writers([('src/fund/fund_api.py', _ast.parse(accumulate), accumulate)]) == {
        ('src/fund/fund_api.py', 'f'): True}, '累加后递出去被判成"没递" ⇒ 这条闸过宽，会把自己关掉'
    # 上面 m-1 那三格的**反面对照**：三种形状各自"真被叫到"时必须仍算接上 ——
    # 修剪"不可达"不等于修剪"这一族写法"，否则下一轮正常的延迟解锁会被这道闸集体判死。
    honest_live = {
        'lambda 真的被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    _r = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return _r()\n'),
        'for 一个非空字面量': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    for _c in (code,):\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'),
        '自递归的 def 同时有外部调用点': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    def _r(n):\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'
            '        return _r(n - 1)\n'
            '    return _r(3)\n'),
        '累加发生在活 def 里（与上面那格诱饵只差"这个 def 有人叫"）': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    def _fill():\n        d.append(code)\n'
            '    _fill()\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        # 第 59 轮 M-3 那五格的**反面样品**：换了绑法/换了位置的 λ 只要"真被叫到"、
        # 空容器只要"真累加过"，就必须仍算接上 ⇒ 修的是"不可达"，不是"这一族写法"。
        '带标注且真累加': (
            'def f(self, db, code):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d: list = []\n'
            '    d.append(code)\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        '海象绑且真累加': (
            'def f(self, db, code):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    (d := [])\n'
            '    d.append(code)\n'
            '    release_holds_after_nav_commit(db, code, d)\n'),
        '属性位上的 lambda 真的被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    class C:\n        pass\n'
            '    C.r = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return C.r()\n'),
        '字典里的 lambda 真的被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            "    hooks = {'r': lambda: release_holds_after_nav_commit(db, code, inserted)}\n"
            "    return hooks['r']()\n"),
        'for 迭代的是"先放过东西"的那个名字': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = [code]\n'
            '    for _x in d:\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'),
        # 第 60 轮 M-1 那八格的**反面样品**：推导式只要迭代对象**进得去**、换绑法的 λ 只要
        # **真被叫到**、`match` 只要 guard 不恒假，就必须仍算接上 ⇒ 修的是"不可达"这一族，
        # 不是"这一族语法"。少任何一格，下一轮正常的延迟解锁会被这道闸集体判死。
        '推导式 over 非空字面量': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    [release_holds_after_nav_commit(db, code, inserted) for _ in (code,)]\n'),
        '推导式 over 真累加过的名字': (
            'def f(self, db, code):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    d = []\n'
            '    d.append(code)\n'
            '    [release_holds_after_nav_commit(db, code, d) for _ in d]\n'),
        '带标注绑的 lambda 真被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    r: callable = lambda: release_holds_after_nav_commit(db, code, inserted)\n'
            '    return r()\n'),
        '海象绑的 lambda 真被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    (r := lambda: release_holds_after_nav_commit(db, code, inserted))\n'
            '    return r()\n'),
        '列表里的 lambda 按下标真被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    hooks = [lambda: release_holds_after_nav_commit(db, code, inserted)]\n'
            '    return hooks[0]()\n'),
        '下标位上绑的 lambda 真被叫了': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            "    hooks = {}\n"
            "    hooks['r'] = lambda: release_holds_after_nav_commit(db, code, inserted)\n"
            "    return hooks['r']()\n"),
        'return 之前那一行照常算（剪的是它后面，不是整个函数）': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    release_holds_after_nav_commit(db, code, inserted)\n'
            '    return 0\n'),
        'try 里 return 不把自己的 finally 剪掉': (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    try:\n        return 0\n'
            '    finally:\n'
            '        release_holds_after_nav_commit(db, code, inserted)\n'),
    }
    if _match_supported():
        honest_live['match 的 guard 不恒假'] = (
            'def f(self, db, code, inserted):\n'
            '    db.add(FundHistory(fund_code=code))\n'
            '    match code:\n'
            '        case _ if 1 == 1:\n'
            '            release_holds_after_nav_commit(db, code, inserted)\n')
    for label, src in honest_live.items():
        assert _nav_writers([('src/fund/fund_api.py', _ast.parse(src), src)]) == {
            ('src/fund/fund_api.py', 'f'): True}, \
            '"%s" 被判成"没接解锁"⇒ 这条闸过宽，会把真接线的正常写法一起挡掉' % label


def test_the_nav_lookback_has_one_home_for_both_questions(monkeypatch):
    """"同步往回拉多少天"这件事只许有一处实现（第 54 轮 A-5 的加固）。

    重问间隔必须**跟着 `config` 那个键动**，不是"今天恰好等于它 +1"：
    第一版我只比了 `endpoint_days == NAV_HISTORY_LOOKBACK_DAYS + 1`，而默认值 30 ⇒
    变异体把间隔写死成 31 也照样绿（第 54 轮变异 M15 打绿，就是这么暴露的）。
    所以这里把键改成一个不是 30 的数再问一次。
    """
    import ast
    from pathlib import Path as _P
    from src.core.config import config

    root = _P(__file__).resolve().parents[2]
    monkeypatch.setattr(config, 'NAV_HISTORY_LOOKBACK_DAYS', 47, raising=False)
    assert unverifiable_retry_days('same_nav_endpoint') == 48, \
        '间隔不跟着回补范围动 ⇒ 它其实是第二个写死的数，两边会各漂各的'
    # 有凭据那一档不许被这个键带着走（它跟的是凭据 TTL）
    assert unverifiable_retry_days('no_source_history') != 48

    api = (root / 'src' / 'fund' / 'fund_api.py').read_text(encoding='utf-8')
    tree = ast.parse(api)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == 'get_fund_history')
    defaults = [ast.unparse(d) for d in fn.args.defaults]
    assert not any('NAV_HISTORY_LOOKBACK_DAYS' in d for d in defaults), \
        '取历史的窗口又回到**导入时**算死的签名默认值 ⇒ 改键不动它，而这句话有了第二个出处'\
        '（第 56 轮 m-5：上一版这条判据正向钉的就是这个形状）'
    cfg = (root / 'src' / 'core' / 'config.py').read_text(encoding='utf-8')
    assert cfg.count('NAV_HISTORY_LOOKBACK_DAYS =') == 1, '这个数在 config 里立了两处'
    # 取数只能从 `nav_backfill_days()` 出来：**整个 `src/`** 里再有人自己读那个键就是第二把尺子
    # （第 56 轮 m-5：上一版只扫 `prediction_lifecycle.py` 一个文件，`fund_api.py` 那两处看不见）
    readers = set()
    for path in sorted((root / 'src').rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if rel.endswith('core/config.py'):
            continue                       # 定义处自己不算"读"
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Attribute) and \
                    node.attr == 'NAV_HISTORY_LOOKBACK_DAYS':
                readers.add(rel)
    assert readers == {'src/services/prediction_lifecycle.py'}, \
        '读那个键的文件不止 `prediction_lifecycle`（实测 %s）⇒ "同步往回拉多少天"又有了第二个出处' \
        % sorted(readers)
    assert 'return nav_backfill_days() + 1' in (
        root / 'src' / 'services' / 'prediction_lifecycle.py').read_text(encoding='utf-8'), \
        '端点档不再走那唯一的出处'


@pytest.mark.parametrize('period_days', [0, 1, 5, 6, 11, 30, 45])
def test_the_creation_schedule_never_writes_a_date_after_the_target(period_days):
    """不变式：两个创建出口夹出来的 `next_verify_date` 不得晚于目标日。

    晚于目标日的排期会被 `is_held_unverifiable` 读成"数据源给不出这段"⇒ 冤枉一条
    本来可验的预测。这条用例就是在防那一天。
    """
    from src.services.prediction_service import PredictionService
    from src.analyzer.llm_analyzer import LLMAnalyzer

    pred_date = TODAY - timedelta(days=9)
    target = pred_date + timedelta(days=period_days)
    for value in (PredictionService._calculate_next_verify_date(pred_date, target),
                  LLMAnalyzer.calculate_next_verify_date(LLMAnalyzer.__new__(LLMAnalyzer),
                                                         pred_date, target)):
        assert value <= target, f'排期 {value} 越过了目标日 {target}：结构性归因会开始冤枉人'


def test_the_list_route_splits_the_two_counts_and_they_add_up(test_db, monkeypatch):
    """路由级（不是私有方法级）：`due` + `unverifiable` 必须等于全部到期未判。

    只看服务层的私有方法，绿灯替不了"页面上真有这个按钮、接口真给这个数"。
    """
    from src.api.routes.predictions import get_predictions

    held = _seed(test_db, target=TODAY - timedelta(days=4))
    plain = _seed(test_db, target=TODAY - timedelta(days=2), fund_code='HOLD02')
    soon = _seed(test_db, target=TODAY + timedelta(days=9), fund_code='HOLD03')
    lc.apply_unverifiable_hold(held, as_of=TODAY)
    test_db.commit()

    def facets():
        return get_predictions(db=test_db, exclude_flat=True)['meta']['facets']

    def ids(lifecycle):
        page = get_predictions(db=test_db, lifecycle=lifecycle, exclude_flat=True)
        return {row['id'] for row in page['data']}

    assert facets()['due'] == 1 and facets()['unverifiable'] == 1
    assert facets()['due'] + facets()['unverifiable'] == 2      # 到期未判总数没被吞
    assert ids('due') == {plain.id}
    assert ids('unverifiable') == {held.id}
    assert facets()['upcoming'] == 1 and soon.id not in ids('due')
    # 两档互斥：同一条预测不许既"今天可验"又"结构性不可验"
    assert not (ids('due') & ids('unverifiable'))


def test_the_batch_receipt_says_why_a_row_was_not_verified(test_db, monkeypatch):
    """批量验证的回执必须点名被压住的那些，而不是让它们从"到期不验证"里消失。

    `verify_all_pending` 早就把 `filter_unverifiable` 接进了 `skipped`，
    上一批 `filter_unverifiable` 恒为空 ⇒ 那条接线从来没被走过。这条用例是它第一次有人测。
    """
    from src.services.prediction_verify_service import PredictionVerifyService

    held = _seed(test_db, target=TODAY - timedelta(days=4), fund_code='HOLD09')
    lc.apply_unverifiable_hold(held, as_of=TODAY)
    test_db.commit()

    result = PredictionVerifyService(test_db).verify_all_pending(as_of=TODAY)

    assert result['success'] is True
    reasons = {s['prediction_id']: s['reason'] for s in result['data']['skipped']}
    assert held.id in reasons, '被压住的预测既不在队列里、也不在回执里 = 没人知道它去哪了'
    assert '结构性不可验' in reasons[held.id]
    assert held.id not in {r['prediction_id'] for r in result['data']['results']}


def test_a_live_fund_with_a_history_gap_is_never_closed(test_db, monkeypatch):
    """反面对照（最贵的一格）：库里这只基金**最近还在发净值**、只是缺中间那段 ⇒ 只锁不关。

    少了这道对照，"我们自己几天没同步"就会被读成"产品停更"，
    于是批量验证会替老板把他本可以验证的预测关掉。
    """
    from src.models.database import FundHistory

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='LIVE01')
    # 窗口**之前**也要有一行，才真的是"缺中间那段"（历史洞）。只留窗口之后那一行的话，
    # 形状就变成"库里首笔晚于窗口终点" = 第 53 轮 B-1 那档永久判不出，会被新证据关掉。
    test_db.add(FundHistory(fund_code='LIVE01', nav_date=TODAY - timedelta(days=40),
                            nav=1.31))
    test_db.add(FundHistory(fund_code='LIVE01', nav_date=TODAY - timedelta(days=2),
                            nav=1.5))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False
    assert p.is_deleted is False, '最近还在发净值的产品不许被当成停更关掉'
    assert p.next_verify_date == TODAY + timedelta(days=unverifiable_retry_days())


def test_the_second_structural_answer_on_a_dead_target_closes_the_row(test_db, monkeypatch):
    """第二次答"没有" + 库里最后一条净值早于窗口 ⇒ 收进回收站：原因写清、可恢复、不写结论。"""
    from src.models.database import FundHistory, PredictionChangeLog

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='DEAD01')
    test_db.add(FundHistory(fund_code='DEAD01', nav_date=TODAY - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is True and result['held_until'] is None
    assert p.is_deleted is True and p.deleted_by == 'system'
    assert '回收站' in p.delete_reason and '不计入准确率' in p.delete_reason
    assert p.is_correct is None, '关闭不是判错：一个结论都不许写'
    # 留痕：台账里能翻到这一笔是谁动的、动之前长什么样
    logs = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == p.id).all()
    assert logs and logs[-1].source == 'system'
    # 活跃面彻底干净：既不在到期队列，也不在"结构性不可验"那一档
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}
    assert p.id not in {x.id for x in filter_unverifiable(test_db, as_of=TODAY)}


def test_a_refused_close_still_puts_the_lock_back(test_db, monkeypatch):
    """该关但**关不成**（归档咽喉拒了）⇒ 必须退回上锁，不许"既不锁也不报"（第 52 轮 A-6）。

    旧写法是一条 `else`：只有"判不出来关"那一支才上锁。于是咽喉拒收时（行已不在活跃面、
    并发被别的批次拿走等）这一轮什么都不写 ⇒ 它明天仍在到期队列里，每一批重问一次、
    每一次又试关一次，而回执上既没有 `held_until` 也没有 `closed_as_unverifiable`，
    页面上看不出这一行今天被问过。
    """
    from src.models.database import FundHistory
    from src.services.prediction_service import PredictionService

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='REF01')
    test_db.add(FundHistory(fund_code='REF01', nav_date=TODAY - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    monkeypatch.setattr(PredictionService, 'close_as_unverifiable',
                        lambda self, pid, note: False, raising=True)
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    held = TODAY + timedelta(days=unverifiable_retry_days())
    assert result['closed_as_unverifiable'] is False
    assert result['held_until'] == held.isoformat(), '关不成又不上锁 ⇒ 这批行每天都白跑一遍'
    test_db.refresh(p)
    assert p.is_deleted is False and p.next_verify_date == held
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_a_window_entirely_before_the_first_nav_row_closes_the_row(test_db, monkeypatch):
    """第 53 轮 B-1 的 BLOCKER：窗口**整段早于**库里首笔净值 ⇒ 这是第二种"永久"，得单独认。

    镜像实测 7 行到期未判里有这一族（`515440` 五条：窗口 07-23~08-20，库里首笔 09-02）：
    末条净值活得好好地在窗口之后 ⇒ `nav_cannot_cover_window`（末条早于起点）恒为 False ⇒
    关不掉，而重问日每三天把它们弹回「待验证到期」再踢出去一次 ⇒ 老板那句"打开后我不想看到
    过期没验的预测"每个周期破一次。源端已经答过"这段没有"（`no_source_history` 的前提），
    而库里在窗口终点**之后**有行 ⇒ 不是我们同步坏了，是那几天它真的还没有净值。
    """
    from src.models.database import FundHistory

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='NEW01')
    test_db.add(FundHistory(fund_code='NEW01', nav_date=TODAY - timedelta(days=2), nav=1.0))
    test_db.add(FundHistory(fund_code='NEW01', nav_date=TODAY - timedelta(days=1), nav=1.01))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is True, '第二种永久形状不许没人认'
    assert p.is_deleted is True and p.deleted_by == 'system' and p.is_correct is None
    assert '还没有开始发净值' in p.delete_reason, '这句话不能说成"停更"（方向相反）'
    assert (TODAY - timedelta(days=2)).isoformat() in p.delete_reason


def test_the_two_permanent_shapes_are_told_apart():
    """`stale_close_evidence` 是一张表，不是一句猜：停更 / 还没开始 / 洞 / 说不清 各一格。"""
    d = lc.stale_close_evidence
    gone = TODAY - timedelta(days=400)
    assert d(local_latest_nav=gone, window_start=TODAY - timedelta(days=10)) == 'stopped'
    assert d(local_first_nav=TODAY - timedelta(days=2),
             window_end=TODAY - timedelta(days=3)) == 'pre_inception'
    # 库里两头都有行、只是中间缺一段 ⇒ 洞，补拉能填，两种永久都不许认
    assert d(local_latest_nav=TODAY - timedelta(days=2), window_start=TODAY - timedelta(days=10),
             local_first_nav=TODAY - timedelta(days=40), window_end=TODAY - timedelta(days=3)) is None
    # 日期说不清一律不敢下结论
    assert d(local_latest_nav=None, window_start=None,
             local_first_nav=None, window_end=None) is None
    assert d(local_first_nav=TODAY - timedelta(days=2), window_end=None) is None


def test_a_retag_sends_the_old_targets_lock_back_below_the_target(test_db):
    """改标必须把压在**旧标的**上的重问锁退回目标日之前（第 53 轮 B-5）。

    不清它：`was_locked_previously` 只看"那根日期晚于自己的目标日"，而锁恰好写在晚于的位置上
    ⇒ 换了标的之后的第一次结构性结论会被读成"第二次"，条件齐了就当场进回收站。
    """
    from src.fund.fund_sync_manager import FundSyncManager
    from src.models.database import FundHistory

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY + timedelta(days=20), fund_code='RTG01')
    # 新标的给得出这段窗口的证据 ⇒ 改标本身该放行（不然这条用例验不到"清锁"那一半）
    test_db.add_all([FundHistory(fund_code='RTG02', nav_date=TODAY - timedelta(days=10), nav=1.0),
                     FundHistory(fund_code='RTG02', nav_date=TODAY - timedelta(days=3), nav=1.1)])
    test_db.commit()

    FundSyncManager.retag_prediction(test_db, p, 'RTG02', '改标后的基金', source='unit_test')
    # `retag_prediction` 自己不提交（调用方负责），不 commit 就 refresh 等于把这次改标回滚掉
    test_db.commit()
    test_db.refresh(p)
    assert p.fund_code == 'RTG02'
    assert p.next_verify_date <= p.target_date, (
        '旧标的的那把锁还挂着 ⇒ 新标的的第一问会被当成第二次，可能一跳进回收站')


def test_only_the_hold_writes_a_date_after_the_target(tmp_path):
    """`next_verify_date` 的写侧站点必须闭合（第 52 轮 A-7）—— 这条不变式是整个"问过两次"的根基。

    判据分两半，缺一个都不算钉住：
    ① **本条**：全仓能写这一列的代码点逐字等于登记名单，加一处不登记就红、登记了却没写也红；
    ② 名单里除 `apply_unverifiable_hold` 之外，每一站写进去的日期都被夹在目标日之前 ——
      创建侧由 `test_the_creation_schedule_never_writes_a_date_after_the_target` 钉，
      改标侧由 `test_a_retag_sends_the_old_targets_lock_back_below_the_target` 钉。
    边界说清楚：本条只数"谁写了这一列"，不静态证明"写进去的是哪天"（那一半是行为判据的活）。
    """
    import ast
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    registered = {
        # 唯一会写"晚于目标日"的那一天的一道（结构性结论的重问日）
        ('src/services/prediction_lifecycle.py', 'apply_unverifiable_hold'),
        ('src/services/prediction_lifecycle.py', 'release_unverifiable_hold'),
        # 改标：把旧标的的锁退回目标日（行为判据在上一条）
        ('src/fund/fund_sync_manager.py', 'retag_prediction'),
        ('src/services/prediction_service.py', 'update_prediction_fields'),
        # 创建：构造行时排期，两个出口都被 min(..., target) 夹住
        ('src/services/post_analysis_service.py', '_build_prediction'),
        ('src/utils/concurrent_analyzer.py', 'analyze_posts_concurrent'),
        ('src/utils/concurrent_analyzer.py', 'analyze_single_post'),
        # 一次性修复工具（不入库的路径不在这儿）
        ('scripts/repair_replay_side_effects.py', 'main'),
    }

    def site_names(node):
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and t.attr == 'next_verify_date':
                        out.add('assign')
            if isinstance(n, ast.Call):
                fn = getattr(n.func, 'id', '') or getattr(n.func, 'attr', '')
                if (fn == 'setattr' and len(n.args) > 1
                        and isinstance(n.args[1], ast.Constant)
                        and n.args[1].value == 'next_verify_date'):
                    out.add('setattr')
            if isinstance(n, ast.keyword) and n.arg == 'next_verify_date':
                out.add('kwarg')
        return out

    found = set()
    for base in ('src', 'scripts'):
        for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root).replace(os.sep, '/')
                tree = ast.parse(open(path, encoding='utf-8').read())
                for fn in [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                    if site_names(fn):
                        found.add((rel, fn.name))
    assert found == registered, (
        '写 next_verify_date 的站点与登记名单对不上：多出 %s / 少登记 %s ⇒ '
        '"晚于目标日的那一天只可能由重问锁写下"这句判据的地基开始漏' %
        (sorted(found - registered), sorted(registered - found)))

    # 反空判：现造一处（async 也算）必须被点名
    fake = ast.parse("async def elsewhere(p):\n"
                     "    p.next_verify_date = None\n")
    hit = any(site_names(n) for n in ast.walk(fake)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    assert hit, '扫描器自己恒空 ⇒ 这条判据等于没判（第 53 轮 A-7：async def 曾经是盲区）'


def test_a_degenerate_endpoint_holds_the_row_out_of_the_due_queue(test_db, monkeypatch):
    """目标日落在休市日（起点与终点是同一条净值）也是**问过之后**的结构性结论。

    2026-09-27 在镜像上量到的形状：`512680 / 512170` 两条 `07-10 → 07-11`（周六），
    验证器每次都答 `same_nav_endpoint` ⇒ 任务 #8 那把锁当时只认 `no_source_history`，
    这两条就永远坐在「待验证到期」里，每次点按钮都白跑它们一遍。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3), fund_code='END01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint',
        'message': '目标日及之前只有那一条净值，起点与终点是同一条'})

    result = svc.verify_prediction(p.id)

    assert result['success'] is False
    held = TODAY + timedelta(days=unverifiable_retry_days('same_nav_endpoint'))
    assert result['held_until'] == held.isoformat(), '退化端点没被锁 ⇒ 到期队列里永远有它'
    test_db.refresh(p)
    assert p.next_verify_date == held and p.is_correct is None
    assert classify(p, as_of=TODAY) == UNVERIFIABLE
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_a_first_ask_never_closes_even_when_the_fund_has_stopped(test_db, monkeypatch):
    """第一次问出来只许上锁 —— 排期日期在**过去**不等于"上一轮是结构性结论"。

    2026-09-27 镜像实测抓到的：`should_close_as_stale_target` 的第①条以前只看
    "这根日期过了没有"，而到期队列里的行那根日期本来就是创建时排出来的（必然 ≤ 目标日 ≤ 今天）
    ⇒ 任何一条第一次判出结构性结论、且第②条也成立，就会被**直接**收进回收站，
    "问过两次"那句承诺当场为假。真正的凭据是**这根日期落在目标日之后**：
    创建排期被 `test_the_creation_schedule_never_writes_a_date_after_the_target` 夹在目标日之前，
    所以只有上一轮的锁会写在目标日之后。
    """
    from src.models.database import FundHistory

    target = TODAY - timedelta(days=3)
    p = _seed(test_db, target=target, next_verify=target, fund_code='DEAD01')
    test_db.add(FundHistory(fund_code='DEAD01', nav_date=target - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False, '第一次问出来就关 ⇒ "问过两次"是句空话'
    assert p.is_deleted is False
    assert p.next_verify_date == TODAY + timedelta(days=unverifiable_retry_days())


def test_a_degenerate_endpoint_is_never_closed_even_on_the_second_ask(test_db, monkeypatch):
    """退化端点**只锁不关**，第二次问出来也不关（第 52 轮 A-1 逼出来的边界）。

    我上一版给它配了"库里目标日之后已有净值 ⇒ 那天不是交易日"这条关闭证据，被实测驳回：
    同一句证据还有第二种来路 —— **这只标的自己有数据洞**，补拉就能填。库里分得开吗？分不开
    （2026-09-27 镜像逐日数过：真休市的 2026-07-11 周六全库 1 行（货币基金照发），
    交易日的 2026-09-08 有 202 行）。关错的代价是"永久消失 + 一句假原因"，
    锁的代价只是"隔几天再问一次" ⇒ 宁可一直问。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='END02')
    # 把**另外**两条关闭证据都摆足（库里末条净值远早于窗口起点 + 上一轮的锁已过点）：
    # 少了这两行，这条用例拒的是"净值覆盖不了窗口"，与"退化端点能不能关"这件事无关
    # —— 第 52 轮的变异 M2（把 same_nav_endpoint 塞进可关名单）在它上面打绿，就是这么暴露的。
    from src.models.database import FundHistory
    test_db.add(FundHistory(fund_code='END02', nav_date=TODAY - timedelta(days=400),
                             nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False, '这条关不得：证据分不清休市与数据洞'
    assert p.is_deleted is False
    assert result['held_until'] == (TODAY + timedelta(
        days=unverifiable_retry_days('same_nav_endpoint'))).isoformat()


def test_the_structural_reasons_are_all_dispositioned(test_db):
    """「结构性」名单与"能不能关"两张名单必须拼得上、不重叠，加一档不登记就红。

    反空判（B-1 那一族）：把第三档塞进 `STRUCTURAL_VERDICT_REASONS` 而不登记处置，
    这条必须立刻判不符 ⇒ 它钉的是"以后加档的人别忘了回答要不要关"。
    """
    structural = set(lc.STRUCTURAL_VERDICT_REASONS)
    closable = set(lc.CLOSABLE_VERDICT_REASONS)
    lock_only = set(lc.LOCK_ONLY_VERDICT_REASONS)

    assert closable & lock_only == set(), '一档不许同时"能关"又"只锁"'
    assert structural == closable | lock_only, (
        '新增结构性结论要登记处置方式（能关 / 只锁），别让它掉到 if 的默认分支上：%s'
        % (structural ^ (closable | lock_only)))
    # 关闭那条路只认登记过的档；其余一律 False（第一次问出来之外也不许顺手关）
    for reason in lock_only:
        assert lc.should_close_as_stale_target(
            verdict_reason=reason, previous_hold=TODAY - timedelta(days=1),
            target_date=TODAY - timedelta(days=3),
            local_latest_nav=TODAY - timedelta(days=400),
            window_start=TODAY - timedelta(days=3), today=TODAY) is False

    tampered = structural | {'a_reason_nobody_disposed_of'}
    assert tampered != closable | lock_only, '空判对照：塞一档进来必须被上面那条发现'


def test_the_verifier_s_whole_vocabulary_is_dispositioned(test_db):
    """三张名单不能只跟彼此对表 —— 必须跟**验证器真会答的那些 reason**对表（第 53 轮 A-8 = B-8）。

    上面那条 `test_the_structural_reasons_are_all_dispositioned` 只问"名单拼得上吗"，
    它看不见两件本批真的踩到的事：
    ① 名单里挂着一档验证器**从来不会答**的理由 ⇒ 那条锁/关的代码是死路（#112 就是这么藏着的）；
    ② 验证器新答一档、名单里没人认领 ⇒ 它掉进"既不锁也不关"的默认分支，
      于是"到期却永远判不出来"又回到页面上（老板点名要清零的那一档）。
    所以这里从 `_check_fund_data_availability` 的源码里**现读**词表，再对三张名单。
    """
    import ast as _ast
    import inspect
    import os

    from src.services.prediction_verify_service import PredictionVerifyService

    src_path = inspect.getsourcefile(PredictionVerifyService)
    assert os.path.abspath(src_path).replace(os.sep, '/').endswith(
        'src/services/prediction_verify_service.py'), src_path
    fn = None
    for node in _ast.walk(_ast.parse(open(src_path, encoding='utf-8').read())):
        if (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == '_check_fund_data_availability'):
            fn = node
            break
    assert fn is not None, '找不到 `_check_fund_data_availability` ⇒ 词表扫描恒空，这条判据没判'

    answers = set()          # 验证器会答出口的全部 reason 字面量
    for node in _ast.walk(fn):
        if isinstance(node, _ast.keyword) and node.arg == 'reason':
            if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                answers.add(node.value.value)
        elif isinstance(node, _ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, _ast.Constant) and k.value == 'reason'
                        and isinstance(v, _ast.Constant) and isinstance(v.value, str)):
                    answers.add(v.value)
    assert answers, '词表扫出来是空的 ⇒ 这条判据结构上不可能红'

    structural = set(lc.STRUCTURAL_VERDICT_REASONS)
    # 会自愈的失败：明天净值到了就判得出来 ⇒ 绝不配锁（锁了等于亲手藏一条可验的预测）
    self_healing = {'insufficient_points', 'no_history', 'end_nav_too_old',
                    'endpoint_lag_unproven', 'waiting_target_nav'}
    # "数据够了"那一侧的三种说明：本来就不是失败
    enough = {'exact_target', 'weekend_previous', 'waited_previous'}

    assert answers == structural | self_healing | enough, (
        '验证器的词表与三档名单对不上 ⇒ 要么新增一档没人处置（会掉进默认分支、'
        '永远回到到期队列），要么名单里挂着一档它从不答的（那条锁/关是死路）：%s'
        % (answers ^ (structural | self_healing | enough)))
    assert not (structural & self_healing) and not (structural & enough), \
        '一档 reason 不许既"结构性"又"会自愈"'

    # 名单不只是标签：行为必须跟着分档走（否则对表通过、代码仍然锁错）
    full_close = dict(previous_hold=TODAY - timedelta(days=1),
                      target_date=TODAY - timedelta(days=3),
                      local_latest_nav=TODAY - timedelta(days=400),
                      window_start=TODAY - timedelta(days=3), today=TODAY)
    for reason in self_healing | enough:
        assert lc.is_structural_verdict(reason) is False, '%s 被当成结构性失败 ⇒ 会上锁' % reason
        assert lc.should_close_as_stale_target(verdict_reason=reason, **full_close) is False, (
            '%s 判得出关闭 ⇒ 一条明天就能验的预测被收进回收站' % reason)
    for reason in structural:
        assert reason in answers, '名单挂着 %s，验证器从不答它 ⇒ 那条路是死代码' % reason

    # 反空判：验证器多答一档、名单没人认领时，上面那条对表必须响
    assert (answers | {'a_reason_nobody_registered'}) != structural | self_healing | enough, (
        '空判：新增一档 reason 不会被发现')


def test_only_one_place_decides_whether_a_failure_is_structural():
    """"哪些失败算结构性"这句话只许 `is_structural_verdict` 一处回答（第 52 轮 B-1）。

    仓库规矩：每句"唯一出处"都要有会红的判据，否则下一轮就多一个出处。
    判的是 AST 里的**比较运算**（`reason == 'xxx'`、`xxx in (…)`）——
    写下结论那一处（`reason='same_nav_endpoint'` 是关键字参数）与标签表
    （`REASON_LABELS` 的字典键）都不是"门槛"，不误伤。
    """
    import ast as _ast
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    reasons = set(lc.STRUCTURAL_VERDICT_REASONS)

    def reason_gates(node):
        hits = []
        for cmp_node in _ast.walk(node):
            if not isinstance(cmp_node, _ast.Compare):
                continue
            operands = [cmp_node.left] + list(cmp_node.comparators)
            literals = {o.value for o in operands
                        if isinstance(o, _ast.Constant) and isinstance(o.value, str)}
            # 抄第二把尺子有两种拼法：`reason == 'xxx'` 与 `reason in ('xxx', 'yyy')`。
            # 只判前一种的话，后者（把整张名单原样抄一遍）结构上不可能红（第 52 轮我自己
            # 写第一版时就是这样，是变异 M6 打绿之后补的）。
            for o in operands:
                if isinstance(o, (_ast.Tuple, _ast.List, _ast.Set)):
                    literals |= {e.value for e in o.elts
                                 if isinstance(e, _ast.Constant) and isinstance(e.value, str)}
            picked = literals & reasons
            if picked and any(not isinstance(o, _ast.Constant) for o in operands):
                hits.append(cmp_node.lineno)      # 一侧是字面量、另一侧是变量 ⇒ 拿它当门槛
        return hits

    offenders = []
    scanned = 0
    for base, _dirs, files in os.walk(os.path.join(root, 'src')):
        for name in files:
            if not name.endswith('.py'):
                continue
            rel = os.path.relpath(os.path.join(base, name), root).replace(os.sep, '/')
            scanned += 1
            tree = _ast.parse(open(os.path.join(root, rel), encoding='utf-8').read())
            # `FunctionDef` 与 `AsyncFunctionDef` 一起走（第 53 轮 A-7 = B-7）：
            # 只数前者的话，`src/` 里那些 `async def` 是结构性盲区 —— 抄第二把尺子的人
            # 只要把函数写成 async，这条判据就永远看不见。
            for fn in [n for n in _ast.walk(tree)
                       if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]:
                for lineno in reason_gates(fn):
                    offenders.append('%s:%d' % (rel, lineno))
    assert scanned > 50, '扫描面塌了（只数到 %d 个文件）⇒ 这条判据结构上不可能红' % scanned
    assert offenders == [], (
        '结构性判据被抄成第二把尺子：%s ⇒ 加一档要改 `STRUCTURAL_VERDICT_REASONS` 与两张处置名单'
        % offenders)
    # 反空判：现造两处违规都要被点名（一种比较、一种"把名单整个抄一遍"）
    fake = _ast.parse("def gate(reason):\n"
                      "    return reason == 'no_source_history'\n")
    assert reason_gates(fake), '扫描器自己恒空 ⇒ 这条判据等于没判'
    fake_tuple = _ast.parse("def gate(reason):\n"
                            "    return reason in ('no_source_history', 'same_nav_endpoint')\n")
    assert reason_gates(fake_tuple), '抄成元组名单的那一把看不见 ⇒ 判据有洞'
    # 第三条样品：**同一个函数写成 async** ⇒ 扫描面必须仍然盖到它（A-7 = B-7 那一格）
    tree = _ast.parse("async def gate(reason):\n"
                      "    return reason == 'no_source_history'\n")
    async_hits = [ln for n in _ast.walk(tree)
                  if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                  for ln in reason_gates(n)]
    assert async_hits, 'async def 又成盲区了 ⇒ 写成异步就能抄第二把尺子'


def test_the_close_decision_reads_both_conditions_not_just_the_answer(test_db):
    """那条升级规则的边界：缺任一个条件都不许关（每次调用拆一条门）。"""
    stale = dict(verdict_reason='no_source_history',
                 previous_hold=TODAY - timedelta(days=1),
                 target_date=TODAY - timedelta(days=3),
                 local_latest_nav=TODAY - timedelta(days=400),
                 window_start=TODAY - timedelta(days=3), today=TODAY)
    assert lc.should_close_as_stale_target(**stale) is True
    assert lc.should_close_as_stale_target(**{**stale, 'previous_hold': None}) is False
    assert lc.should_close_as_stale_target(**{**stale, 'verdict_reason': 'insufficient_points'}) is False
    # 那根日期落在自己的目标日**之前** ⇒ 它是创建排期写的，不是上一轮的锁 ⇒ 第一次问出来不关
    assert lc.should_close_as_stale_target(
        **{**stale, 'previous_hold': TODAY - timedelta(days=3)}) is False
    # 锁还没到点（未来）⇒ 这一轮根本不该被问到，更谈不上关
    assert lc.should_close_as_stale_target(
        **{**stale, 'previous_hold': TODAY + timedelta(days=1)}) is False
    # 库里最后一条正好落在窗口起点那天 ⇒ 不是"窗口之前就没发过"，不关
    assert lc.should_close_as_stale_target(
        **{**stale, 'local_latest_nav': TODAY - timedelta(days=3)}) is False


def test_when_the_nav_arrives_the_lock_is_taken_off(test_db, monkeypatch):
    """净值后来补上了 ⇒ 锁必须撤掉，否则这条预测永远带着一个已失效的标签。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY + timedelta(days=5))
    svc = _service(test_db, monkeypatch, {
        'available': True, 'message': '数据充足', 'data_points': 9,
        'latest_date': p.target_date, 'end_nav_age_days': 0, 'reason': 'exact_target'})
    # 这一支只关心"锁撤没撤"，后面取净值的腿一律不真外呼（conftest 会拦，但拦在这里
    # 只会让用例红得不相关）
    monkeypatch.setattr(type(svc), 'get_nav_by_date', lambda self, *a, **k: None)

    svc.verify_prediction(p.id)      # 后面会因取不到净值而停住，锁也该已经撤了

    test_db.refresh(p)
    assert p.next_verify_date is None
    assert classify(p, as_of=TODAY) == DUE_UNVERIFIED
    assert p.is_correct is None
