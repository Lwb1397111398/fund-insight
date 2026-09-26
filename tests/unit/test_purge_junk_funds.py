# -*- coding: utf-8 -*-
"""`scripts/purge_junk_funds.py` 的安全阀。

批量删除在这个仓库里已经付过账，所以"什么情况下必须拒绝"要有会红的用例，
而不是靠脚本作者（我）记得看输出。
"""
import importlib.util
import io
import os
from datetime import date, datetime

import pytest

from src.models.database import FundHistory, FundInfo, Prediction

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    'purge_junk', os.path.join(ROOT, 'scripts', 'purge_junk_funds.py'))
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)


def _add_fund(db, code, name='垃圾码测试'):
    if not db.query(FundInfo).filter_by(fund_code=code).first():
        db.add(FundInfo(fund_code=code, fund_name=name))
    db.commit()


def test_a_code_with_live_predictions_is_a_blocker(test_db):
    """老板批准的是"垃圾码"，不是"还有人在用的码"：有活预测就整批不动。"""
    from src.models.database import Blogger, Post
    blogger = Blogger(name='垃圾码博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, content='垃圾码帖子',
                post_date=date(2026, 1, 5))
    test_db.add(post)
    test_db.flush()
    _add_fund(test_db, 'ZZZ001')
    test_db.add(Prediction(post_id=post.id, blogger_id=blogger.id, fund_code='ZZZ001',
                           fund_name='X', prediction_type='up', sector='测试',
                           prediction_date=date(2026, 1, 5), prediction_period='1周',
                           target_date=date(2026, 1, 12)))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ001',))
    assert rows[0]['live_predictions'] == 1
    actions, blockers = purge.plan(rows)
    assert blockers and not any(a[0] == 'fund_info' for a in actions), \
        '有活预测的码被放行删除 ⇒ 预测的标的会指向空'


def test_nav_rows_also_block_the_delete(test_db):
    """净值不为零就不是"垃圾"：这类"股票名挂在别人基金码"的行只能改名不能删。"""
    _add_fund(test_db, 'ZZZ002')
    test_db.add(FundHistory(fund_code='ZZZ002', nav_date=date(2026, 1, 5), nav=1.0))
    test_db.commit()
    actions, blockers = purge.plan(purge.inspect(test_db, ('ZZZ002',)))
    assert blockers, '有净值历史却照样删 ⇒ 同码真基金的数据被毁'


def test_truly_junk_code_is_planned_with_its_mapping_rows_first(test_db):
    """零预测零净值才允许删，且必须先删映射行（外键指向 fund_info）。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ003', '某股票名挂在基金码')
    test_db.add(SectorFundMapping(sector_name='ZZZ测试板块', fund_code='ZZZ003',
                                  fund_name='某股票名挂在基金码', is_active=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ003',))
    actions, blockers = purge.plan(rows)
    assert not blockers
    kinds = [a[0] for a in actions]
    assert kinds == ['mapping', 'fund_info'], \
        '顺序必须是先映射后档案：反过来的话外键会拒（镜像开了 foreign_keys）'
    assert actions[0][4] == 0, '同板块没有别的可服务行时要说出来（删完这个板块就没标的了）'


def test_missing_code_is_skipped_not_fatal(test_db):
    """镜像与生产现状不同（生产每个垃圾码还挂 1 行映射）⇒ 这个库里没有就跳过。"""
    actions, blockers = purge.plan(purge.inspect(test_db, ('不存在的码',)))
    assert actions == [] and blockers == []


def test_soft_deleted_prediction_blocks_until_explicitly_allowed(test_db):
    """回收站里那条预测没有外键挡着：删了档案就留下一条"打开即 500"的可恢复行。

    第 24 轮 B 复现：镜像 `predictions.id=3009`（603758、is_deleted=1）在旧版预检里
    根本数不到（只数 `is_deleted==0`），脚本照样报告"零预测，可以删"。
    """
    from src.models.database import Blogger, Post
    blogger = Blogger(name='回收站博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, content='回收站帖子', post_date=date(2026, 1, 5))
    test_db.add(post)
    test_db.flush()
    _add_fund(test_db, 'ZZZ004')
    test_db.add(Prediction(post_id=post.id, blogger_id=blogger.id, fund_code='ZZZ004',
                           fund_name='X', prediction_type='up', sector='测试',
                           prediction_date=date(2026, 1, 5), prediction_period='1周',
                           target_date=date(2026, 1, 12), is_deleted=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ004',))
    assert rows[0]['live_predictions'] == 0 and rows[0]['dead_predictions'] == 1, \
        '预检看不见软删行'
    actions, blockers = purge.plan(rows)
    assert blockers and not actions, '有软删预测却静默放行 ⇒ 留下指向空档案的行'
    actions, blockers = purge.plan(rows, allow_dead_predictions=True)
    assert not blockers and [a[0] for a in actions] == ['fund_info'], \
        '显式允许之后要能过，否则这个开关是假的'


def test_owner_locked_mapping_row_blocks_the_delete(test_db):
    """老板逐行确认过的有意代理不能被机器"顺手删掉"，要删必须明说。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ005')
    test_db.add(SectorFundMapping(sector_name='ZZZ老板板块', fund_code='ZZZ005',
                                  fund_name='有意代理', is_active=True, reviewed=True,
                                  reviewed_by='owner', owner_locked=True))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ005',))
    actions, blockers = purge.plan(rows)
    assert blockers and not actions, '老板锁定的映射行被机器删了'
    actions, blockers = purge.plan(rows, allow_owner_rows=True)
    assert [a[0] for a in actions] == ['mapping', 'fund_info']


def test_retry_rows_are_planned_before_the_archive(test_db):
    """`fund_sync_retry` 也对外键指向 fund_info：上一版只认 sector_fund_mapping。"""
    from src.models.database import FundSyncRetry
    _add_fund(test_db, 'ZZZ006')
    test_db.add(FundSyncRetry(fund_code='ZZZ006', retry_count=1,
                              next_retry_time=datetime(2026, 1, 6, 9, 0)))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ006',))
    assert rows[0]['retry_rows'] == 1
    actions, blockers = purge.plan(rows)
    assert not blockers
    assert [a[0] for a in actions] == ['fund_sync_retry', 'fund_info'], \
        '顺序必须先把引用表清干净，再删档案'


def _backup_of(test_db, tmp_path, code):
    import json
    dump = tmp_path / 'purge-backup.json'
    dump.write_text(json.dumps(purge._dump_rows(test_db, (code,)),
                               ensure_ascii=False, default=str), encoding='utf-8')
    return str(dump)


def test_backup_then_restore_puts_everything_back(test_db, tmp_path):
    """"可回滚"不是一个形容词：备份 → 删干净 → 按它逐列还原回来（真写要显式 apply）。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ007', '可回滚测试')
    mapping = SectorFundMapping(sector_name='ZZZ回滚板块', fund_code='ZZZ007',
                                fund_name='可回滚测试', is_active=True, confidence=0.77)
    test_db.add(mapping)
    test_db.commit()
    mapping_id = mapping.id

    dump = _backup_of(test_db, tmp_path, 'ZZZ007')
    # 断言必须打在**文件内容**上：dump 是路径字符串，
    # 上一版写 `assert 'fund_history' not in dump` 等于在检查文件名（第 26 轮 A 判 MAJOR：恒真）
    import json as _json
    tables = [e['table'] for e in _json.loads(io.open(dump, encoding='utf-8').read())]
    assert 'fund_history' not in tables, tables

    # dry-run 默认不写：这是第 25 轮 A 抓到的"全脚本唯一没有确认词的写路径"
    assert purge.restore(test_db, dump) == (0, 0, 0)
    test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').delete()
    test_db.query(FundInfo).filter_by(fund_code='ZZZ007').delete()
    test_db.commit()
    assert test_db.query(FundInfo).filter_by(fund_code='ZZZ007').first() is None
    assert purge.restore(test_db, dump) == (0, 0, 0)     # 仍然只是计划
    assert test_db.query(FundInfo).filter_by(fund_code='ZZZ007').first() is None

    purge.restore(test_db, dump, apply=True)
    back = test_db.query(FundInfo).filter_by(fund_code='ZZZ007').first()
    assert back is not None and back.fund_name == '可回滚测试'
    m = test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').first()
    assert m is not None and m.id == mapping_id and abs(m.confidence - 0.77) < 1e-6, \
        '还原没按原 id/逐列回来：备份等于没备'

    assert purge.restore(test_db, dump, apply=True) == (0, 0, 0), '幂等：跑两次不翻倍'
    assert test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ007').count() == 1


def test_restore_dedupe_uses_business_key_not_the_recyclable_id(test_db, tmp_path):
    """代理主键会被复用：备份里的 id 已属于**别的行**时，该还原的那行必须真的回来。

    第 25 轮 B 复现：旧实现拿 `id` 判重 ⇒ "1 行本就在库里，跳过"，垃圾码那行根本没还原。
    """
    from src.models.database import FundInfo as FI
    _add_fund(test_db, 'ZZZ008', 'id会复用的码')
    original = test_db.query(FI).filter_by(fund_code='ZZZ008').first()
    fid = original.id
    dump = _backup_of(test_db, tmp_path, 'ZZZ008')

    # 模拟"id 被回收"：删掉原行后另插一行，让它拿到同一个主键
    test_db.query(FI).filter_by(fund_code='ZZZ008').delete()
    test_db.commit()
    test_db.add(FI(id=fid, fund_code='999999', fund_name='占了原 id 的新基金'))
    test_db.commit()

    done, failed, refused = purge.restore(test_db, dump, apply=True)
    assert done >= 1 and failed == 0, \
        '备份里的 id 被别人占了就跳过 ⇒ 该还原的没还原，还报告"本就在库里"'
    back = test_db.query(FI).filter_by(fund_code='ZZZ008').first()
    assert back is not None and back.fund_name == 'id会复用的码'
    assert back.id != fid, 'id 被占时应当自增，而不是撞主键或盖掉占位行'
    assert test_db.query(FI).filter_by(fund_code='999999').first() is not None, \
        '还原不能动到占用者的行'


def test_restore_refuses_to_regrant_owner_immunity(test_db, tmp_path):
    """老板免疫不能由一份 JSON 盖回来 —— 与 `audit-import`、`/api/config/import` 同一口径。"""
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ009', '老板锁定行')
    test_db.add(SectorFundMapping(sector_name='ZZZ老板板块', fund_code='ZZZ009',
                                  fund_name='有意代理', is_active=True, reviewed=True,
                                  reviewed_by='owner', owner_locked=True))
    test_db.commit()
    dump = _backup_of(test_db, tmp_path, 'ZZZ009')
    test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ009').delete()
    test_db.commit()

    assert purge.restore(test_db, dump, apply=True) == (0, 0, 1), '未经显式开关就还原了老板免疫'
    assert test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ009').first() is None

    purge.restore(test_db, dump, apply=True, restore_owner_immunity=True)
    row = test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ009').first()
    assert row is not None and row.owner_locked and row.reviewed_by == 'owner', \
        '显式开关必须是有效的，否则它就是个假闸门'

def test_restore_dedupe_needs_every_business_column_to_match(test_db, tmp_path):
    """同一板块已有**另一只**基金时，被删的那行必须还能还原。

    第 26 轮两份复评共同判 MAJOR：业务键判重写成"任一列相等即已存在"，
    于是同板块占位就让该还原的行"静默不还原"，回执写着"跳过已存在"——
    与它要修的原 bug 同一个症状。
    """
    from src.models.database import SectorFundMapping
    _add_fund(test_db, 'ZZZ010', '同板块两行')
    test_db.add_all([
        SectorFundMapping(sector_name='ZZZ同板块', fund_code='ZZZ010',
                          fund_name='该还原的行', is_active=True),
        SectorFundMapping(sector_name='ZZZ同板块', fund_code='999999',
                          fund_name='占位另一行', is_active=True),
    ])
    test_db.commit()
    dump = _backup_of(test_db, tmp_path, 'ZZZ010')
    test_db.query(SectorFundMapping).filter_by(fund_code='ZZZ010').delete()
    test_db.commit()

    done, failed, refused = purge.restore(test_db, dump, apply=True)
    assert done >= 1, '同板块另有占位行就跳过 ⇒ 复合键只比了一列'
    codes = sorted(r.fund_code for r in test_db.query(SectorFundMapping).filter_by(
        sector_name='ZZZ同板块'))
    assert codes == ['999999', 'ZZZ010'], codes


def test_the_production_flag_has_teeth_and_the_check_runs_before_any_write():
    """`--production` 从"多印一行 [target]"变成**方向闸**（第 45 轮 B-m5：装饰性旗子）。

    旧写法里这把旗子只决定打印什么：把它从 argparse 整条删掉，脚本行为一个字都不变 ——
    而"没写这把旗子就动不到生产"正是操作员会照做的读法。现在四种形状各测一次：
    ① 默认 + 镜像 ⇒ 放行；② 旗子 + 真远程 ⇒ 放行；
    ③ 旗子 + 镜像 ⇒ 拒；④ 旗子 + **本机** PostgreSQL ⇒ 也拒（`postgresql://u@127.0.0.1/db`
    不是那台线上库，第 45 轮 B-m5 的另一半）；⑤ 没旗子却解析出远程 ⇒ 拒（钉库没生效）。
    再加一条"main 真的调了它、而且排在第一次 commit 之前"的 AST 判据 ——
    不然这函数可以永远是对的却没人用它。
    """
    import ast
    mirror = 'sqlite:///data/fund_insight.db'
    prod = 'postgresql://u:p@aws-0-x.pooler.supabase.com:6543/postgres'
    local_pg = 'postgresql://u@127.0.0.1:5432/db'
    assert purge._target_agrees_with_the_flag(False, mirror) is None
    assert purge._target_agrees_with_the_flag(True, prod) is None
    # 第 46 轮 A-m3 / B-M12：一把公网非 PG 也不算"那台线上库"
    assert purge._target_agrees_with_the_flag(True, 'mysql://u:p@db.partner.example/d'), \
        'MySQL 满足了 `--production` ⇒ 这把硬删脚本只问"远不远"，没问"是不是那台"'
    assert purge._target_agrees_with_the_flag(True, 'postgresql://u:p@staging.example.com/db'), \
        '别人的暂存库满足了 `--production`'
    for flag, url, what in ((True, mirror, '本地镜像'), (True, local_pg, '本机'),
                            (False, prod, '硬删')):
        why = purge._target_agrees_with_the_flag(flag, url)
        assert why and why.startswith('[abort]'), '%s + %s 竟被放行' % (flag, url)
        assert what in why or 'postgres' in why.lower(), '拒绝理由没说到点上：%s' % why

    tree = ast.parse(open(os.path.join(ROOT, 'scripts', 'purge_junk_funds.py'),
                          encoding='utf-8').read())
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'main')
    checks = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
              and getattr(n.func, 'id', '') == '_target_agrees_with_the_flag']
    commits = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
               and getattr(n.func, 'attr', '') in ('commit', 'flush', 'delete', 'add')]
    assert checks, 'main() 压根没调用方向闸 ⇒ 上面那三格是死代码'
    assert commits, 'main() 里没有写动作 ⇒ 这条"排在写之前"的判据是空判'
    assert min(checks) < min(commits), \
        '方向闸排在第一次写之后 ⇒ 它拦不住它说要拦的那件事（%s vs %s）' % (checks, commits)


def _junk_with_dead_prediction(db, code, judged=False):
    """造一个"只有回收站行挂着"的垃圾码：这正是生产上 603758 的形状。"""
    from src.models.database import Blogger, Post
    blogger = Blogger(name='硬删博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='硬删帖子', post_date=date(2026, 2, 5))
    db.add(post)
    db.flush()
    _add_fund(db, code)
    extra = {'is_correct': False, 'verify_count': 1} if judged else {}
    db.add(Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                      fund_name='X', prediction_type='up', sector='测试',
                      prediction_date=date(2026, 2, 5), prediction_period='1周',
                      target_date=date(2026, 2, 12), is_deleted=True, **extra))
    db.commit()


def test_dropping_dead_predictions_plans_backs_up_and_restores_in_dependency_order(test_db, tmp_path):
    """老板 2026-09-23 选的就是这条："先备份再硬删那条预测，然后一起删码"。

    三件事一起钉：① 不给旗子仍然整批拒；② 给了旗子要把它排进动作、并抄进**同一份**备份，
    且备份里 `predictions` 必须排在 `fund_info` **之后**（先插子表会撞 posts/bloggers 外键，
    第 33 轮在另一把删数据脚本上踩过"按备份原序插＝先子后父"）；③ 删完能按这份备份还原回来。
    """
    import json as _json
    _junk_with_dead_prediction(test_db, 'ZZZ020')
    rows = purge.inspect(test_db, ('ZZZ020',))
    assert rows[0]['dead_predictions'] == 1 and len(rows[0]['dead_prediction_ids']) == 1
    actions, blockers = purge.plan(rows)
    assert blockers and not actions, '不给旗子就放行 ⇒ 留下指向空档案的可恢复行'

    actions, blockers = purge.plan(rows, drop_dead_predictions=True)
    assert not blockers and [a[0] for a in actions] == ['prediction', 'fund_info'], actions

    dump = tmp_path / 'purge-with-predictions.json'
    dump.write_text(_json.dumps(purge._dump_rows(test_db, ('ZZZ020',), True),
                                ensure_ascii=False, default=str), encoding='utf-8')
    tables = [e['table'] for e in _json.loads(dump.read_text(encoding='utf-8'))]
    assert 'predictions' in tables, tables
    assert tables.index('predictions') > tables.index('fund_info'), \
        '还原顺序错了：预测排在档案前面会撞外键 ⇒ %s' % tables

    the_id = rows[0]['dead_prediction_ids'][0]
    test_db.query(Prediction).filter(Prediction.id == the_id).delete(synchronize_session=False)
    test_db.query(FundInfo).filter_by(fund_code='ZZZ020').delete(synchronize_session=False)
    test_db.commit()
    assert test_db.query(Prediction).filter(Prediction.id == the_id).first() is None
    done, failed, refused = purge.restore(test_db, str(dump), apply=True)
    assert (failed, refused) == (0, 0), '还原本身失败了却像成功：done=%s failed=%s refused=%s' % (
        done, failed, refused)
    back = test_db.query(Prediction).filter(Prediction.id == the_id).first()
    assert back is not None and back.is_deleted is True and back.fund_code == 'ZZZ020', \
        '备份里有这行，还原却没把它插回来'
    assert test_db.query(FundInfo).filter_by(fund_code='ZZZ020').first() is not None


def test_a_judged_recycle_bin_prediction_is_never_hard_deleted(test_db):
    """带结论的软删行是审计证据，不是垃圾：`--drop-dead-predictions` 也不许把它删掉。

    反向对照：老口子 `--allow-dead-predictions`（明知有软删行仍然只删档案）必须还能过，
    否则这两条旗子的区别就只写在注释里。
    """
    _junk_with_dead_prediction(test_db, 'ZZZ021', judged=True)
    rows = purge.inspect(test_db, ('ZZZ021',))
    assert rows[0]['dead_predictions_judged'] == 1, '预检数不清"带结论的那几条"'
    actions, blockers = purge.plan(rows, drop_dead_predictions=True)
    assert blockers and not actions, '带已判结论的回收站行被硬删 ⇒ 结论证据永久消失'
    assert '结论' in blockers[0], blockers
    actions, blockers = purge.plan(rows, allow_dead_predictions=True)
    assert not blockers and [a[0] for a in actions] == ['fund_info']


def test_a_dead_prediction_with_a_change_log_is_never_dropped(test_db):
    """2026-09-26 副本演练量出来的真缺陷：硬删那条预测撞 `prediction_change_logs` 的
    `ON DELETE RESTRICT` 外键 —— 旧写法在**计划里承诺"删 1 行软删预测"，执行时才发现做不到**，
    整批回滚（备份已经落盘，但一次失败的删除没人想复盘）。

    现在动手前就拒，并且把可行的路说清：改指到有效基金（保留台账），或老板明确要清台账。
    """
    from src.models.database import PredictionChangeLog
    _junk_with_dead_prediction(test_db, 'ZZZ022')
    pid = purge.inspect(test_db, ('ZZZ022',))[0]['dead_prediction_ids'][0]
    test_db.add(PredictionChangeLog(prediction_id=pid, action='retag', source='sync',
                                    changed_fields=['fund_code'],
                                    before_state={'fund_code': 'OLD'},
                                    after_state={'fund_code': 'ZZZ022'}))
    test_db.commit()

    rows = purge.inspect(test_db, ('ZZZ022',))
    assert rows[0]['dead_prediction_dependents'] == {'prediction_change_logs': 1}, rows[0]
    actions, blockers = purge.plan(rows, drop_dead_predictions=True)
    assert blockers and not actions, '台账还引用着却仍把预测排进删除动作 ⇒ 计划说了做不到的事'
    assert 'prediction_change_logs' in blockers[0] and '改指' in blockers[0], blockers
