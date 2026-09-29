# -*- coding: utf-8 -*-
"""`backfill_viewpoint_archive_stamps.py` 的判据（任务 #142 的第二半：给存量卡住的行补那一对）。

代码那一半（AI 判拒绝时一起写 `deleted_at` / `restore_before`）由
`test_viewpoint_refactor.py` 钉；这个文件钉的是**存量**那一半 —— 镜像现读 418 行
`is_deleted=True` 而缺那一列（命令与分组见 `AGENTS.md` 基线那一段），而且**缺的不是同一列**：
18 行连 `deleted_at` 都没有 ⇒ 线上唯一会删观点行的那把尺子
（`retention_three_buckets._deleted_viewpoint_ids`）要求 `deleted_at.isnot(None)`，
所以这一组**永远进不了清理桶**，改代码不会让已经存在的行自愈；
400 行有 `deleted_at`、只缺 `restore_before` ⇒ 它们今天已在候选里，缺的只是"保留到哪天"那句话。
⇒ 两组的后果相反，`gap_kinds` 必须分开交回来，脚本那句话也不许并成一句。

要钉住的五件事：
① 只补"缺那一列"的行，**已经有值的一行都不覆盖**（别人手动归档的署名与日期不是我的靶子）；
② "补哪一天"是**决定不是默认值**：`--stamp-from today` 让窗口从今天重算、
   `created` 让早就过期的行当场成为清理候选 ⇒ 两种结果都在清单里，`--apply` 必须点名，
   而**选中与否由那把真尺子回答**（判据直接跑 `ThreeBucketRetentionService.build_plan()`，
   不读脚本自己印的那句话）；
③ 保留天数**只有 `ThreeBucketPolicy().deleted_viewpoint_days` 一个出处**（改成写死 30 当场红
   —— 那是 M71 的载荷）；
④ 缺 `created_at` 的行**拒绝补**，不许拿今天或 `viewpoint_date` 冒充它进来的那天；
⑤ 默认 dry-run 一个字都不写、缺确认词/缺点名在**连库之前**退出，且能按备份原样还原；
⑥ **两种缺口分开分组**，并且由真清理尺子作证：只缺 `restore_before` 的行今天已在删除候选里，
   两列都缺的那一组才永远进不了桶（并成一句"缺那一对"就会对前一组说反话）。
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'scripts', 'backfill_viewpoint_archive_stamps.py')


def _import_script():
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    import backfill_viewpoint_archive_stamps as mod
    sys.path.pop(0)
    return mod


@pytest.fixture(autouse=True)
def _no_backup_residue_in_the_repo(tmp_path, monkeypatch):
    """夹具真跑 `--apply` 时备份只许落 tmp（与 `CLOSE_BACKUP_DIR` 同一课，第 55 轮卫生账）。"""
    repo = os.path.join(ROOT, 'backup')
    before = set(os.listdir(repo)) if os.path.isdir(repo) else set()
    monkeypatch.setattr(_import_script(), 'BACKUP_DIR', str(tmp_path / 'backup'), raising=True)
    monkeypatch.setenv('VP_BACKFILL_BACKUP_DIR', str(tmp_path / 'env'))
    yield
    after = set(os.listdir(repo)) if os.path.isdir(repo) else set()
    assert after == before, (
            '用例往仓库级 `backup/` 落了文件（%s）⇒ 备份写到了仓库而不是 tmp'
            % sorted(after - before))


def _seed(test_db, *, content='情绪表达', deleted=True, deleted_at=None,
          restore_before=None, created_at='now', summary='rejected:情绪表达'):
    from src.models.database import Viewpoint
    created = (datetime.now() if created_at == 'now' else created_at)
    v = Viewpoint(fund_code='TEST01', content=content, author='测试',
                  source='manual', viewpoint_date=date.today() - timedelta(days=60),
                  analysis_summary=summary, is_deleted=deleted, deleted_at=deleted_at,
                  restore_before=restore_before)
    if created is not None:
        v.created_at = created
    test_db.add(v)
    test_db.commit()
    if created is None:
        # 列有默认值 ⇒ 造"压根没有 created_at"这一格只能建完之后清掉再提交
        test_db.execute(__import__('sqlalchemy').text(
            'update viewpoints set created_at=null where id=:i'), {'i': v.id})
        test_db.commit()
    test_db.refresh(v)
    return v


def _today():
    from src.services.prediction_lifecycle import current_as_of
    return current_as_of()


def _candidates_selected(test_db, today):
    """那把**真尺子**今天选中了哪些「回收站观点」行（不读脚本自己印的话）。"""
    from src.services.retention_three_buckets import ThreeBucketRetentionService
    plan = ThreeBucketRetentionService(test_db, today=today).build_plan()
    return set(plan.candidate_ids[ThreeBucketRetentionService.BUCKET_DELETED_VP])


def test_only_rows_missing_the_pair_are_candidates_and_an_existing_value_is_never_overwritten(test_db):
    mod = _import_script()
    legacy = _seed(test_db)
    stamped = _seed(test_db, content='广告引流',
                    deleted_at=datetime.now() - timedelta(days=5),
                    restore_before=_today() + timedelta(days=25))
    live = _seed(test_db, content='深度分析', deleted=False)

    got = [v.id for v in mod.candidates(test_db)]
    assert got == [legacy.id], (
            '候选名单把%s算进来了 ⇒ "缺那一列"这把尺子量的不是那一列'
            % ('已带戳的行' if stamped.id in got else '没进回收站的行'))

    retention = _retention_days()
    items, skipped = mod.plan_for(test_db, mod.candidates(test_db), 'today', retention, _today())
    mod.apply_backfill(test_db, items, retention, _today(), 'today')
    test_db.refresh(stamped)
    assert stamped.deleted_at is not None and stamped.restore_before is not None
    test_db.refresh(live)
    assert live.deleted_at is None and live.restore_before is None


def test_the_two_stamp_choices_differ_on_the_real_ruler_not_on_the_scripts_arithmetic(test_db):
    """`created` 让过期行**当场**进清理候选，`today` 让它再等满 N 天 —— 由真尺子回答。"""
    mod = _import_script()
    retention = _retention_days()
    old = _seed(test_db, created_at=datetime.now() - timedelta(days=retention + 10))
    fresh = _seed(test_db, content='新闻转述',
                  created_at=datetime.now() - timedelta(days=1))
    today = _today()

    items, skipped = mod.plan_for(test_db, mod.candidates(test_db), 'created', retention, today)
    assert skipped == []
    assert [i['viewpoint_id'] for i in items] == [old.id, fresh.id]
    # 「进来于 N+10 天」这一行按 created 补 ⇒ 保留日已经过了；昨天那行还没过
    assert [i['until'] <= today for i in items] == [True, False]

    mod.apply_backfill(test_db, items, retention, today, 'created')
    selected = _candidates_selected(test_db, today)
    assert old.id in selected, (
            '按 created 补完，那把真尺子仍然选不中它 ⇒ "补了就出清理桶"是脚本自己算的话')
    assert fresh.id not in selected

    # 同样的两行，换成 today 那一个选项就一行都不该被选中（窗口从今天重算）
    mod2 = _import_script()
    test_db.execute(__import__('sqlalchemy').text(
        'update viewpoints set deleted_at=null, restore_before=null where id in (:a,:b)'),
        {'a': old.id, 'b': fresh.id})
    test_db.commit()
    items2, _ = mod2.plan_for(test_db, mod2.candidates(test_db), 'today', retention, today)
    mod2.apply_backfill(test_db, items2, retention, today, 'today')
    assert not (_candidates_selected(test_db, today) & {old.id, fresh.id}), (
            '按 today 补（窗口重新算）却当场进了清理候选 ⇒ 恢复窗口被算没了')


def _retention_days():
    from src.services.retention_three_buckets import ThreeBucketPolicy
    return ThreeBucketPolicy().deleted_viewpoint_days


def test_the_retention_window_is_the_policys_number_not_a_hard_coded_thirty(test_db, monkeypatch):
    """M71 的载荷：把 `deleted_viewpoint_days` 改掉，补出来的"保留到哪天"必须跟着动。

    问的是**脚本自己取的那个数**（`policy_retention_days()`），不是我这里递进去的参数：
    只断"我传 47 它就算 47"的话，把 `main()` 里那句换成写死 30 也不会红。
    """
    mod = _import_script()
    from src.services import retention_three_buckets as r3
    real_policy = r3.ThreeBucketPolicy
    monkeypatch.setattr(r3, 'ThreeBucketPolicy',
                        lambda *a, **k: real_policy(deleted_viewpoint_days=47))
    retention = mod.policy_retention_days()
    assert retention == 47, (
            '三桶策略已经换成 47 天，脚本取到的还是 %s ⇒ 它里面藏着第二个保留天数'
            '（要么写死、要么自己加了一次算式）' % retention)
    v = _seed(test_db, content='无关内容')
    today = _today()
    items, _ = mod.plan_for(test_db, mod.candidates(test_db), 'today', retention, today)
    mod.apply_backfill(test_db, items, retention, today, 'today')
    test_db.refresh(v)
    assert v.restore_before == today + timedelta(days=47), (
            '保留日不是"今天 + 策略那个数" ⇒ 计划与落笔用的不是同一份保留天数')


def test_a_row_that_cannot_say_when_it_arrived_is_refused_not_invented(test_db):
    """`--stamp-from created` 遇上没有 `created_at` 的行：跳过并说出原因，不拿别的日期顶。"""
    mod = _import_script()
    ghost = _seed(test_db, content='广告引流', created_at=None)
    assert ghost.created_at is None
    items, skipped = mod.plan_for(test_db, [ghost], 'created', _retention_days(), _today())
    assert items == [] and [s[0] for s in skipped] == [ghost.id]
    assert 'viewpoint_date' not in skipped[0][1], '那句原因不许把别的日期当药方'
    test_db.refresh(ghost)
    assert ghost.deleted_at is None and ghost.restore_before is None


def test_a_second_run_is_a_no_op_and_the_backup_restores_the_exact_previous_shape(test_db, capsys):
    mod = _import_script()
    v = _seed(test_db)
    today = _today()
    items, _ = mod.plan_for(test_db, mod.candidates(test_db), 'created', _retention_days(), today)
    path = mod.apply_backfill(test_db, items, _retention_days(), today, 'created')
    test_db.refresh(v)
    stamped, until = v.deleted_at, v.restore_before

    capsys.readouterr()
    items2, _ = mod.plan_for(test_db, mod.candidates(test_db), 'created', _retention_days(), today)
    assert items2 == [], '第二轮还把这行当"要补的"⇒ candidates 那把尺子补完不收敛'
    out = capsys.readouterr().out
    assert '[没写]' not in out  # 第二轮压根不在名单里，不靠"补的时候再拦"

    # 还原 = 回到 NULL（这一族行本来就卡着），不是"再算一次今天"
    assert mod.restore(test_db, path, True) == 0
    test_db.refresh(v)
    assert v.deleted_at is None and v.restore_before is None
    with open(path, encoding='utf-8') as fh:
        payload = json.load(fh)
    assert payload['plan'][0]['had_deleted_at'] is None


def test_the_write_is_guarded_before_the_database_is_touched():
    """缺确认词 / 缺 `--stamp-from` 必须在**连库之前**退 4（用法错不该先付一次连接的代价）。"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    for flags in (['--apply'], ['--apply', '--confirm', 'WRONG']):
        proc = subprocess.run([sys.executable, SCRIPT] + flags, capture_output=True,
                              encoding='utf-8', errors='replace', env=env, cwd=ROOT)
        assert proc.returncode == 4, (
                '%s 退码 %s：%s ⇒ 这两道门有一道排在连库之后就白给了'
                % (flags, proc.returncode, proc.stdout[-300:]))
        assert '[abort]' in proc.stdout
        assert '[target]' not in proc.stdout, (
                '它已经去认库并自报了目标 ⇒ 确认门排在连库之后（这里要的是先拒用法）')


def test_the_two_gaps_are_not_one_gap_and_the_cleaner_only_ever_sees_one_of_them(test_db):
    """缺 `deleted_at` 与只缺 `restore_before` 是**两种后果**，那把真清理尺子替这件事作证。

    镜像现读 418 行里两组都有（18 / 400），所以这不是理论形状：把两句并成一句
    "缺那一对时间戳"就会对 400 行说反话（它们今天已经在删除候选里，补 `restore_before`
    不会让它们更晚被删），而对 18 行说轻话（它们才是"永远进不了桶"的那一组）。
    """
    mod = _import_script()
    today = _today()
    both = _seed(test_db, content='广告引流')
    only_deadline = _seed(test_db, content='情绪表达2',
                          deleted_at=datetime.now() - timedelta(days=999))

    no_stamp, no_deadline = mod.gap_kinds(mod.candidates(test_db))
    assert [v.id for v in no_stamp] == [both.id] and [v.id for v in no_deadline] == [only_deadline.id], (
            '分组看的是"缺哪一列"，不是"缺不缺"⇒ 这一格并错了组，脚本那句现状就是反话')

    got = _candidates_selected(test_db, today)
    assert only_deadline.id in got, (
            '有 `deleted_at`、只缺 `restore_before` 的行不在清理候选里 ⇒ '
            '"它们今天已在候选里"这句话是假的，得按真尺子改')
    assert both.id not in got, (
            '两个时间戳都没有的行进了清理候选 ⇒ `_deleted_viewpoint_ids` 那把尺子改了，'
            '那么"代码只修未来的行"这条整段账要重写')
