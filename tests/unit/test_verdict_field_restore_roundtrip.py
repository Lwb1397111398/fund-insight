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
    """只清本文件造的行（`db_session` 打的是真库文件，提交了就会留给别人）。

    按"我那个博主"倒着删（先预测/日志、再帖子、最后博主），不能按帖子文案前缀删 ——
    用例一旦换文案就会留下帖子，接着博主删不掉，外键约束当场炸。
    """
    yield
    from src.models.database import PredictionChangeLog
    blogger_ids = [b.id for b in db_session.query(Blogger).filter(
        Blogger.name == 'RT还原测试博主').all()]
    pred_ids = [p.id for p in db_session.query(Prediction).filter(
        Prediction.fund_code.like('RT99%')).all()]
    if pred_ids:
        db_session.query(PredictionChangeLog).filter(
            PredictionChangeLog.prediction_id.in_(pred_ids)).delete(synchronize_session=False)
    db_session.query(Prediction).filter(Prediction.fund_code.like('RT99%')).delete(
        synchronize_session=False)
    if blogger_ids:
        db_session.query(Post).filter(Post.blogger_id.in_(blogger_ids)).delete(
            synchronize_session=False)
    db_session.query(Blogger).filter(Blogger.id.in_(blogger_ids or [-1])).delete(
        synchronize_session=False)
    db_session.commit()


def _seed_verified(db, code='RT9999', label='RT 往返测试帖子'):
    blogger = Blogger(name='RT还原测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content=label, post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    ledger = [{'date': '2026-06-08', 'score': 80, 'is_correct': True, 'verify_type': 'final'},
              {'date': '2026-06-15', 'score': 80, 'is_correct': True, 'verify_type': 'final'}]
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code=code,
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


def test_restore_list_is_a_subset_of_the_snapshot():
    """还原清单里的每一个字段都必须真的被快照下来（反方向的那一半）。

    上一版只测了"`clear_verification_fields` 动的字段都在还原清单里"，没测反方向：
    还原清单若多出快照没有的名字，`apply_before_state` 会静默 `continue`，
    "还原了这一项"就又是一句空话（第 16 轮 MINOR-6）。
    """
    from src.services.prediction_change_log_service import SNAPSHOT_FIELDS

    extra = sorted(set(RESTORE_FIELDS) - set(SNAPSHOT_FIELDS))
    assert not extra, '这些字段还原时会去取快照、但快照根本不存，等于永远还原不了：%s' % extra


def test_the_single_reset_path_clears_the_ai_judgment_too():
    """撤结论只有一条清单（`clear_verification_fields`），它必须包含 `ai_judgment`。

    第 15 轮 m-2 的原件是"两条路径等价"，而维护服务那条已经退化成纯转发并被删
    （第 20 轮 MINOR-9：留着就是文档里的第三个调用方虚指）。所以这里只钉住唯一清单本身。
    """
    spy = _FieldSpy()
    clear_verification_fields(spy)
    assert 'ai_judgment' in spy.touched, \
        '撤结论不撤 AI 判词 ⇒ status 回到 pending 却仍挂着"方向判断正确"这种话'


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
                    verify_history=[], target_date=date(2026, 6, 8), blogger_id=1,
                    actual_change=1.0, end_nav=1.05, current_nav=1.05,
                    end_nav_date=date(2026, 6, 8), current_nav_date=date(2026, 6, 8))
    defaults.update(kw)
    return type('R', (), defaults)()


def test_find_desynced_uses_the_ledger_and_only_the_ledger():
    ledger = [{'date': '2026-06-08', 'score': 100, 'is_correct': True, 'change': 1.0}]
    fixable, manual = find_desynced([
        _row(is_correct=True, verify_score=49, verify_history=ledger),        # 3180 那一型
        _row(is_correct=True, verify_score=100, verify_history=ledger),       # 本来就一致
        _row(is_correct=None, verify_score=None, verify_history=ledger),      # 已回溯：不反推结论
        _row(is_correct=None, verify_score=None, verify_history=[]),          # 未验证：不该被碰
    ])
    assert [(p.id, f, have, want) for p, f, have, want in fixable] == [
        (1, 'verify_score', 49, 100)]
    assert manual == []


def test_find_desynced_syncs_the_duplicated_endpoint_date():
    """m-1 的遗留行：同一端点在 `current_nav_date` / `end_nav_date` 里是两个日期。"""
    ledger = [{'date': '2026-06-08', 'score': 100, 'is_correct': True, 'change': 1.0}]
    same_nav = _row(is_correct=True, verify_score=100, verify_history=ledger,
                    end_nav_date=date(2026, 6, 7), current_nav_date=date(2026, 6, 8))
    fixable, manual = find_desynced([same_nav])
    assert [(p.id, f, have, want) for p, f, have, want in fixable] == [
        (1, 'current_nav_date', date(2026, 6, 8), date(2026, 6, 7))]
    assert manual == []

    # 两个字段连数值都不是同一个 ⇒ 描述的不是同一件事，只报不改
    other = _row(id=2, is_correct=True, verify_score=100, verify_history=ledger,
                 end_nav_date=date(2026, 6, 7), current_nav_date=date(2026, 6, 8),
                 current_nav=1.9)
    assert find_desynced([other])[0] == []
    assert len(find_desynced([other])[1]) == 1


def test_find_desynced_refuses_a_ledger_entry_from_another_observation():
    """人工确认只改标量、不追加台账（`prediction_service.verify`）⇒ 末条属于另一次观察。

    不锚定这一点，`--apply` 会把无关窗口的分数写进这条预测并重算博主均分（第 16 轮 m-3）。
    """
    ledger = [{'date': '2026-06-08', 'score': 100, 'is_correct': True, 'change': 1.0}]
    manual_row = _row(id=3, is_correct=True, verify_score=60, verify_history=ledger,
                      actual_change=9.9)          # 人工填的实际涨跌幅
    fixable, manual = find_desynced([manual_row])
    assert fixable == [], '台账与标量不是同一次观察，不该自动改'
    assert '不是同一次观察' in manual[0][1]


def test_find_desynced_refuses_to_guess_when_evidence_contradicts():
    """台账与标量结论方向相反、或根本没有台账：只报，不改。"""
    rows = [
        _row(id=11, is_correct=True, verify_score=20,
             verify_history=[{'score': 90, 'is_correct': False, 'change': 1.0}]),
        _row(id=12, is_correct=True, verify_score=20, verify_history=[]),
        _row(id=13, is_correct=True, verify_score=None,
             verify_history=[{'score': 90, 'is_correct': True, 'change': 1.0}]),
        _row(id=14, is_correct=True, verify_score=20,
             verify_history=[{'is_correct': True, 'change': 1.0}]),
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


def test_offline_replay_defers_llm_leg_rows_instead_of_excusing_them(db_session):
    """末轮走过 LLM 复核那一腿的行：离线重放**让出去**，不许算进"可解释"。

    第 16 轮 MAJOR-1 证伪了我上一版的"可解释桶"：它的条件全是旧值侧证据，对重放
    出来的值毫无约束 ⇒ 确定性判据整体坏掉（把边界行算成 0 分）也会被吞掉，
    而盲区恰好覆盖闸门最该盯的边界行（那一腿只在 20~80 分/阈值附近/方向相反时介入）。
    """
    from scripts.replay_verifications_on_copy import _sample, last_entry_is_llm_verify

    deterministic = _seed_verified(db_session)
    prediction = _seed_verified(db_session, code='RT9998', label='第二条')
    prediction.verify_history = list(prediction.verify_history) + [
        {'date': '2026-06-15', 'score': 100, 'is_correct': True, 'verify_type': 'llm_verify'}]
    db_session.commit()

    assert last_entry_is_llm_verify(prediction) is True
    assert last_entry_is_llm_verify(deterministic) is False

    rows, deferred = _sample(db_session, Prediction, 100, [], offline=True)
    deferred_ids = [p.id for p in deferred]
    assert prediction.id in deferred_ids, '末轮走过 LLM 那一腿的行没被让出去'
    assert deterministic.id not in deferred_ids
    assert deterministic.id in [p.id for p in rows]
    assert prediction.id not in [p.id for p in rows], "离线重放把没法比对的行放进去了"
    # 不带 --offline（LLM 那一腿可复现）时必须回到抽样池里，别让它们永久逃检
    rows_online, deferred_online = _sample(db_session, Prediction, 100, [], offline=False)
    assert deferred_online == [] and prediction.id in [p.id for p in rows_online]
