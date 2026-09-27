# -*- coding: utf-8 -*-
"""`close_unknowable_predictions.py` 的判据（把"永远判不出来"的预测收进回收站）。

要钉住的是**三条证据都要在场**这件事：
① 现场问数据源、这个窗口答 **0 条**；② 库里这只代码最后一条净值**早于窗口起点**；
③ 这条行**以前被结构性重问锁压过**（`was_locked_previously`，与验证器同一把尺子）——
脚本这一次现问才算第二次，回收站里那句"已问过两次"才写得出口（第 52 轮 A-4 实测：
旧写法收掉的 5 行全都从没被锁过，那句话是写多的）。
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


def _seed(db, *, code, target_offset, nav_back_days, pred_type='up', held=False):
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
    if held:
        # 上一轮验证器真问过、写过一把**已经到点**的重问锁（那根日期晚于自己的目标日 = 只有
        # 锁会写在那儿；且必须 ≤ 今天，否则锁还没到点，脚本这一轮就不该关）
        # ⇒ 脚本这一次现问才算"第二次"，那句"已问过两次"才写得出口。
        # 第 53 轮 A-2：这一档原来写成 `target_date + 30 天`（锁在**未来**），
        # 正向用例于是替"锁没到点也关"这个错行为作了保。
        p.next_verify_date = today - timedelta(days=1)
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


@pytest.fixture(autouse=True)
def _no_backup_residue_in_the_repo(tmp_path, monkeypatch):
    """跑这个文件里的任何用例，都不许往仓库级 `backup/` 落一份文件（第 55 轮的卫生账）。

    上一版只有两条用例记得 `os.remove(path)`，另外两条（`apply_close` 在 85 / 131 行）
    关掉之后就把备份留在仓库里 —— 2026-09-27 数过一次：`backup/` 里当天 48 份
    `close-unknowable-*.json` 全部只装着夹具那一行（复核
    `grep -l 测试基金 backup/close-unknowable-*.json | wc -l`）。
    那些文件名**只到秒**，两个会话同时跑就会互相盖掉对方的"回滚载荷"，
    而它长得和真备份一模一样 —— 第 34 轮 `write_manifest` 那一族的复发。

    机制：① 进程内那份模块常量改指 `tmp_path`；② 子进程走脚本认的 `CLOSE_BACKUP_DIR`；
    ③ 收尾再对一次仓库目录，漏一个文件就红（把 ① 撤掉复跑，这条就会红 —— 它不是装饰）。
    """
    repo = os.path.join(ROOT, 'backup')
    before = set(os.listdir(repo)) if os.path.isdir(repo) else set()
    monkeypatch.setattr(_import_script(), 'BACKUP_DIR',
                        str(tmp_path / 'backup'), raising=True)
    monkeypatch.setenv('CLOSE_BACKUP_DIR', str(tmp_path))
    yield
    after = set(os.listdir(repo)) if os.path.isdir(repo) else set()
    assert after == before, (
            '用例往仓库级 `backup/` 落了文件（%s）⇒ 备份写到了仓库而不是 tmp，'
            '而"跑完全量后要 git status 看有没有新残渣"这条又只能靠人记'
            % sorted(after - before))


def test_both_evidences_present_closes_and_leaves_the_verdict_untouched(test_db, monkeypatch):
    mod = _import_script()
    p = _seed(test_db, code='DEAD99', target_offset=3, nav_back_days=400, held=True)
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


def test_the_archive_stamp_and_the_restore_deadline_come_from_the_beijing_clock(test_db, monkeypatch):
    """归档时间戳与"保留到哪天"必须出自同一把北京钟（第 53 轮 A-11）。

    本机看不见这一格（这台机器就在 +8）⇒ 只有**故意让墙钟与北京钟差一天**才测得到。
    Render 的容器在 UTC：北京 00:00~08:00 关掉的行，`date.today()` 会少一天 ⇒
    回收站里"保留到 X 日"比页面上的"截至日"早一天，而恢复下界正是拿它算的。
    """
    from datetime import datetime as real_datetime

    import src.services.prediction_service as ps
    from src.models.database import Prediction
    from src.services import prediction_lifecycle as lc

    mod = _import_script()
    beijing = date(2026, 9, 27)
    monkeypatch.setattr(lc, 'current_as_of', lambda: beijing)

    class ConflictDate(date):
        @classmethod
        def today(cls):
            return beijing - timedelta(days=1)

    class ConflictDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 9, 26, 23, 30)

    monkeypatch.setattr(ps, 'date', ConflictDate)
    monkeypatch.setattr(ps, 'datetime', ConflictDateTime)

    p = _seed(test_db, code='DEAD98', target_offset=3, nav_back_days=400, held=True)
    _stub_source(monkeypatch, {'DEAD98': []})
    items, _skipped = mod.plan(test_db, beijing)
    mod.apply_close(test_db, items, beijing)

    row = test_db.query(Prediction).filter(Prediction.id == p.id).first()
    assert row.restore_before == beijing + timedelta(days=30), (
        '恢复下界又去问墙钟了 ⇒ 两把钟冲突时会少一天')
    assert row.deleted_at.date() == beijing, (
        '归档时刻的**日期**必须与页面上的"截至日"同一把钟：%s' % row.deleted_at)


def test_a_two_ask_claim_is_only_allowed_when_the_row_can_produce_the_lock(test_db):
    """回收站里那句"已问过两次"逐行问一句：这一行**当时真被锁过吗**（第 53 轮 B-6）。

    A-4 修之前那一版收口脚本第一次问出来就关了，于是镜像 5 行带着这句话躺在回收站里，
    而台账前像显示它们的 `next_verify_date` 全部 ≤ 自己的目标日（只有重问锁会写在那天之后）。
    生产那 15 行问过一遍：日期确实晚于目标日 ⇒ 话是真的 ⇒ 这条订正必须一行都不动它们。
    """
    mod = _import_script()

    def _archive(code, *, locked, by='system'):
        p = _seed(test_db, code=code, target_offset=5, nav_back_days=400)
        p.is_deleted = True
        p.deleted_by = by
        p.next_verify_date = (p.target_date + timedelta(days=30)) if locked \
            else p.target_date            # 没锁过：那根日期还在目标日或其之前
        p.delete_reason = ('标的 %s 的数据源给不出这段净值，%s ⇒ 无法判定，'
                           '既不算判对也不算判错，不计入准确率' % (code, mod.UNPROVEN_CLAIM))
        test_db.commit()
        return p

    unproven = _archive('LIE001', locked=False)
    proven = _archive('TRUE01', locked=True)
    manual = _archive('USER001', locked=False, by='user')   # 老板自己写的句子，不归我订正

    found = mod.find_unproven_claims(test_db)
    assert [p.id for p, _o, _n in found] == [unproven.id], (
        '订正名单与"拿不出证据的行"对不上 ⇒ 要么漏了说谎的那行，要么动了有据的那行')
    for _p, old, new in found:
        assert mod.UNPROVEN_CLAIM not in new and '没有更早的重问记录' in new
        assert '不计入准确率' in new, '改口不许顺手把准确率那句说明删掉'
    test_db.refresh(proven)
    test_db.refresh(manual)
    assert mod.UNPROVEN_CLAIM in proven.delete_reason
    assert mod.UNPROVEN_CLAIM in manual.delete_reason


def test_rewriting_the_sentence_touches_only_that_column(test_db, monkeypatch):
    """订正默认 dry-run 一行都不改；真改只动 `delete_reason`，并且先留下带原句的备份。"""
    import json

    mod = _import_script()
    p = _seed(test_db, code='LIE002', target_offset=5, nav_back_days=400)
    p.is_deleted = True
    p.deleted_by = 'system'
    p.next_verify_date = p.target_date
    p.delete_reason = '数据源给不出这段净值，%s ⇒ 不计入准确率' % mod.UNPROVEN_CLAIM
    test_db.commit()
    before = {c.key: getattr(p, c.key) for c in p.__table__.columns}

    found = mod.find_unproven_claims(test_db)
    assert [x[0].id for x in found] == [p.id]
    # 这一支**不问数据源**：改一句文案不该背 15 次外呼。真去问会被 conftest 的零网络闸
    # 当场打死（`BlockedRealHttp`），所以这里故意不注入任何桩 —— 它响了就是这一支写歪了。
    path = mod.apply_reword(test_db, found, date.today())
    try:
        test_db.refresh(p)
        payload = json.load(open(path, encoding='utf-8'))
        assert payload['plan'][0]['old'] == before['delete_reason'], '备份里要留着原句'
        assert mod.UNPROVEN_CLAIM not in p.delete_reason
        for key, value in before.items():
            if key == 'delete_reason':
                continue
            assert getattr(p, key) == value, '订正只许动 delete_reason，%s 被碰了' % key
        from src.models.database import PredictionChangeLog
        log = test_db.query(PredictionChangeLog).filter(
            PredictionChangeLog.prediction_id == p.id).order_by(
            PredictionChangeLog.id.desc()).first()
        assert log is not None and log.action == 'archive_note_fixed'
        assert log.changed_fields == ['delete_reason'], log.changed_fields
    finally:
        os.remove(path)


def test_a_row_never_locked_before_is_not_closed(test_db, monkeypatch):
    """第三条证据：这条行**以前没被锁过** ⇒ 脚本这一问就是第一次，不关（第 52 轮 A-4）。

    实测过的形状：2026-09-27 镜像上脚本收掉的 5 行 `next_verify_date` 全部 ≤ 目标日
    （＝从没被结构性锁过），而它们回收站里那句原因统统写着"已问过两次仍无答案"。
    判"问过几次"的尺子不许有两份 ⇒ 这里直接复用验证器那把 `was_locked_previously`。
    """
    mod = _import_script()
    p = _seed(test_db, code='FIRST99', target_offset=3, nav_back_days=400)
    _stub_source(monkeypatch, {'FIRST99': []})

    items, skipped = mod.plan(test_db, date.today())

    assert items == [], '第一次问出来就关 ⇒ 那句"已问过两次"又是写多的'
    assert [s[0] for s in skipped] == [p.id] and '只该上锁' in skipped[0][2]
    test_db.refresh(p)
    assert p.is_deleted is False


def test_a_lock_that_has_not_expired_yet_is_not_closed(test_db, monkeypatch):
    """锁**还没到点**的行不许被脚本关掉（第 53 轮 A-2：脚本原来只抄了判据的一半）。

    `should_close_as_stale_target` 里有"上一轮被锁过"与"锁已到点"两道，上一版脚本只抄前者
    ⇒ 实测同一行（锁 2026-10-24、今天 09-27）验证器 False、脚本"可关 1 条"。
    现在整条判据交回验证器那个函数，少任何一道都关不成。
    """
    mod = _import_script()
    p = _seed(test_db, code='FUTU99', target_offset=3, nav_back_days=400, held=True)
    p.next_verify_date = date.today() + timedelta(days=30)     # 锁还在未来
    test_db.commit()
    _stub_source(monkeypatch, {'FUTU99': []})

    items, skipped = mod.plan(test_db, date.today())

    assert items == [], '锁没到点 ⇒ 这一轮连"第二次"都还没成立'
    assert [s[0] for s in skipped] == [p.id] and '还没到点' in skipped[0][2]


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
    p = _seed(test_db, code='REST99', target_offset=3, nav_back_days=400, held=True)
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


def test_restore_refuses_to_overwrite_a_row_somebody_else_touched(test_db, monkeypatch):
    """还原 = 撤销**我这一次**动作，不是"把行盖回备份"（第 53 轮 B-3 / 任务 #117）。

    中间如果这一行被别人动过（改过原因、换了署名、甚至已经放回活跃列表），
    旧的写法只按 id 盖回去 ⇒ 老板手动归档的署名与他写的原因被无声清掉，台账还自称 `user`。
    现在必须先比对现状，不符就整行拦下来；只有显式 `--force-restore` 才硬盖。
    """
    from src.models.database import Prediction

    mod = _import_script()
    p = _seed(test_db, code='RSVR99', target_offset=3, nav_back_days=400, held=True)
    _stub_source(monkeypatch, {'RSVR99': []})
    items, _ = mod.plan(test_db, date.today())
    path = mod.apply_close(test_db, items, date.today())
    test_db.refresh(p)
    assert p.is_deleted is True

    # 有人在关闭之后往这句话后面补了内容 ⇒ 现状与备份不一致
    p.delete_reason = p.delete_reason + '（老板补的一句）'
    test_db.commit()

    try:
        assert mod.restore(test_db, path, apply_it=True) == 4, '现状不符时必须拒还原'
        test_db.refresh(p)
        assert p.is_deleted is True, '拦下来的行一行都不许动'
        assert '老板补的一句' in p.delete_reason, '拦下来还不算完：那句话不能被盖掉'

        # 署名不是 system ⇒ 同样拦（这一行已经不是"我关的"了）
        p.delete_reason = items[0]['note']          # 把上一处差异修回去，只留署名这一处
        p.deleted_by = 'user'
        test_db.commit()
        assert mod.restore(test_db, path, apply_it=True) == 4
        test_db.refresh(p)
        assert p.deleted_by == 'user' and p.is_deleted is True

        # 显式硬盖才放行，并且要说清拦了几行
        assert mod.restore(test_db, path, apply_it=True, force=True) == 0
        test_db.refresh(p)
        assert p.is_deleted is False and p.deleted_by is None
    finally:
        os.remove(path)


def test_fix_wording_dry_run_leaves_the_row_alone_and_says_so(tmp_path):
    """`--fix-wording` 的 dry-run 必须**一行都不改**，并且退码要说"这是 dry-run"（第 54 轮 A-7）。

    这一支以前只有内部函数用例（`apply_reword` 直接调）⇒ CLI 层零判据：
    哪天有人把 `if not args.apply` 写成 `if False`，一次"先看一眼"的调用就会真改生产回收站，
    而全套绿灯一声不响。退码 2 也是契约的一部分（`sweep_sector_mappings.py` 同规）：
    脚本"算了但没动手"与"动手了"必须在退码上分得开，否则跑批日志里两者长得一样。
    """
    copy = tmp_path / 'copy-fixwording.db'
    url = 'sqlite:///' + copy.as_posix()
    # 夹具写成文件、不用 `python -c`：内联字符串要过 bash + python 两层转义，
    # 而"转义写错把源码截断"在这个仓里已经付过一次账。
    seed = tmp_path / 'seed.py'
    peek = tmp_path / 'peek.py'
    seed.write_text(u'''# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, %r)
os.environ["DATABASE_URL"] = %r
from datetime import date, timedelta
from src.models.database import (Base, Blogger, Post, Prediction, SessionLocal,
                                 engine)

Base.metadata.create_all(engine)
db = SessionLocal()
b = Blogger(name=u"文案订正博主")
p = Post(blogger=b, title=u"t", content=u"c", post_date=date.today())
db.add_all([b, p])
db.flush()
db.add(Prediction(
    blogger_id=b.id, post_id=p.id, fund_code="900001", fund_name=u"样例",
    sector=u"测试", prediction_type="up", status="pending",
    prediction_date=date.today() - timedelta(days=9),
    prediction_period=u"1周",
    target_date=date.today() - timedelta(days=3),
    # 那根日期**早于**自己的目标日 ⇒ 它是创建期排出来的，不是重问锁写下的
    # （`was_locked_previously` 只认晚于目标日的那一种）⇒ 这句"已问过两次"拿不出证据。
    next_verify_date=date.today() - timedelta(days=4),
    delete_reason=u"数据源给不出这段净值，已问过两次仍无答案 ⇒ 不计入准确率",
    deleted_by="system", deleted_at=date.today(),
    restore_before=date.today() + timedelta(days=30), is_deleted=True))
db.commit()
db.close()
print("seeded")
''' % (ROOT, url), encoding='utf-8')
    peek.write_text(u'''# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, %r)
os.environ["DATABASE_URL"] = %r
from src.models.database import Prediction, SessionLocal

db = SessionLocal()
print(u"|".join((p.delete_reason or u"") for p in db.query(Prediction)))
''' % (ROOT, url), encoding='utf-8')

    def _run(script, *extra):
        # `CLOSE_BACKUP_DIR` ⇒ 备份写进 `tmp_path`，不落仓库级 `backup/`（第 55 轮小三条）。
        # 这条用例真起子进程跑 `--apply`，而备份文件名只到秒：不设这个出口，
        # 每跑一次基线就在仓库里留一份没人收的 JSON，并与别的会话同名相撞。
        env = dict(os.environ, PYTHONIOENCODING='utf-8', DATABASE_URL=url,
                   CLOSE_BACKUP_DIR=str(tmp_path))
        return subprocess.run([sys.executable, script] + list(extra),
                              capture_output=True, text=True, env=env, cwd=ROOT,
                              timeout=300, encoding='utf-8', errors='replace')

    r = _run(str(seed))
    assert r.returncode == 0, '夹具没建起来：%s' % (r.stdout + r.stderr)[-400:]

    def _reason():
        q = _run(str(peek))
        assert q.returncode == 0, '读不回执失败：%s' % (q.stdout + q.stderr)[-300:]
        return q.stdout.strip().splitlines()[-1]

    before = _reason()
    assert '已问过两次' in before, '夹具形状不对：这句话本来就该是被订正的对象'

    dry = _run(SCRIPT, '--fix-wording')
    assert dry.returncode == 2, 'dry-run 的退码必须是 2（动手那一支才是 0）：%s' % dry.stdout[-400:]
    assert '[dry-run]' in dry.stdout and '一行都没动' in dry.stdout
    assert _reason() == before, 'dry-run 把回收站里那句话改了 ⇒ "先看一眼"会动手'

    real = _run(SCRIPT, '--fix-wording', '--apply', '--confirm', 'CLOSE-UNVERIFIABLE')
    assert real.returncode == 0, '真订正没走通：%s' % real.stdout[-500:]
    after = _reason()
    # 判据是"那句断言换成了那句有据的话"，不是"旧短语一个字符都不出现"——
    # 订正后的话里会**引用**旧说法解释为什么改，拿子串判会把自己判红。
    assert after != before and '没有更早的重问记录' in after, \
        '--apply 之后话没被改 ⇒ 这一支其实什么都没做'
    assert '已问过两次仍无答案 ⇒' not in after, '那句没出处的断言还挂着 ⇒ 只加了注释没改掉它'

    # 备份这一腿也要有牙：真订正必须留下**能用**的原句（仓库里不许留残渣，
    # 由文件顶上那条 autouse 夹具统一管，这里不重复数第二遍）。
    landed = sorted((tmp_path / 'backup').glob('archive-note-*.json'))
    assert landed, '订正完没留下备份 ⇒ 原句丢了，"可随时回滚"是空话'
    assert '900001' in landed[0].read_text(encoding='utf-8'), \
        '备份里没有样例那一行的原句 ⇒ 这份备份还原不了任何东西'


def test_cli_refuses_before_touching_the_database():
    """缺确认词、以及"在镜像上喊 --production"，都要**在连库与取数之前**退 4。

    两格都把 `DATABASE_URL` 钉成 sqlite：即便哪天 `.env` 指向生产，这条用例也**不可能**
    在生产上写一行（`load_dotenv` 默认不覆盖已有变量）。
    """
    env = dict(os.environ, PYTHONIOENCODING='utf-8',
               DATABASE_URL='sqlite:///' + os.path.join(ROOT, 'data', 'copy-cli-close-unk.db'))
    for args, expect_word in [
        (['--apply'], 'CLOSE-UNVERIFIABLE'),
        (['--fix-wording', '--apply'], 'CLOSE-UNVERIFIABLE'),
        (['--production', '--apply', '--confirm', 'CLOSE-UNVERIFIABLE'], '线上'),
    ]:
        r = subprocess.run([sys.executable, SCRIPT] + args,
                           capture_output=True, text=True, env=env, cwd=ROOT, timeout=300,
                           encoding='utf-8', errors='replace')   # 父进程默认 cp936，中文回执会解不开
        assert r.returncode == 4, '%s ⇒ 退码 %s（应为 4）：%s' % (args, r.returncode, r.stdout)
        assert expect_word in (r.stdout + r.stderr), (args, r.stdout)


def test_a_rewording_backup_is_not_a_closure_backup(test_db):
    """`--fix-wording` 留下的那份备份喂给 `--restore-from` 必须**被拒**（第 54 轮 B-1 / A-8）。

    旧写法会真的"还原"：那份备份的 `plan[]` 里没有 `note` ⇒ "行上原因与关闭时不一样"
    那道比对结构性永不触发，而其余三道（在回收站 / 署名 system / 无结论）对**订正过的行**
    全部为真 ⇒ `restore_prediction()` 一把把该留在回收站的行放回活跃列表，退码还是 0。
    评审席在临时 sqlite 上实测跑出了这一步（`[还原] 1 / 1 行已回到活跃列表（拦下 0 行）`）。
    文案订正的反向是"把 old 写回那一列"，与"撤销关闭"不是一件事。
    """
    import json

    mod = _import_script()
    p = _seed(test_db, code='RWD99', target_offset=3, nav_back_days=400, held=True)
    # 一行已经在回收站、署名 system、带着订正过的话 —— 这正是 `--fix-wording` 会留下的形状
    p.is_deleted, p.deleted_by, p.delete_reason = True, 'system', '按区间问过仍无答案（已订正）'
    test_db.commit()
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..',
                                        'data', '_review_tmp_reword.json'))
    payload = {'created_at': str(date.today()), 'as_of': str(date.today()),
               'reason_kind': 'unproven_two_ask_claim',
               'rows': [{'id': p.id}],
               'plan': [{'prediction_id': p.id, 'old': '旧句', 'new': '新句'}]}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
    try:
        assert mod.restore(test_db, path, apply_it=True) == 4, '文案备份不许当关闭备份还原'
        test_db.refresh(p)
        assert p.is_deleted is True, (
            '它把行放回了活跃列表 ⇒ 老板在回收站里点"恢复"之前，这一行已经被"还原"过一次了')
    finally:
        os.remove(path)


def test_a_fund_with_no_nav_rows_at_all_does_not_get_told_it_is_covered(test_db, monkeypatch):
    """库里一行净值都没有 ⇒ 那句 skip 话不许说"覆盖得到这段窗口"（第 54 轮 B-8）。

    `stale_close_evidence` 对"两个日期都说不清"是**放行**（拿不准就别关，方向没错），
    但旧回执把那一句印成了它的反面 —— 而这句话老板会照着决定要不要再等。
    """
    mod = _import_script()
    p = _seed(test_db, code='NONAV9', target_offset=3, nav_back_days=400, held=True)
    # 把这只代码的净值清干净：库里既没有末条也没有首条
    from src.models.database import FundHistory
    test_db.query(FundHistory).filter(FundHistory.fund_code == 'NONAV9').delete()
    test_db.commit()
    _stub_source(monkeypatch, {'NONAV9': []})

    items, skipped = mod.plan(test_db, date.today())

    assert items == [], '库里一行净值都没有 ⇒ 说不清，不该关'
    assert [s[0] for s in skipped] == [p.id]
    assert '一行净值都没有' in skipped[0][2], skipped[0][2]
    assert '覆盖得到' not in skipped[0][2], (
        '这句话正好说反：那种形状恰恰是"覆盖不到 / 说不清"')

