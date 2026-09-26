# -*- coding: utf-8 -*-
"""`close_unknowable_predictions.py` 的判据（把"永远判不出来"的预测收进回收站）。

要钉住的是**两条证据都要在场**这件事：
① 现场问数据源、这个窗口答 **0 条**；② 库里这只代码最后一条净值**早于窗口起点**。
只中一条就不许关 —— 少了 ①会把一次接口抖动当成产品停更，少了 ②会把**我们自己没同步**
说成产品停更（那正是第 23 轮那种把镜像坏了当库坏了的错，代价是老板本可以验证的预测被关掉）。
另外钉三件收尾的事：默认 dry-run 一行都不动、缺确认词在连库之前就退出、
关闭**不写 is_correct**（判对/判错都不算，准确率不动）且能按备份还原。
"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'scripts', 'close_unknowable_predictions.py')


def _seed(db, *, code, target_offset, nav_back_days, pred_type='up'):
    from src.models.database import Blogger, FundHistory, Post, Prediction

    blogger = db.query(Blogger).filter(Blogger.name == '关闭判据博主').first()
    if not blogger:
        blogger = Blogger(name='关闭判据博主', platform='wechat')
        db.add(blogger)
        db.flush()
    today = date.today()
    post = Post(blogger_id=blogger.id, title='c', content='c',
                post_date=today - timedelta(days=40))
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                   fund_name='测试基金', sector='测试', prediction_type=pred_type,
                   prediction_date=today - timedelta(days=40), prediction_period='1个月',
                   target_date=today - timedelta(days=target_offset), status='pending',
                   is_expired=False)
    db.add(p)
    if nav_back_days is not None:
        db.add(FundHistory(fund_code=code, nav_date=today - timedelta(days=nav_back_days),
                           nav=1.2))
    db.commit()
    db.refresh(p)
    return p


def _stub_source(monkeypatch, rows_by_code):
    """数据源桩：给 list ⇒ 答了这么多条；给 None ⇒ 没答话（抛错同义）。"""
    import importlib

    api = importlib.import_module('src.fund.fund_api')
    monkeypatch.setattr(
        api.fund_api, 'get_fund_history_range',
        lambda code, start, end: rows_by_code.get(code, []))


def _import_script():
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    import close_unknowable_predictions as mod  # noqa: F401
    sys.path.pop(0)
    return mod


def test_both_evidences_present_closes_and_leaves_the_verdict_untouched(test_db, monkeypatch):
    mod = _import_script()
    p = _seed(test_db, code='DEAD99', target_offset=3, nav_back_days=400)
    _stub_source(monkeypatch, {'DEAD99': []})

    items, skipped = mod.plan(test_db, date.today())
    assert [i['prediction_id'] for i in items] == [p.id] and skipped == []

    mod.apply_close(test_db, items, date.today())
    test_db.refresh(p)
    assert p.is_deleted is True and p.deleted_by == 'system'
    assert p.is_correct is None, '关闭不是判错：判对判错都不许写'
    assert p.verify_count in (0, None)
    assert '回收站' in p.delete_reason and '不计入准确率' in p.delete_reason
    from src.models.database import PredictionChangeLog
    log = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == p.id).order_by(
        PredictionChangeLog.id.desc()).first()
    assert log is not None and log.source == 'system'


def test_a_recent_nav_row_means_we_are_stale_not_the_fund(test_db, monkeypatch):
    """反面对照 ①：源端答 0 条，但库里最后一条净值**晚于窗口起点** ⇒ 是我们没同步，不关。"""
    mod = _import_script()
    p = _seed(test_db, code='LATE99', target_offset=3, nav_back_days=5)
    _stub_source(monkeypatch, {'LATE99': []})

    items, skipped = mod.plan(test_db, date.today())
    assert items == [] and [s[0] for s in skipped] == [p.id]


def test_a_silent_source_is_not_an_empty_answer(test_db, monkeypatch):
    """反面对照 ②：接口没答话（None）≠"这段没有" ⇒ 一条都不许关（第 10 轮 B-1 那一课）。"""
    mod = _import_script()
    _seed(test_db, code='NONE99', target_offset=3, nav_back_days=400)
    _stub_source(monkeypatch, {'NONE99': None})

    items, skipped = mod.plan(test_db, date.today())
    assert items == [] and '没答话' in skipped[0][2]


def test_source_ansering_rows_keeps_the_prediction_waiting(test_db, monkeypatch):
    """反面对照 ③：源端这个窗口给得出净值 ⇒ 该预测还在等，交给正常重问节奏。"""
    mod = _import_script()
    _seed(test_db, code='GAPS99', target_offset=3, nav_back_days=400)
    _stub_source(monkeypatch, {'GAPS99': [{'FSRQ': '2026-09-01', 'DWJZ': '1.1'}]})

    items, _ = mod.plan(test_db, date.today())
    assert items == []


def test_restore_from_backup_is_dry_run_by_default(test_db, monkeypatch):
    """还原默认只报"将放回几行"；真还原要 --apply，且恢复后原因被清空（回到待验证）。"""
    mod = _import_script()
    p = _seed(test_db, code='REST99', target_offset=3, nav_back_days=400)
    _stub_source(monkeypatch, {'REST99': []})
    items, _ = mod.plan(test_db, date.today())
    path = mod.apply_close(test_db, items, date.today())
    test_db.refresh(p)
    assert p.is_deleted is True
    try:
        assert mod.restore(test_db, path, apply_it=False) == 3
        test_db.refresh(p)
        assert p.is_deleted is True, 'dry-run 不许把行放回活跃列表'

        assert mod.restore(test_db, path, apply_it=True) == 0
        test_db.refresh(p)
        assert p.is_deleted is False and p.delete_reason is None
        assert p.is_correct is None, '还原也不会凭空写出结论'
    finally:
        os.remove(path)        # 用例自己造的备份，绝不留在仓库里（上一轮那种残渣就是这么来的）


def test_cli_refuses_before_touching_the_database():
    """缺确认词、以及"在镜像上喊 --production"，都要**在连库与取数之前**退 4。

    两格都把 `DATABASE_URL` 钉成 sqlite：即便哪天 `.env` 指向生产，这条用例也**不可能**
    在生产上写一行（`load_dotenv` 默认不覆盖已有变量）。
    """
    env = dict(os.environ, PYTHONIOENCODING='utf-8',
               DATABASE_URL='sqlite:///' + os.path.join(ROOT, 'data', 'copy-cli-close-unk.db'))
    for args, expect_word in [
        (['--apply'], 'CLOSE-UNVERIFIABLE'),
        (['--production', '--apply', '--confirm', 'CLOSE-UNVERIFIABLE'], '线上'),
    ]:
        r = subprocess.run([sys.executable, SCRIPT] + args,
                           capture_output=True, text=True, env=env, cwd=ROOT, timeout=300,
                           encoding='utf-8', errors='replace')   # 父进程默认 cp936，中文回执会解不开
        assert r.returncode == 4, '%s ⇒ 退码 %s（应为 4）：%s' % (args, r.returncode, r.stdout)
        assert expect_word in (r.stdout + r.stderr), (args, r.stdout)
