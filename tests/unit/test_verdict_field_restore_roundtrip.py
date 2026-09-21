# -*- coding: utf-8 -*-
"""还原工具必须真的能还原，同步判据必须只认行内证据（第 15 轮）。

来由（一次真实事故，不是假想）：`scripts/replay_verifications_on_copy.py --limit 200`
报出唯一一条"未解释漂移" id=3180 —— 端点数值完全一致，只有 `is_correct` 从 True 变
False。顺着台账查到根因：`scripts/repair_replay_side_effects.py` 自己手抄了一份
还原字段清单，**漏了 `verify_score`**，于是那次还原把 88 行的结论搬回来了、分数却停在
误写值上，11 行因此出现"结论 True / 分数 49 / 台账 100"的自相矛盾，博主平均分也偏。
同一次排查还发现还原侧漏了 `verify_history`（第 14 轮只把它加进了快照，没加进还原清单），
所以"整批还原"这句承诺当时仍是半真半假。

这里的三个用例各自挡一类复发：
1. 还原后 `verify_history`/`verify_score` 必须回到原样（会红的往返测试，不是清单包含测试）；
2. 唯一的还原清单必须覆盖 `clear_verification_fields` 动的每一个字段；
3. 不许再有第二份手抄清单；同步脚本只按台账改分数，证据矛盾时只报不改。
"""
import ast
import io
import os
from datetime import date, timedelta

import pytest

from src.models.database import Blogger, Prediction, Post
from src.services.prediction_change_log_service import (
    add_prediction_change_log, snapshot_prediction)
from src.services.prediction_verify_service import clear_verification_fields
from scripts.resync_verdict_scalars import find_desynced
from scripts.restore_prediction_batch import RESTORE_FIELDS, apply_before_state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FieldSpy:
    """记录"哪些字段名被写过"，用来把字段清单和唯一判据源对齐。"""

    def __init__(self):
        object.__setattr__(self, 'touched', set())

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        self.touched.add(name)


@pytest.fixture(autouse=True)
def _clean_rt_rows(db_session):
    """只清本文件造的行（`db_session` 打的是真库文件，提交了就会留给别人）。"""
    yield
    from src.models.database import PredictionChangeLog
    ids = [p.id for p in db_session.query(Prediction).filter(
        Prediction.fund_code == 'RT9999').all()]
    if ids:
        db_session.query(PredictionChangeLog).filter(
            PredictionChangeLog.prediction_id.in_(ids)).delete(synchronize_session=False)
    db_session.query(Prediction).filter(Prediction.fund_code == 'RT9999').delete(
        synchronize_session=False)
    db_session.query(Post).filter(Post.content == 'RT 往返测试帖子').delete(
        synchronize_session=False)
    db_session.query(Blogger).filter(Blogger.name == 'RT还原测试博主').delete(
        synchronize_session=False)
    db_session.commit()


def _seed_verified(db):
    blogger = Blogger(name='RT还原测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='RT 往返测试帖子', post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    ledger = [{'date': '2026-06-08', 'score': 80, 'is_correct': True, 'verify_type': 'final'},
              {'date': '2026-06-15', 'score': 80, 'is_correct': True, 'verify_type': 'final'}]
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code='RT9999',
        fund_name='往返测试基金', prediction_type='up', prediction_content='上涨',
        prediction_date=date(2026, 6, 1), prediction_period='1周',
        target_date=date(2026, 6, 8), status='success', is_correct=True,
        verify_count=2, verify_score=80, start_nav=1.0, end_nav=1.2,
        actual_change=20.0, verify_history=ledger)
    db.add(prediction)
    db.commit()
    return prediction


def test_restore_actually_brings_verify_history_and_score_back(db_session):
    """往返测试：改完之后用还原清单退回去，台账和分数都得回来。

    变异形态必须和生产一致 —— 验证路径对 `verify_history` 是**原地 append**
    （`prediction_verify_service.py:1086`），不是重新赋值。写成重新赋值的话，
    "快照存了活对象引用"这个 bug 根本不会被测出来（第 15 轮 MAJOR-1）。
    """
    prediction = _seed_verified(db_session)
    before = snapshot_prediction(prediction)
    assert 'verify_history' in before, "快照不带历史，还原就无从依据"
    # 前像必须是**拷贝**：拿完快照之后再原地 append，也不能把前像一起改掉
    prediction.verify_history.append(
        {'date': '2026-06-22', 'score': 49, 'is_correct': False, 'verify_type': 'final'})
    assert len(before['verify_history']) == 2, "快照存的是活引用，前像被后来的改动污染了"
    prediction.verify_history.pop()
    db_session.commit()

    prediction.verify_score = 49                     # 误写：跑批把分数改了
    prediction.verify_history.append(
        {'date': '2026-06-22', 'score': 49, 'is_correct': False, 'verify_type': 'final'})
    prediction.is_correct = False
    prediction.verify_count = 3
    log = add_prediction_change_log(db_session, prediction, action='verification_rollback',
                                    source='maintenance', before_state=before, run_id='t-rt')
    db_session.commit()
    assert log is not None
    assert 'verify_history' in (log.changed_fields or []), (
        '台账被原地改过却不在 changed_fields 里 ⇒ 快照与前像都是活的引用')

    apply_before_state(prediction, log.before_state)
    db_session.commit()
    db_session.refresh(prediction)
    assert prediction.verify_score == 80, "标量分数没退回原值"
    assert prediction.verify_count == 2
    assert len(prediction.verify_history or []) == 2, "台账还是重判那一轮的那三条"
    assert [h['score'] for h in prediction.verify_history] == [80, 80]


def test_both_reset_paths_clear_the_same_fields():
    """撤结论的两条路径必须动同一批字段（第 15 轮 m-2）。

    维护服务以前自己抄了 16 行赋值、判据函数只有 15 行，差别就在 `ai_judgment`：
    走 `rollback_invalid_verifications` 撤掉的预测，状态回到 pending 却仍挂着上一轮
    的 AI 判词（"预测下跌实际微涨，方向判断正确"这种话）。
    """
    from src.services.prediction_maintenance_service import PredictionMaintenanceService

    service_side, maintenance_side = _FieldSpy(), _FieldSpy()
    clear_verification_fields(service_side)
    PredictionMaintenanceService._reset_verification(maintenance_side)
    assert 'ai_judgment' in service_side.touched, '撤结论不撤 AI 判词，pending 行会留着旧判词'
    assert service_side.touched == maintenance_side.touched, (
        '两条"退回未验证"的字段清单又分叉了：%s'
        % (service_side.touched ^ maintenance_side.touched))


def test_restore_field_list_covers_every_verdict_field():
    """还原清单必须覆盖"退回未验证"会动的每一个字段。

    漏一个的后果已经实测过：还原后字段之间自相矛盾，而还原脚本自己报告"已还原 N 条"。
    """
    spy = _FieldSpy()
    clear_verification_fields(spy)
    missing = sorted(spy.touched - set(RESTORE_FIELDS))
    assert not missing, ("这些判定字段会被 clear_verification_fields 清空，"
                         "却不在还原清单里，还原后必然与台账打脸：%s" % missing)


def test_repair_script_does_not_hand_copy_its_own_field_list():
    """手抄清单就是这个 bug 的来源：清单只允许 restore_prediction_batch 一处。"""
    path = os.path.join(ROOT, 'scripts', 'repair_replay_side_effects.py')
    tree = ast.parse(io.open(path, encoding='utf-8').read())
    local_defs = [n.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  for n in node.targets if isinstance(n, ast.Name)]
    assert 'RESTORE_FIELDS' not in local_defs, (
        '还原脚本又自己抄了一份字段清单 —— 请改成 from restore_prediction_batch import '
        'RESTORE_FIELDS，否则漏字段这类 bug 会在两份清单之间重新长出来')
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)
    assert 'RESTORE_FIELDS' in imported, '修复脚本没有复用唯一的还原字段清单'


def _row(**kw):
    """不入库的假预测：find_desynced 只读属性，不需要真 ORM 行。"""
    defaults = dict(id=1, fund_code='515000', is_correct=False, verify_score=0,
                    verify_history=[], target_date=date(2026, 6, 8), blogger_id=1)
    defaults.update(kw)
    return type('R', (), defaults)()


def test_find_desynced_uses_the_ledger_and_only_the_ledger():
    ledger = [{'date': '2026-06-08', 'score': 100, 'is_correct': True}]
    fixable, manual = find_desynced([
        _row(is_correct=True, verify_score=49, verify_history=ledger),        # 3180 那一型
        _row(is_correct=True, verify_score=100, verify_history=ledger),       # 本来就一致
        _row(is_correct=None, verify_score=None, verify_history=ledger),      # 已回溯：不反推结论
        _row(is_correct=None, verify_score=None, verify_history=[]),          # 未验证：不该被碰
    ])
    assert [(p.id, have, want) for p, have, want in fixable] == [(1, 49, 100)]
    assert manual == []


def test_find_desynced_refuses_to_guess_when_evidence_contradicts():
    """台账与标量结论方向相反、或根本没有台账：只报，不改。"""
    rows = [
        _row(id=11, is_correct=True, verify_score=20,
             verify_history=[{'score': 90, 'is_correct': False}]),
        _row(id=12, is_correct=True, verify_score=20, verify_history=[]),
        _row(id=13, is_correct=True, verify_score=None,
             verify_history=[{'score': 90, 'is_correct': True}]),
        _row(id=14, is_correct=True, verify_score=20,
             verify_history=[{'is_correct': True}]),
    ]
    fixable, manual = find_desynced(rows)
    assert fixable == [], '证据互相矛盾时不许自动改库'
    assert sorted(p.id for p, _why in manual) == [11, 12, 13, 14]


def test_find_desynced_ignores_rows_without_a_verdict():
    """未验证的行没有"该同步成什么"的答案；滚动验证途中也不该被脚本插手。"""
    today = date.today()
    rows = [_row(id=21, is_correct=None, verify_score=None, verify_history=[],
                 target_date=today + timedelta(days=3))]
    assert find_desynced(rows) == ([], [])


def test_llm_leg_bucket_requires_every_piece_of_evidence():
    """漂移闸门那条"LLM 复核腿"分类必须是**证据齐了才免**，不能是橡皮图章。

    加了这一桶，"未解释漂移=0"才有意义：少任何一条行内证据都要退回未解释。
    """
    from scripts.replay_verifications_on_copy import _llm_leg_explanation

    ledger = {'verify_type': 'llm_verify', 'score': 100, 'is_correct': True}
    before = {'verify_score': 100, 'is_correct': True}
    diffs = {'is_correct': (True, False), 'status': ('success', 'failed')}
    assert _llm_leg_explanation(diffs, before, ledger), '三条证据都在却没认出来'
    assert _llm_leg_explanation(diffs, before, None) is None, '没有台账就无从证明走过那一腿'
    assert _llm_leg_explanation(diffs, before, dict(ledger, verify_type='final')) is None
    assert _llm_leg_explanation(diffs, before, dict(ledger, score=80)) is None, \
        '台账分数与旧标量不是一份记录，不能算同一腿'
    assert _llm_leg_explanation(diffs, dict(before, is_correct=False), ledger) is None
    assert _llm_leg_explanation(dict(diffs, end_nav=(1.0, 1.1)), before, ledger) is None, \
        '端点也变了就不是"少一条腿"能解释的，得留在未解释里'
