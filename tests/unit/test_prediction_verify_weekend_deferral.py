# -*- coding: utf-8 -*-
"""S7-2：目标日落在休市日的短期预测 —— 不能放宽成假结论，也不能混进"数据不足"。

复现的原始事实（本地镜像库，预测 1709：512680，周期 1 天，目标 2026-07-11 周六）：
`[起点, 目标日]` 窗口里只有 07-10 一条净值。上一轮（S7-1）的做法是放宽点数门放它过去，
结果 `weekend_previous` 分支取到的起点净值与终点净值是**同一条**，涨跌幅恒为 0，
今天真把它判成了「预测方向错误，最终涨跌+0.00%」并计入准确率 —— 那是假结论。
所以 S7-2 改成：起点与终点落在同一条净值时**拒判**（`same_nav_endpoint`），
并且把"缺目标日附近历史"（补拉最新数据永远补不到）与"净值太旧"（该更新）分开说。
"""
from datetime import date

import pytest

from src.models.database import Blogger, FundHistory, Post, Prediction
import importlib
# 注意：**不能**写 `from src.fund import fund_api as fund_api_module` —— 那个包属性是
# `FundAPI` 实例（`src/fund/__init__.py` 干的），在它身上打桩是空操作，
# 本文件的用例因此真打过东财接口（第 16 轮 BLOCKER-1）。要桩就桩模块。
fund_api_module = importlib.import_module('src.fund.fund_api')
from src.services.prediction_verify_service import PredictionVerifyService

START = date(2026, 7, 10)        # 周五：预测起点，也是唯一一条落在目标日及之前的净值
TARGET_SAT = date(2026, 7, 11)   # 周六：休市，没有净值
PREV = date(2026, 7, 9)
NEXT_MON = date(2026, 7, 13)


def _rows(db, code, days):
    for i, d in enumerate(days):
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0 + i * 0.01))
    db.commit()


def _check(db, code, start, end, min_points=2, today=None):
    return PredictionVerifyService(db)._check_fund_data_availability(
        fund_code=code, nav_start_date=start, window_end=end, target_date=end,
        min_data_points=min_points, today=today or date(2026, 9, 21))


@pytest.fixture(autouse=True)
def no_network_backfill(monkeypatch):
    """单测零网络：验证路径里的按需补拉一律桩掉（这些用例要测的是判据，不是抓取）。"""
    class _NoProbe:
        @staticmethod
        def backfill_history_range(*a, **kw):
            return 0

    monkeypatch.setattr(fund_api_module, 'fund_data_manager', _NoProbe(), raising=False)


def _seed(db, code, prediction_date, target_date):
    blogger = Blogger(name='休市测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, title='t', content='c', post_date=prediction_date)
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                   fund_name='测试基金', sector='测试', prediction_type='up',
                   prediction_date=prediction_date, prediction_period='1天',
                   target_date=target_date, status='pending', is_deleted=False)
    db.add(p)
    db.commit()
    return p


def test_single_nav_endpoint_is_refused_not_judged_flat(test_db):
    """1709 复现：窗口里只有起点那一条 ⇒ 拒判，且给的原因不是"数据不足"。"""
    _rows(test_db, '512680', [PREV, START, NEXT_MON])
    r = _check(test_db, '512680', START, TARGET_SAT)
    assert r['available'] is False, r
    assert r['reason'] == 'same_nav_endpoint', r
    assert '涨跌幅必然为 0' in r['message'], r


def test_refusing_a_degenerate_endpoint_writes_no_conclusion(test_db, monkeypatch):
    """拒判必须真的不落库：`is_correct` 保持 NULL，不产生 0% 的假结论。"""
    from src.services import prediction_verify_service as pvs

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 21)

    monkeypatch.setattr(pvs, 'date', FixedDate)
    _rows(test_db, '512680', [PREV, START, NEXT_MON])
    p = _seed(test_db, '512680', START, TARGET_SAT)
    result = PredictionVerifyService(test_db).verify_prediction(p.id)
    test_db.refresh(p)
    assert result.get('success') is False, result
    assert p.is_correct is None, '退化终点不该写结论'
    assert p.status == 'pending', p.status
    assert not p.end_nav_date, p.end_nav_date


def test_target_today_is_not_called_permanently_unverifiable(test_db):
    """第 10 轮 M-1：目标日就是今天、净值还没发布，是**明天自愈**的日常情形。

    退化终点门必须靠"目标日之后已有净值"来证明目标日真是休市日；
    否则每个工作日 10:30 的 Cron 都会把当天的 1 天期预测误标成"永久不可验"，
    还会建议把目标日往后挪 —— 那正好是仓库明令禁止的未来函数。
    """
    today_tue = date(2026, 7, 21)          # 周二：预测起点
    tomorrow_wed = date(2026, 7, 22)       # 周三：目标日，当天净值尚未发布
    _rows(test_db, '159915', [today_tue])  # 之后没有任何净值 ⇒ 证不了"目标日是休市日"
    r = _check(test_db, '159915', today_tue, tomorrow_wed, today=tomorrow_wed)
    assert r['available'] is False, r
    assert r['reason'] != 'same_nav_endpoint', r
    assert '无法判定方向' not in r['message'] and '调整' not in r['message'], r


def test_weekend_target_with_a_real_second_point_still_verifies(test_db):
    """收紧不能过头：起点 07-09、终点 07-10 是两条净值，周末目标日照旧按前值判定。"""
    _rows(test_db, '159206', [PREV, START, NEXT_MON])
    r = _check(test_db, '159206', PREV, TARGET_SAT)
    assert r['available'] is True, r
    assert r['reason'] == 'weekend_previous', r
    assert r['data_points'] == 2 and r['latest_date'] == START, r


def test_missing_history_near_target_no_longer_says_update_the_data(test_db):
    """515440/158038 类：本地最新净值**晚于**窗口终点 ⇒ 缺的是那段历史，
    提示"请更新基金数据"会把人带去跑同步（同步只补最近端，永远补不到）。"""
    _rows(test_db, '515440', [date(2026, 9, 2), date(2026, 9, 3)])
    r = _check(test_db, '515440', date(2026, 7, 27), date(2026, 7, 28))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r
    assert '请更新基金数据' not in r['message'], r
    assert '补拉最新数据补不到这个窗口' in r['message'], r


def test_stale_nav_still_asks_for_an_update(test_db):
    """反向：最新净值确实**落后**于窗口终点（该更新数据）时，仍要说"请更新基金数据"。"""
    _rows(test_db, '518880', [date(2026, 5, 6), date(2026, 5, 7)])
    r = _check(test_db, '518880', date(2026, 7, 27), date(2026, 7, 28))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r
    assert r['days_behind'] > 0 and '请更新基金数据' in r['message'], r


def test_dead_fund_needs_evidence_before_being_called_unverifiable(test_db):
    """第 12 轮 MAJOR-3：结构性归因必须有凭据，"到期很久了"这种日历推断不算证据。

    净值停在目标日之前的老基金：
    - 没问过数据源 ⇒ 只能报"数据不足"，继续重试（可能就是我们自己的镜像坏了）；
    - 问过并留下凭据 ⇒ 才允许判 `same_nav_endpoint`，措辞引用凭据。
    配套修复在补拉侧：短窗口免补拉现在要求"终点那天也有净值行"，
    否则这种窗口永远问不到凭据。
    """
    from src.fund import backfill_proofs
    code = '003033'
    _rows(test_db, code, [date(2020, 12, 4), START])
    r = _check(test_db, code, START, TARGET_SAT, today=date(2026, 9, 21))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r

    backfill_proofs.record_probe(test_db, code, START, TARGET_SAT, 0)
    test_db.commit()
    r2 = _check(test_db, code, START, TARGET_SAT, today=date(2026, 9, 21))
    assert r2['reason'] == 'same_nav_endpoint', r2
    assert '已按区间问过数据源' in r2['message'], r2


def test_normal_weekday_case_is_untouched(test_db):
    """已能验证的常规预测结论不变（这道修改只收紧退化情形，不动正常窗口）。"""
    t = date(2026, 7, 15)        # 周三，有净值
    _rows(test_db, '159915', [date(2026, 7, 14), t, date(2026, 7, 16)])
    r = _check(test_db, '159915', date(2026, 7, 14), t)
    assert r['available'] is True and r['reason'] == 'exact_target', r


def test_current_nav_date_uses_the_nav_actually_used(test_db, monkeypatch):
    """第 15 轮 m-1：同一端点不能在两个字段里是两个日期。

    `end_nav_date` 在 S7-2 改成"实际取到的那一天"（周六没有净值就是周五），
    但 `current_nav_date` 还写着请求的 `window_end` —— 同一笔净值两个日期，
    而两个字段都会进快照与前端"当前净值"。
    """
    from src.services import prediction_verify_service as pvs

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 21)

    class _NoLLM:
        def verify_prediction(self, *a, **kw):
            return None

    monkeypatch.setattr(pvs, 'date', FixedDate)
    # 边界分才会去问 LLM，这里把那一腿关掉：本用例测的是落库日期，不是评分
    monkeypatch.setattr(pvs.PredictionVerifyService, 'llm_analyzer',
                        property(lambda self: _NoLLM()))
    _rows(test_db, '159206', [PREV, START, NEXT_MON])
    p = _seed(test_db, '159206', PREV, TARGET_SAT)
    result = PredictionVerifyService(test_db).verify_prediction(p.id)
    test_db.refresh(p)
    assert result.get('success') is True, result
    assert p.end_nav_date == START, p.end_nav_date          # 用的是周五那条
    assert p.current_nav_date == START, (
        'current_nav_date 仍写请求的周六 %s，与 end_nav_date %s 打脸'
        % (p.current_nav_date, p.end_nav_date))
    assert p.current_nav == p.end_nav


def test_injected_today_reaches_the_proof_ttl(test_db, monkeypatch):
    """第 15 轮 m-3：注入的 `today` 要一路传到凭据判定。

    TTL 分档（窗口终点距今 <30 天 ⇒ 只信 1 天）与"未来时间戳"防线都按天算；
    上一轮只把 today 传到了 `covering_probe`，凭据入口这一层仍吃墙上时钟 ⇒
    固定日期的回放会拿到一个"当时并不存在的宽限"。
    """
    from src.fund import backfill_proofs

    seen = {}

    def _spy(db, code, start, end, today=None):
        seen['today'] = today
        return None

    monkeypatch.setattr(backfill_proofs, 'fresh', _spy)
    _rows(test_db, '515440', [date(2026, 9, 2), date(2026, 9, 3)])
    injected = date(2026, 5, 6)
    _check(test_db, '515440', date(2026, 7, 27), date(2026, 7, 28), today=injected)
    assert seen['today'] == injected, (
        '凭据判定没吃到注入的 today（拿到的是 %r）' % (seen.get('today'),))


# --- 第 16 轮 BLOCKER-2：端点早于目标日时，"市场没有那一行"与"本地缺那一行"必须可区分 ---

def test_lagging_endpoint_without_evidence_is_not_finalized(test_db):
    """点数够、但端点比目标日早 2 个工作日且拿不到"那几天休市"的证据 ⇒ 不许落死结论。

    复现镜像实测：515070 / 端点 09-11 / 目标 09-17 被 `waited_previous` 判成"判错 0 分"，
    而数据源其实有目标日净值（现取回来判对 100 分）。`is_correct` 一旦非空就永不重判
    （`filter_due_for_verify` 只捞 NULL），所以这种"用前值定终身"必须挡在写库之前。
    """
    _rows(test_db, '515070', [date(2026, 9, 10), date(2026, 9, 11)])
    r = _check(test_db, '515070', date(2026, 9, 10), date(2026, 9, 15),
               today=date(2026, 9, 22))
    assert r['available'] is False, r
    assert r['reason'] == 'endpoint_lag_unproven', r
    assert '不能拿这条前值下终局结论' in r['message'], r


def test_lagging_endpoint_refuses_without_writing_a_verdict(test_db, monkeypatch):
    """拒判必须真的不落库：结论、状态、台账都不许多出一条。"""
    from src.services import prediction_verify_service as pvs

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 22)

    _rows(test_db, '515180', [date(2026, 9, 10), date(2026, 9, 11)])
    p = _seed(test_db, '515180', date(2026, 9, 10), date(2026, 9, 15))
    monkeypatch.setattr(pvs, 'date', FixedDate)
    result = PredictionVerifyService(test_db).verify_prediction(p.id)
    test_db.refresh(p)
    assert result.get('success') is False, result
    assert p.is_correct is None and p.status == 'pending', (p.is_correct, p.status)
    assert not p.verify_history


def test_a_row_after_the_target_proves_those_days_were_holidays(test_db):
    """合法证据②：库里已有目标日之后的净值 ⇒ 中间空着的工作日确实是法定节假日，可判。"""
    _rows(test_db, '512400', [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 16)])
    r = _check(test_db, '512400', date(2026, 9, 10), date(2026, 9, 15),
               today=date(2026, 9, 22))
    assert r['available'] is True, r
    assert r['reason'] == 'waited_previous', r


def test_a_fresh_proof_also_legitimises_the_previous_endpoint(test_db):
    """合法证据③：已按区间问过数据源并留下凭据 ⇒ 可以再判，且提示语要带凭据原文。"""
    from src.fund import backfill_proofs

    _rows(test_db, '159501', [date(2026, 9, 10), date(2026, 9, 11)])
    db_session = test_db
    backfill_proofs.record_probe(db_session, '159501', date(2026, 9, 10), date(2026, 9, 15),
                                 source_rows=2)
    db_session.commit()
    r = _check(test_db, '159501', date(2026, 9, 10), date(2026, 9, 15),
               today=date(2026, 9, 22))
    assert r['available'] is True, r


def test_saturday_target_needs_no_evidence_to_use_friday(test_db):
    """不能收紧过头：周六目标日 + 周五端点是**日历可证**的，不该因此卡住。"""
    _rows(test_db, '159995', [PREV, START])          # 07-09(四)、07-10(五)，之后没有行
    r = _check(test_db, '159995', PREV, TARGET_SAT, today=date(2026, 7, 13))
    assert r['available'] is True, r
    assert r['reason'] == 'weekend_previous', r
