# -*- coding: utf-8 -*-
"""第 97 批（2026-10-14）：常驻调度器接线 + 执行日持久化 + 启动追赶。

老板点名的诉求：「待验证的会自动验证，但基金不会自动更新，验证前应先拿到最新数据」。
探索当场量到的事实：
① 「验证前先更新基金」的顺序在调度器（10:00 窗口）、Render Cron（daily）、验证侧
   按区间补拉（`verify_prediction` 对每条预测先 `backfill_history_range`）三处本来就成立，
   数据不齐时验证只会等（`waiting_target_nav` / `endpoint_lag_unproven`）不会硬判。
② 真正缺的是「自动」：`start_scheduler()` 在全仓没有任何调用点（只有本模块
   `__main__` 自测入口）——所谓常驻调度器从没跑过；生产库的 `batch_analysis_tasks`
   里也查不到 Cron 应有时刻（北京 18:30 附近）产生的验证批次 ⇒ 推进全靠手点。
③ 旧注释声称"使用持久化状态，避免重启后重置"，实际四个日期字段纯内存，每次重启
   都会全量重放 update+verify（白打数据源、易撞东财限流）。

本文件的判据把这三件事钉住：开关接线（默认关、开则起停）、追赶顺序（更新在验证前、
当天跑过的不重放）、持久化往返（跨实例读回、坏值当没跑过、读库炸了不崩线程）、
镜像探针永不自启。
"""
from datetime import date, datetime

from src.core.config import config
from src.tasks.scheduler import (
    BEIJING_TZ,
    TaskScheduler,
    _STATE_ATTRS,
    _STATE_KEY_PREFIX,
)

# 固定的"今天"：故意不用墙钟，"按北京日比较"才量得出来（同第 88 批那把钉住的北京钟）。
NOW = datetime(2026, 3, 14, 14, 30, tzinfo=BEIJING_TZ)
TODAY = NOW.date()


def _recording_scheduler():
    """桩掉两个真动作 + 用真 `_mark_step_done` 的置位语义（记调用并写内存日期）。"""
    calls = []
    s = TaskScheduler()

    def _mark(step_key, day):
        calls.append('mark:' + step_key)
        setattr(s, _STATE_ATTRS[step_key], day)

    s._run_fund_update = lambda: calls.append('update') or {"success": True}
    s._run_prediction_verify = lambda: calls.append('verify') or {"success": True}
    s._mark_step_done = _mark
    return s, calls


# ---------------------------------------------------------------- 追赶逻辑

def test_stale_day_runs_update_before_verify():
    """当天两班都没跑 ⇒ 必须先「基金更新」再「预测验证」，各自记完成日。"""
    s, calls = _recording_scheduler()          # 日期全 None = 从没跑过
    s._ensure_verify_cycle(NOW)
    assert calls == ['update', 'mark:fund_update', 'verify', 'mark:verify']


def test_same_day_replay_is_silent():
    """当天已跑完 ⇒ 再进来一个字节都不许做（旧版每次重启全量重放的那一笔）。"""
    s, calls = _recording_scheduler()
    s._ensure_verify_cycle(NOW)
    calls.clear()
    s._ensure_verify_cycle(NOW)                # 模拟同一天内的第二次/重启
    assert calls == []


def test_verify_done_today_but_update_missing_runs_update_only():
    """验证跑过、更新没跑（比如当天手动验过但没更新）⇒ 只补更新，不重放验证。"""
    s, calls = _recording_scheduler()
    s._last_verify_date = TODAY
    s._ensure_verify_cycle(NOW)
    assert calls == ['update', 'mark:fund_update']


def test_update_done_today_runs_verify_without_reupdate():
    """更新跑过、验证没跑 ⇒ 直接验证，不再打一遍全量更新。"""
    s, calls = _recording_scheduler()
    s._last_fund_update_date = TODAY
    s._ensure_verify_cycle(NOW)
    assert calls == ['verify', 'mark:verify']


# ---------------------------------------------------------------- 持久化往返

def _purge_state_rows():
    from src.models.database import SessionLocal, SystemConfig
    db = SessionLocal()
    try:
        db.query(SystemConfig).filter(
            SystemConfig.config_key.in_(
                [_STATE_KEY_PREFIX + k for k in _STATE_ATTRS])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_mark_and_load_roundtrip_survives_a_new_instance():
    """`_mark_step_done` 写进 `system_config` 的日期，新实例必须读得回来。

    这条只认"新实例"：光验证内存置位的话，M126（不提交库）那种变异打不红。
    """
    _purge_state_rows()
    try:
        s1 = TaskScheduler()
        s1._mark_step_done('verify', date(2026, 3, 13))
        s1._mark_step_done('fund_update', date(2026, 3, 14))

        s2 = TaskScheduler()
        s2._load_persisted_dates()
        assert s2._last_verify_date == date(2026, 3, 13)
        assert s2._last_fund_update_date == date(2026, 3, 14)
        assert s2._last_cleanup_date is None     # 没写过的键保持"没跑过"
    finally:
        _purge_state_rows()


def test_corrupt_state_value_is_treated_as_never_run():
    """库里日期坏掉（手改/旧格式）⇒ 当作没执行过，让它自然重跑，而不是崩。"""
    from src.models.database import SessionLocal, SystemConfig
    _purge_state_rows()
    try:
        db = SessionLocal()
        db.add(SystemConfig(config_key=_STATE_KEY_PREFIX + 'fund_update',
                            config_value='not-a-date'))
        db.commit()
        db.close()

        s = TaskScheduler()
        s._load_persisted_dates()
        assert s._last_fund_update_date is None
    finally:
        _purge_state_rows()


def test_state_read_failure_does_not_kill_the_scheduler(monkeypatch):
    """连 `SessionLocal()` 本身都炸 ⇒ 不抛出（调度线程起手不许死），
    且按"当天没跑过"fail-open —— 宁可多补一轮，不许自动变零轮。"""
    import src.models.database as dbmod

    def _boom():
        raise RuntimeError('数据库炸了')

    monkeypatch.setattr(dbmod, 'SessionLocal', _boom)
    s = TaskScheduler()
    s._last_verify_date = date(2026, 1, 1)     # 预置旧值 ⇒ 读失败也不许保留"跑过"假象
    s._load_persisted_dates()                  # 不应抛
    assert s._last_verify_date is None         # fail-open：当作没跑过，会重补


# ---------------------------------------------------------------- 接线开关

def test_default_off_app_startup_does_not_start_scheduler(monkeypatch):
    """默认（SCHEDULER_ENABLED=false，生产/测试口径）⇒ lifespan 一个调度线程都不起。"""
    from fastapi.testclient import TestClient
    import src.tasks.scheduler as sched_mod
    from src.api import main as api_main

    assert config.SCHEDULER_ENABLED is False    # conftest 钉死 false 的前提自证
    rec = []
    monkeypatch.setattr(sched_mod, 'start_scheduler', lambda: rec.append('start'))
    monkeypatch.setattr(sched_mod, 'stop_scheduler', lambda: rec.append('stop'))
    with TestClient(api_main.app):
        pass
    assert rec == []


def test_switch_on_app_startup_starts_and_stops_scheduler(monkeypatch):
    """开关置 true ⇒ lifespan 起手 start、收手 stop（异常也要收线程）。"""
    from fastapi.testclient import TestClient
    import src.tasks.scheduler as sched_mod
    from src.api import main as api_main

    rec = []
    monkeypatch.setattr(sched_mod, 'start_scheduler', lambda: rec.append('start'))
    monkeypatch.setattr(sched_mod, 'stop_scheduler', lambda: rec.append('stop'))
    monkeypatch.setattr(config, 'SCHEDULER_ENABLED', True)
    with TestClient(api_main.app):
        pass
    assert rec == ['start', 'stop']


def test_serve_mirror_never_autostarts_the_scheduler():
    """镜像探针必须永久钉死调度器：`.env`=true 时调试起服务也不许自动打数据源、
    不许与真实应用互抢「当天已跑」标记。判的是 serve_mirror 源码在
    `uvicorn.run(...)` 之前把环境变量与已加载的 config 属性两处都设 false。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / 'scripts' / 'serve_mirror.py').read_text(
        encoding='utf-8')
    run_at = src.index('uvicorn.run(')
    before = src[:run_at]
    assert "os.environ['SCHEDULER_ENABLED'] = 'false'" in before
    assert '_app_config.SCHEDULER_ENABLED = False' in before
