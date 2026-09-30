"""板块压根没有可用标的时，同步器给它补一只"问得出净值"的基金（任务 #157）。

老板 2026-09-28 原话：「某预测对应板块对应的基金被发现抓取不到且确认没有办法的情况，
可以把该板块对应的基金变成其他好的基金，我们已经有板块对应基金的流程和相关代码，
完全可以稍作修改然后复用的」⇒ 复用 `get_fund_for_sector`（内置板块表 + 别名 +
"不许硬凑"名单）与 `retag_prediction`（留台账、可整批还原），**不**新造第二条改标路。

镜像 2026-09-28 实测这一档的量：33 个板块标签 / 200 条未判预测在库里**一行映射都没有**
（`sector_alias` 0 行），而旧写法只把它们数成 `predictions_no_mapping` 就一个字不做。
复现（默认钉本地镜像；日期现算，别抄）：
    python scripts/q.py "with t as (select coalesce(nullif(sector,''),nullif(sector_type,'')) s, id
    from predictions where is_deleted=0 and is_correct is null) select s, count(*) n from t
    where s is not null and (select count(*) from sector_fund_mapping m
    where m.is_active=1 and m.sector_name=t.s)=0 group by 1 order by n desc"

这批自己踩到的一次（写在判据里防下一轮）：第一版没有"只动问不出证据的那几条"这道门，
镜像 2026-09-29 同一份代码只把门换成 `if False`，预览从 `would_update 28` 涨到 **655**
（其中 627 条走补标那一路、**470 条带着已判结论**）—— 一次按钮就清掉几百条已有结论，
正是第 18 轮"一键清空 515 条结论"的形状。`test_a_row_that_can_already_be_evidenced_is_left_alone`
钉的就是这道门；把那道 `continue` 摘掉，它必须当场红（变异 M29）。
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from starlette.requests import Request

from src.api.routes import predictions as prediction_routes
from src.core import config
from src.models.database import (
    Blogger,
    FundHistory,
    FundInfo,
    Post,
    Prediction,
    PredictionChangeLog,
    SectorFundMapping,
)
from src.services.prediction_maintenance_service import PredictionMaintenanceService

from src.services import prediction_lifecycle as _lc

lc_current_as_of = _lc.current_as_of

GOLD = '黄金'


@pytest.fixture(autouse=True)
def _foreign_keys_are_enforced(test_db):
    """这份文件里的"没有档案就插不进去"必须是**库自己说的**，不是注释里说的。

    共享夹具是裸 `create_engine("sqlite:///:memory:")`，而 SQLite **默认不强制外键** ⇒
    "有外键所以插进去会 IntegrityError"这句话在这份夹具里结构性不可验证
    （第 66 轮复评 MI-1，同一件事第 50 轮在 `test_fund_info_archive_gate.py:34` 已经踩过一次：
    外键没开 ⇒ "档案被拒建、映射行却改到那个码上"这个形状在绿灯里过；
    而生产 PostgreSQL 会直接撞墙、镜像静默留脏 —— 两个库各坏一种）。
    """
    test_db.execute(__import__('sqlalchemy').text('PRAGMA foreign_keys = ON'))
    yield


def _request(headers=None):
    raw_headers = [
        (key.lower().encode('latin-1'), value.encode('latin-1'))
        for key, value in (headers or {}).items()
    ]
    return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': raw_headers})


CONFIRM = {'X-Danger-Confirm': 'sync-prediction-mapping'}


def _builtin_target(label=GOLD):
    """内置表对这个板块答的是哪只 —— 判据不抄代码里的常量，跟着那张表走。"""
    from src.constants.sector_fund_map import get_fund_for_sector
    hit = get_fund_for_sector(label) or {}
    assert hit.get('code'), '内置板块表里 %s 这一行没了 ⇒ 本文件的夹具要换一个板块' % label
    return hit['code'], hit.get('name') or hit['code']


def _archive(db, code, name, *, with_nav_for=None):
    db.add(FundInfo(fund_code=code, fund_name=name, sector_type=GOLD))
    if with_nav_for:
        start, end = with_nav_for
        day, n = start, 0
        while day <= end and n < max(2, config.VERIFY_MIN_DATA_POINTS):
            db.add(FundHistory(fund_code=code, fund_name=name, nav_date=day,
                               nav=1.0 + n * 0.01, day_growth=0.1))
            day, n = day + timedelta(days=1), n + 1
    db.flush()


def _stale_archive(db, code='DEAD01', name='停更的标的', last=date(2020, 12, 8)):
    """档案在、净值只到 2020-12-08 —— 生产上 `003033` 就是这个形状（任务 #130）。"""
    db.add(FundInfo(fund_code=code, fund_name=name, sector_type=GOLD))
    db.add(FundHistory(fund_code=code, fund_name=name, nav_date=last, nav=1.0,
                       day_growth=0.0))
    db.flush()


def _nav(db, code, name, start=date(2026, 7, 1)):
    """给这只标的铺 `VERIFY_MIN_DATA_POINTS` 笔净值 —— 三个档位都要它"给得出"过。"""
    for offset in range(max(2, config.VERIFY_MIN_DATA_POINTS)):
        db.add(FundHistory(fund_code=code, fund_name=name,
                             nav_date=start + timedelta(days=offset),
                             nav=1.0, day_growth=0.1))
    db.flush()


def _prediction(db, *, sector=GOLD, fund_code='DEAD01', verified=False,
                window=None, sector_type=None):
    blogger = Blogger(name='补标的测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, title='补标的测试帖', content='用于测试板块补标的。',
                post_date=date(2026, 7, 1), analyzed=True)
    db.add(post)
    db.flush()
    start, end = window or (date(2026, 7, 1), date(2026, 7, 8))
    value = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=fund_code,
                       fund_name='挂在死标的上的基金' if fund_code else None,
                       sector=sector, sector_type=sector_type,
                       prediction_type='up', prediction_content='看涨',
                       confidence=80, prediction_date=start, prediction_period='1周',
                       target_date=end, status='success' if verified else 'pending',
                       is_expired=verified, is_correct=True if verified else None,
                       verify_count=1 if verified else 0, verify_score=80 if verified else 0,
                       verify_history=[{'date': '2026-07-08', 'score': 80}] if verified else [],
                       is_deleted=False)
    db.add(value)
    db.flush()
    return value


def test_a_sector_with_no_mapping_at_all_gets_the_builtin_target(test_db):
    """库里一行映射都没有 ⇒ 补一行、并把那条问不出证据的预测改到它身上（含台账）。"""
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    pred = _prediction(test_db)
    test_db.commit()

    service = PredictionMaintenanceService(test_db)
    plan = service.sync_sector_mappings(dry_run=True)
    assert [item['sector'] for item in plan['sectors_to_fill']] == [GOLD], plan['sectors_to_fill']
    assert plan['sectors_to_fill'][0]['fund_code'] == code
    assert plan['sectors_to_fill'][0]['mode'] == 'insert'
    assert test_db.query(SectorFundMapping).count() == 0, '预览就写库 ⇒ "预览"两个字是假的'

    done = service.sync_sector_mappings(dry_run=False, run_id='t-gapfill-1')
    test_db.refresh(pred)
    assert done['sectors_filled'] == 1
    assert pred.fund_code == code, '预测没被改到补上的标的上 ⇒ 补映射行等于什么都没做'
    assert test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).count() == 1
    row = test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).first()
    assert row.reviewed and row.reviewed_by == 'seed' and not row.owner_locked, \
        '补上的行要么不被下一次同步认（reviewed False ⇒ 这块永远还是黑洞），' \
        '要么白拿了老板署名（owner/locked ⇒ 机器给自己发了免检章）'
    assert row.verify_message and '内置' in row.verify_message, \
        '这一行的来源没写在行上 ⇒ 页面与下一次体检都看不出它是机器补的还是人挑的'
    assert row.evidence and code in row.evidence, '机器改标不留依据 ⇒ 页面上没法复核是谁说的'
    # 台账：没有 run_id 的改标等于不可回滚（第 51 轮那条规矩）
    assert test_db.query(PredictionChangeLog).filter_by(
        prediction_id=pred.id, run_id='t-gapfill-1').count() == 1


def test_a_stale_mapping_row_does_not_blind_the_second_sector_label(test_db):
    """第一根标签**有**映射行、可那一行指的标的已经停更 ⇒ 第二根标签的补标那一路照样要走。

    任务 #171 镜像现读的那两条 `2303/2304`（`sector='A股'` 命中库里 `515440` 那一行，
    `sector_type='科技'` 库里没行、只有内置表答得出 `515000`）就是这个形状。
    补标那一路此前只在"`sector` 与 `sector_type` 在库里都没有行"时才启动
    （`missing_labels` 那一腿），于是这类行既走不到回落、也走不到补标，
    最终只能等「关进回收站」—— 老板要的是"变成其他好的基金"。
    这一格同时钉住两件事：**计划表里出现的是第二块板块**（不是第一块），
    并且真跑之后映射行与预测都落了地。
    """
    farm_code, farm_name = _builtin_target('农业')
    _archive(test_db, farm_code, farm_name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    test_db.add(SectorFundMapping(sector_name=GOLD, fund_code='DEAD01',
                                  fund_name='停更的标的', reviewed=True, is_active=True,
                                  confidence=0.95))
    pred = _prediction(test_db, sector=GOLD, fund_code='DEAD01', sector_type='农业')
    test_db.commit()

    service = PredictionMaintenanceService(test_db)
    plan = service.sync_sector_mappings(dry_run=True)
    assert [item['sector'] for item in plan['sectors_to_fill']] == ['农业'], plan['sectors_to_fill']
    assert plan['sectors_to_fill'][0]['fund_code'] == farm_code
    assert plan['sectors_to_fill'][0]['mode'] == 'insert'
    assert plan['predictions_via_gap_fill_planned'] == 1, \
        '这一条要动的是"第二块板块补出来的标的"，计划数不说出来就无法在点执行之前复核'
    assert test_db.query(SectorFundMapping).filter_by(sector_name='农业').count() == 0

    done = service.sync_sector_mappings(dry_run=False, run_id='t-gapfill-second-label')
    test_db.refresh(pred)
    assert done['sectors_filled'] == 1
    assert done['predictions_via_gap_fill'] == 1
    assert pred.fund_code == farm_code, '映射行补了、预测还挂在停更的那只上 ⇒ 白做一次'
    assert test_db.query(SectorFundMapping).filter_by(sector_name='农业').count() == 1
    assert test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).count() == 1, \
        '第一块板块那一行是老板审过的，一个字都不许动'


def test_a_row_that_can_already_be_evidenced_is_left_alone(test_db):
    """板块新补的标的**不许**抢走自己就问得出净值的预测（第 18 轮那次一键清空的形状）。"""
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _archive(test_db, 'OWN01', '自己有好标的', )
    for offset in range(max(2, config.VERIFY_MIN_DATA_POINTS)):
        test_db.add(FundHistory(fund_code='OWN01', fund_name='自己有好标的',
                                nav_date=date(2026, 7, 1) + timedelta(days=offset),
                                nav=1.0, day_growth=0.1))
    pred = _prediction(test_db, fund_code='OWN01', verified=True)
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=False,
                                                                        run_id='t-keep-1')
    test_db.refresh(pred)
    assert result['predictions_kept_own_target'] == 1, result
    assert result['predictions_via_gap_fill'] == 0
    assert result['sectors_filled'] == 0, '一条预测都不必动，却为这个板块建了映射行 ⇒ 孤儿行'
    assert pred.fund_code == 'OWN01' and pred.is_correct is True, \
        '它自己那只标的就给得出这段窗口的净值，却被换成板块标的并清了结论'


def test_the_preview_and_the_run_agree_on_which_sectors_get_filled(test_db):
    """预览说补哪几块，实跑就只能补那几块（第 51 轮 B-2 那族：数不同=回执说谎）。"""
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    _prediction(test_db)
    test_db.commit()

    service = PredictionMaintenanceService(test_db)
    plan = service.sync_sector_mappings(dry_run=True)
    run = service.sync_sector_mappings(dry_run=False, run_id='t-agree-1')
    assert [i['sector'] for i in plan['sectors_to_fill']] == \
        [i['sector'] for i in run['sectors_to_fill']]
    # 计划那一份两边同数；**做到**那一份只有实跑才许非零（第 66 轮复评 MA-4：
    # 原来只有一个键，语义跟着"预览还是实跑"漂，那正是"拿要不要做当做了"的形状）。
    assert plan['predictions_via_gap_fill_planned'] == run['predictions_via_gap_fill_planned'] == 1
    assert plan['predictions_via_gap_fill'] == 0, '预览就报"已经改过去了" ⇒ 完成时是假的'
    assert run['predictions_via_gap_fill'] == 1, run
    assert plan['would_update'] == run['predictions_updated'], (plan['would_update'],
                                                               run['predictions_updated'])


def test_a_sector_the_builtin_table_cannot_answer_is_said_out_loud(test_db):
    """内置表也说不出对口品种 ⇒ 不猜、不建行，并把"为什么补不了"交到回执里。"""
    _stale_archive(test_db)
    _prediction(test_db, sector='卫星互联网产业')     # "不许硬凑"名单的更长说法
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-refuse-1')
    refused = {item['sector']: item['reason'] for item in result['sectors_refused_to_fill']}
    assert '卫星互联网产业' in refused, result['sectors_refused_to_fill']
    assert '内置表' in refused['卫星互联网产业'] or '硬凑' in refused['卫星互联网产业']
    assert result['sectors_filled'] == 0
    assert test_db.query(SectorFundMapping).count() == 0, '名单说不许硬凑，还是给它配了一只'


def test_an_owner_picked_target_is_never_swapped_by_the_machine(test_db):
    """老板署名挑定的那一行（有意代理）⇒ 机器一个字都不动，只说出来。

    夹具用 `is_active=False` 而不是"活跃但被判不可服务"：`row_unservable()` 的 owner
    例外只认老板这次确认过的那只基金，所以一条**活跃**的 owner 行本来就进不了"板块没有
    标的"这一档（也就用不到这道拒收）—— 停用的 owner 行才是真会撞上的形状。
    """
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    _archive(test_db, 'PROXY01', '老板挑的代理')      # 映射行的外键要认这个码
    test_db.add(SectorFundMapping(sector_name=GOLD, fund_code='PROXY01', fund_name='老板挑的代理',
                                  is_active=False, reviewed=True, reviewed_by='owner',
                                  owner_locked=True, is_fetchable=False))
    pred = _prediction(test_db, fund_code='DEAD01')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-owner-1')
    refused = {item['sector']: item['reason'] for item in result['sectors_refused_to_fill']}
    assert GOLD in refused and '老板' in refused[GOLD], refused
    assert result['predictions_updated'] == 0, '这个板块被拒了却还是改标了 ⇒ 拒收没落地'
    row = test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).first()
    assert row.fund_code == 'PROXY01' and row.owner_locked, '机器把老板署名那行换了标的'
    test_db.refresh(pred)
    assert pred.fund_code == 'DEAD01'


def test_a_candidate_without_an_archive_is_refused_and_says_why(test_db):
    """内置表答得出标的，但**本库还没有那只的档案** ⇒ 不建行、并把"先跑一次更新基金"说出来。

    这一格不是装饰：`sector_fund_mapping.fund_code` 有外键，插进去就是 IntegrityError
    （第 50 轮那族悬空行）；而"档案都没有"与"净值给不出这段窗口"是两件不同的事，
    老板照着那句话做的动作也不同（前者点「更新基金」，后者什么也不用做）。
    """
    from src.constants.sector_fund_map import get_fund_for_sector
    label = '白酒'
    hit = get_fund_for_sector(label) or {}
    assert hit.get('code'), '内置表里 白酒 这一行没了 ⇒ 换一个板块标签'
    _stale_archive(test_db, code='DEAD02', name='停更的标的')
    p = _prediction(test_db, sector=label, fund_code='DEAD02')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-noarch-1')
    refused = {item['sector']: item['reason'] for item in result['sectors_refused_to_fill']}
    assert label in refused and '档案' in refused[label], refused
    assert test_db.query(SectorFundMapping).count() == 0
    test_db.refresh(p)
    assert p.fund_code == 'DEAD02', '拒了却仍然改了它的标的'


def test_a_fillable_sector_with_nothing_to_move_gets_no_row(test_db):
    """另一块板块真会改标（于是写库那一段确实执行了），也不许顺手给这块建行。

    这一格是 M30 的凭据。上一版把这条判断并进了 `..._is_left_alone` 里那句
    `sectors_filled == 0` —— 而实跑在 `candidates` 为空时**提前 return**，那一句
    根本执行不到 ⇒ 变异（给所有"可补"板块都建行）打出 GREEN，判据只是在描述自己。
    只有让候选非空（另一块板块真有一条预测要动），再问"这块没动的板块被建了行没有"，
    这道门才有牙。
    """
    gold, gold_name = _builtin_target(GOLD)
    med, med_name = _builtin_target('医疗')
    assert med != gold, '两块板块挑到同一只标的 ⇒ 这一格分不出是谁的行'
    _archive(test_db, gold, gold_name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _archive(test_db, med, med_name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _archive(test_db, 'OWN01', '自己有好标的')
    for offset in range(max(2, config.VERIFY_MIN_DATA_POINTS)):
        test_db.add(FundHistory(fund_code='OWN01', fund_name='自己有好标的',
                                nav_date=date(2026, 7, 1) + timedelta(days=offset),
                                nav=1.0, day_growth=0.1))
    _stale_archive(test_db, code='DEAD03', name='停更的标的')
    kept = _prediction(test_db, sector=GOLD, fund_code='OWN01')
    moved = _prediction(test_db, sector='医疗', fund_code='DEAD03')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-two-sectors-1')
    sectors = sorted(row.sector_name for row in test_db.query(SectorFundMapping).all())
    assert sectors == ['医疗'], \
        '映射行建到了 %s —— 黄金那块一条预测都不用动，不该有它的行' % sectors
    assert result['sectors_filled'] == 1 == len(result['sectors_to_fill'])
    assert {i['sector'] for i in result['sectors_fillable']} >= {GOLD, '医疗'}, \
        '"可以补"与"这次真补"必须分开报：可补的那一块要看得见，只是不许动它'
    assert [i['sector'] for i in result['sectors_to_fill']] == ['医疗']
    test_db.refresh(kept); test_db.refresh(moved)
    assert kept.fund_code == 'OWN01' and moved.fund_code == med, (kept.fund_code, moved.fund_code)


def test_a_dead_sector_row_is_rewritten_in_place_not_added_alongside(test_db):
    """已有行确认给不出净值 ⇒ **改写那一行**而不是另加一行：
    生产库里有一条模型没声明的 `sector_fund_mapping_sector_name_key UNIQUE(sector_name)`
    （2026-09-22 直连 pg_constraint 实测，镜像没有）⇒ 另加一行在生产直接撞约束。"""
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    dead = SectorFundMapping(sector_name=GOLD, fund_code='DEAD01', fund_name='停更的标的',
                             is_active=True, reviewed=True, reviewed_by='agent',
                             confidence=0.9, is_fetchable=False)
    test_db.add(dead)
    _prediction(test_db, fund_code='DEAD01')
    test_db.commit()
    dead_id = dead.id

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-rewrite-1')
    assert result['sectors_to_fill'][0]['mode'] == 'update', result['sectors_to_fill']
    rows = test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).all()
    assert len(rows) == 1, '同一板块添了第二行：镜像静默成功、生产撞唯一约束（第 49 轮那一课）'
    assert rows[0].id == dead_id and rows[0].fund_code == code
    assert rows[0].is_fetchable is None, \
        '换了标的还留着旧标的的"可服务"结论 ⇒ 新标的从没体检过却被当成体检过（第 8/9 轮那族幽灵行）'


def test_a_filled_row_is_usable_the_next_time_the_sync_runs(test_db):
    """补完标的的那一行**必须重新被同步器自己认到**，否则"补上标的"只捞出当下这几条。

    第 66 轮复评 MA-1/MA-2 量到的两格（都在 `update` 那一支，内存 sqlite 两遍跑批实测）：
    ① 不恢复 `is_active` ⇒ 只有"本来对同步器不可见"的行才会走到这里，留着 False
       就是下一次跑批这板块又是黑洞，而 `blocked_codes` 此时正好等于刚写进去的那只
       ⇒ 从此永久回"内置表给的就是库里那只"，谁都不再动它；
    ② 把署名从 `agent` 改成 `seed` 却留着旧标的的 0.70 分 ⇒ `_mapping_eligible`
       不再走 agent 那一臂（proxy 0.68），退到 `min_confidence 0.85` 兜底 ⇒ 不合格。
       那正是这个函数自己 docstring 写明"本轮修的就是"的死区（S4a M2）。
    判据问的是**第二次跑批的行为**，不是那一行列值：列值对而同步器看不见，一样是黑洞。
    """
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    old = SectorFundMapping(sector_name=GOLD, fund_code='DEAD01', fund_name='停更的标的',
                            is_active=False, reviewed=False, reviewed_by='agent',
                            match_kind='proxy', confidence=0.70)
    test_db.add(old)
    _prediction(test_db, fund_code='DEAD01')
    test_db.commit()

    first = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-usability-1')
    assert first['sectors_filled'] == 1
    row = test_db.query(SectorFundMapping).filter_by(sector_name=GOLD).first()
    assert row is old and row.is_active and row.confidence is None, (
        row.is_active, row.confidence)
    assert PredictionMaintenanceService._mapping_eligible(row, 0.85), \
        '补完的行不合格 ⇒ 下一次同步还是看不见这个板块'

    # 同板块**新**的一条预测（今天还没被任何一次跑批看过）：第二次预览必须直接用它，
    # 而不是"这块没有可用映射 ⇒ 再补一次"或"补不了，因为库里那只就是内置表给的"。
    newer = _prediction(test_db, fund_code='DEAD01')
    test_db.commit()
    second = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    refused = {item['sector']: item.get('kind') for item in second['sectors_refused_to_fill']}
    assert GOLD not in refused, '第二次跑批把这块判成"补不了"：\n%s' % refused
    assert [i['sector'] for i in second['sectors_to_fill']] == [], \
        '补过一次又要补一次 ⇒ 第一次那一次根本没让它可用'
    assert second['would_update'] >= 1, '新预测没被那块已补好的标的接走'
    assert second['predictions_no_mapping'] == 0, second['predictions_no_mapping']
    test_db.refresh(newer)
    assert newer.fund_code == 'DEAD01', '预览就改了标 ⇒ dry_run 是假的'


def test_the_route_names_the_reason_it_actually_hit(test_db):
    """五种拒收各有原因，回执那句话**不许**把它们压成一句写死的诊断（MA-3）。

    页面这一路只印 `message` 一句（预览面板只有一行"预计更新 N 条"，真跑是
    `alert(response.data.message)）⇒ 原来那句"逐块原因见明细"指的是一个不存在的栏
    （第 53 轮 A-1 那句"哪两种原因见上方…"同族），而写死的那半句"内置表也说不出
    对口品种"对六种理由里的五种都是假话。现在话从 `kind` 数出来。
    """
    from src.constants.sector_fund_map import get_fund_for_sector
    label = '白酒'
    hit = get_fund_for_sector(label) or {}
    assert hit.get('code'), '内置表里 白酒 这一行没了 ⇒ 换一个板块标签'
    _prediction(test_db, sector=label, fund_code='DEAD09')     # 没档案 ⇒ no_archive
    _archive(test_db, 'PROXY09', '老板挑的代理')
    _stale_archive(test_db, code='DEAD10', name='停更的标的')
    test_db.add(SectorFundMapping(sector_name=GOLD, fund_code='PROXY09', fund_name='老板挑的代理',
                                  is_active=False, reviewed=True, reviewed_by='owner',
                                  owner_locked=True, is_fetchable=False))
    _prediction(test_db, sector=GOLD, fund_code='DEAD10')      # 署名行 ⇒ owner_locked
    test_db.commit()

    body = prediction_routes.sync_sector_mapping(request=_request(), dry_run=True, db=test_db)
    message = body['message']
    assert '本库还没有那只标的的档案' in message, message
    assert '那行标的是老板署名挑定的' in message, message
    assert '内置表也说不出对口品种' not in message, \
        '这一轮的两种拒收都不是"内置表答不出"，那句话是替别的原因撒的谎：\n%s' % message
    assert '见明细' not in message, '页面上没有"明细"这一栏 ⇒ 指路的话要指到一个真在的地方'


def test_the_execute_sentence_counts_rows_it_really_moved(test_db):
    """完成时那句里的两个数都得是**做完之后**查出来的（MA-4：第 51 轮 B-2 同一族）。"""
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    pred = _prediction(test_db)
    test_db.commit()

    body = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                 dry_run=False, db=test_db)
    data = body['data']
    test_db.refresh(pred)
    assert data['predictions_via_gap_fill'] == 1 == data['predictions_updated']
    assert f"已给 1 个原本没有可用标的的板块补上标的，并把 1 条" in body['message'], body['message']
    assert '会按内置板块表' not in body['message'], '实跑那一支不许再说"会"'
    assert pred.fund_code == code


def test_a_nav_row_without_an_archive_is_still_refused_and_the_db_proves_why(test_db):
    """`fund_history` 有行而 `fund_info` 没档案 ⇒ 拒的是"没档案"，不是"没净值"（MI-1）。

    两件事分不开就会把老板支错地方：话里让他"先跑一次更新基金"，而他真跑了也建不出档案
    的话什么也不会变。另一半是这份文件开头那条 autouse 夹具的存在理由 ——
    "插进去就是 IntegrityError"这句必须**当场长出来一次**，否则它只是注释。
    """
    from sqlalchemy.exc import IntegrityError

    from src.constants.sector_fund_map import get_fund_for_sector
    label = '白酒'
    code = (get_fund_for_sector(label) or {}).get('code')
    assert code, '内置表里 白酒 这一行没了 ⇒ 换一个板块标签'
    # 净值行可以没有档案（`fund_history.fund_code` 没有外键 —— 生产/镜像都允许这个形状）
    for offset in range(3):
        test_db.add(FundHistory(fund_code=code, fund_name='有净值没档案',
                                nav_date=date(2026, 7, 1) + timedelta(days=offset),
                                nav=1.0, day_growth=0.1))
    _prediction(test_db, sector=label, fund_code='DEAD11')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-orphan-nav-1')
    refused = {item['sector']: item for item in result['sectors_refused_to_fill']}
    assert refused.get(label, {}).get('kind') == 'no_archive', refused
    assert '档案' in refused[label]['reason'], refused
    assert test_db.query(SectorFundMapping).count() == 0

    with pytest.raises(IntegrityError):
        test_db.add(SectorFundMapping(sector_name=label, fund_code=code,
                                      fund_name='有净值没档案', is_active=True))
        test_db.flush()
    test_db.rollback()


def test_the_evidence_handed_to_the_retag_gate_is_the_window_slice():
    """交给改标门的那份"窗口内净值日"必须出自 `window_from_calendar` 那一次切片。

    第 66 轮复评 MA-4/MI-4：预览那一腿走 `calendar_gap`（内部切片），而实跑那一腿
    原来把 `nav_calendar` 的**全量**历史当 `evidence` 第一元递进 `retag_prediction`
    ⇒ 同一把尺子两种喂法，实跑只会更松，"预览与实跑同数"从构造成立退化成偶然成立。
    这一格**没法用结果判**（超集只会放行更多行，找一个"切片拒、全量过"的形状，
    在预览那一腿就已经被拦下了），所以按 AST 钉接线：那一次调用的参数必须来自
    `window_from_calendar`，不许再出现 `calendar.get(...)` 直接喂 `evidence=`。
    """
    import ast

    source = (Path(__file__).resolve().parents[2]
              / 'src' / 'services' / 'prediction_maintenance_service.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    body = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == 'sync_sector_mappings')
    tuples = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and getattr(node.value.func, 'id', None) == 'window_from_calendar':
            for target in node.targets:
                if isinstance(target, ast.Tuple):
                    tuples.update(el.id for el in target.elts if isinstance(el, ast.Name))
    assert tuples, '没找到 `days, latest = window_from_calendar(...)` 那一次解包 ⇒ 切片这一腿没接'
    offenders = []
    for node in ast.walk(body):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != 'evidence':
                continue
            if not any(isinstance(name, ast.Name) and name.id in tuples
                       for name in ast.walk(kw.value)):
                offenders.append(kw.value)
    assert not offenders, \
        'evidence= 里递的不是那一次切片的产物（%s）⇒ 全量历史当窗口证据，实跑那一腿只会更松' % offenders
    # 也不许有人在这条路上自己抄一遍 `start <= d <= end`（切片只许在那一处）
    text = ast.unparse(body)
    assert 'prediction_date) and d <=' not in text and 'in_window = [' not in text, \
        '这条路上又抄了一遍窗口切片 ⇒ 同一把尺子两处定义'
    # 板块标签归一必须**两处都用**：建计划那一侧与查候选这一侧各用各的拼法，
    # 就会出现"计划里给 `绿电` 补了标的、预测这一侧查 `绿色电力` ⇒ 谁也没沾上"（MI-6）。
    calls = [node for node in ast.walk(body)
             if isinstance(node, ast.Call)
             and getattr(node.func, 'attr', None) == '_gap_label']
    assert len(calls) == 2, \
        '标签归一只接在 %d 处（应为建计划 + 查候选各一处）⇒ 同义板块又会各写一行映射' % len(calls)



def _nav_in_window(db, code, name, start=date(2026, 7, 1), end=date(2026, 7, 8)):
    """给这只标的在预测窗口里铺够点数（问得出证据的形状）。"""
    db.add(FundInfo(fund_code=code, fund_name=name, sector_type=GOLD))
    day, n = start, 0
    while day <= end and n < max(2, config.VERIFY_MIN_DATA_POINTS):
        db.add(FundHistory(fund_code=code, fund_name=name, nav_date=day,
                           nav=1.0 + n * 0.01, day_growth=0.1))
        day, n = day + timedelta(days=1), n + 1
    db.flush()


def test_a_sector_with_its_own_mapping_row_is_never_treated_as_a_gap_fill(test_db):
    """那道"只紧不松"的门**只**圈补标那一路：板块自己有映射行 ⇒ 照常对齐，一条都不许挡。

    这一格是第 66 轮返修的回归现场（变异 M40 的凭据）。我把 `via_gap` 推成"标签在不在
    补标计划表里"，又把归一后的标签当映射键去查 —— 于是映射那一路也被这道门管上了，
    `tests/unit/test_sector_remap.py` 四条一起红（`predictions_updated` 全成 0）。

    ⚠ **这一格判的是"门的范围"，不是"清结论这件事本身对"**（第 67 轮复评 MAJOR-5 提醒的
    正是这个读法）。两条路问的不是同一个问题：
      · 有映射行 ⇒ "这个板块由哪只标的定价"已经有人答过（老板审查 / agent 匹配），
        换标的之后旧结论会由 `retag_prediction` 清掉并按新标的**重判**（新标的给不出证据
        的那些已经被 #100/#105 那道门拦下）。反例是本仓任务 #101：15 条挂在挂错标的
        `508031` 上的预测改指到 `510300` —— 挂错那只**给得出**这段窗口的净值，
        若把这道门也套在映射那一路，这类行就永远改不过来。
      · 没有映射行 ⇒ 没人替这块板块做过决定，是机器自己从内置表挑 ⇒ 把还能自证的搬走
        纯粹是拿新造的代理替换一份好结论，所以才有这道门。
    代价写在回执里（`predictions_with_verdict` + 路由那句话）：**清了几条结论必须让老板
    在点执行之前看见**，这一格因此同时钉那一句。
    """
    builtin, builtin_name = _builtin_target()
    _archive(test_db, builtin, builtin_name)          # 内置表给黄金的答案（补标那一路）
    _nav_in_window(test_db, 'MAP01', '映射行指定的标的')
    _nav_in_window(test_db, 'OWN01', '自己有好标的')
    test_db.add(SectorFundMapping(sector_name=GOLD, fund_code='MAP01',
                                  fund_name='映射行指定的标的', is_active=True,
                                  reviewed=True, confidence=0.95))
    pred = _prediction(test_db, fund_code='OWN01', verified=True)
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-mapped-1')
    test_db.refresh(pred)
    assert pred.fund_code == 'MAP01', \
        '板块明明有映射行、对齐规则也够用，却被补标那道门挡下（它自己那只标的好好的）'
    assert result['predictions_updated'] == 1, result
    assert result['predictions_kept_own_target'] == 0, \
        '"它自己问得出证据所以不动"这一档只属于补标那一路，映射那一路不许被它数进去'
    assert result['predictions_via_gap_fill'] == 0
    assert result['sectors_filled'] == 0, '这块有行，补标那一路根本不该为它动笔'
    # 范围之外的那一半：清了几条结论，回执必须自己数得出来（这一行被门放过去、结论真被清了）
    assert result['predictions_with_verdict'] == 1, result


def test_a_normalization_that_renames_the_sector_buys_no_target(test_db):
    """归一只许摘前后缀；把标签改成**另一块板块**时，补标那一路必须回答"不知道"。

    第 67 轮复评 MAJOR-3：`normalize_sector_name('债券')` 实测回 `'券商'`（`SECTOR_ALIASES`
    里那条别名走的是"字"而不是"词"），于是这块债券板块在内置表本来答不出标的
    （`get_fund_for_sector('债券')` 实测 None ⇒ 本该走"不猜、交人工/agent"那一档），
    归成 `券商` 就答得出 ⇒ 机器给一块债券板块绑上一只券商 ETF，写的行还署 `seed` 的名，
    看起来像机器审过。这比第 27 轮立第三态要拦的 `核聚变→红利低波` 更坏：那一档至少有字面关系。

    镜像 2026-09-29 现数：未判预测身上的 101 个板块标签里，被归一"改成别的词"的共 **3** 个
    （`债券→券商`、`贵金属→黄金`、`金融→黄金`），后两个在库里**没有映射行** ⇒ 今天真会走到这一格。
    复现命令（自包含内联，干净克隆可跑）逐字写在 `docs/模块总览/板块与基金匹配.md` 末尾那一节。
    """
    wrong_code, wrong_name = _builtin_target('券商')
    # 把"错的那只"喂足档案与净值：这样一挡，唯一能拦住它的就只剩标签这条规则
    _archive(test_db, wrong_code, wrong_name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db, code='DEAD11', name='停更的债券标的')
    pred = _prediction(test_db, sector='债券', fund_code='DEAD11')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-rename-1')
    test_db.refresh(pred)
    assert pred.fund_code == 'DEAD11', \
        '债券板块被归成"券商"、于是绑上了 %s %s ⇒ 归一把"答不出"买通成了"硬凑"' % (
            wrong_code, wrong_name)
    assert result['predictions_via_gap_fill'] == 0 and result['sectors_filled'] == 0, result
    kinds = {item['sector']: item['kind'] for item in result['sectors_refused_to_fill']}
    assert kinds.get('债券') == 'no_static_hit', \
        '这块板块该按"内置表也说不出对口标的"收口，回执却是 %r' % result['sectors_refused_to_fill']
    assert test_db.query(SectorFundMapping).filter_by(sector_name='券商').count() == 0, \
        '为别人的板块写了一行映射：下一次跑批它还会被再认领一次'


def test_two_affix_spellings_of_one_sector_share_one_plan_row(test_db):
    """同义词不合并（`绿色电力`/`绿电` 各自归一仍是自己），**词形**必须合并。

    这是 `_gap_label` 今天唯一真的在做的事：`黄金行情` 与 `黄金` 归一到同一个键 ⇒
    一块板块一行映射、两条预测各归各处。分成两行的后果不止是脏：生产库里有一条模型
    没声明的 `sector_fund_mapping_sector_name_key UNIQUE(sector_name)`（2026-09-22 直连
    `pg_constraint` 实测，镜像没有）⇒ 同一板块的第二行在生产直接撞约束（第 49 轮
    "镜像演练通过不等于生产能过"那一族）。
    ⚠ 别把这一格读成"归一挡住别名同义"：那种说法在 2026-09-29 被实测驳回（任务 #159）。
    """
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db, code='DEAD20', name='停更的标的甲')
    _stale_archive(test_db, code='DEAD21', name='停更的标的乙')
    first = _prediction(test_db, sector=GOLD, fund_code='DEAD20')
    second = _prediction(test_db, sector='黄金行情', fund_code='DEAD21')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-affix-1')
    assert [item['sector'] for item in result['sectors_to_fill']] == [GOLD], \
        result['sectors_to_fill']
    assert result['sectors_filled'] == 1 and test_db.query(SectorFundMapping).count() == 1, \
        '两种词形各写一行映射（生产那条 sector_name UNIQUE 会撞）'
    test_db.refresh(first)
    test_db.refresh(second)
    assert (first.fund_code, second.fund_code) == (code, code), \
        '计划表里只有归一后的那个键，另一种拼法的预测就永远沾不上补来的标的'


def test_the_preview_says_out_loud_how_many_conclusions_it_will_clear(test_db):
    """预览回执里必须有一句"这几条带着已判结论，执行会被清掉"（第 67 轮复评 MAJOR-5）。

    `reset_verified` 一直在逐行明细里，可从没人把它数成一句给老板看的话 ⇒ 页面上那句
    "预计更新 N 条"读起来像"挪一挪没事"，而镜像当天真会动的 28 条里 21 条带着结论
    （2026-09-29 现数，命令见 `docs/模块总览/板块与基金匹配.md` 末尾）。
    两条腿分开、但**各说各的数**（第 68 轮复评 MAJOR-1 + MINOR-2 改的就是这一格）：
    预览用计划数**预告**"执行会把旧结论清掉"；执行那一腿只报回查数 `verified_reset`
    （上方那句「重置 N 个已验证预测」），不许拿计划数再说一遍"已清掉"——
    上一批那句完成时念的是建候选时的计划值，桩掉唯一入口就会说出"0 条没做成、1 条已清掉"。
    """
    _nav_in_window(test_db, 'MAP02', '映射行指定的标的')
    _nav_in_window(test_db, 'OWN02', '自己有好标的')
    test_db.add(SectorFundMapping(sector_name=GOLD, fund_code='MAP02',
                                  fund_name='映射行指定的标的', is_active=True,
                                  reviewed=True, confidence=0.95))
    _prediction(test_db, sector=GOLD, fund_code='OWN02', verified=True)
    _prediction(test_db, sector=GOLD, fund_code='OWN02')      # 一条带结论、一条不带
    test_db.commit()

    plan = prediction_routes.sync_sector_mapping(request=_request(), dry_run=True, db=test_db)
    assert plan['data']['predictions_with_verdict'] == 1, plan['data']
    assert '其中 1 条带着已判结论' in plan['message'], plan['message']
    assert '这一轮还没动库' in plan['message'], plan['message']
    assert '已清掉' not in plan['message'], '预览不许用完成时'

    done = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                 dry_run=False, db=test_db)
    assert done['data']['predictions_with_verdict'] == 1, done['data']
    assert done['data']['verified_reset'] == 1, done['data']
    assert '重置 1 个已验证预测' in done['message'], done['message']
    assert '还没动库' not in done['message'], '执行完了还说"没动库"是反话'
    assert '带着已判结论' not in done['message'] and '已清掉' not in done['message'],         '执行这一腿只许说回查到的清除数，不许拿计划数把同一件事再说一遍：%s' % done['message']


def test_the_mapping_lookup_still_asks_with_the_raw_sector_label(test_db):
    """查映射用**原样标签**：`normalize_sector_name` 会吃前缀（实测 `RMAP白酒 → 白酒`）。

    先归一再查，库里那一行（键是 `RMAP黄金`）永远查不到 ⇒ "按板块对齐标的"整条路静默失效，
    而这条路上还挂着一道归一（`_gap_label`）——两种拼法混在一起，谁也对不上谁。
    归一**只**许用作补标计划表的键（`_lookup_mapping` 自己那三步里已经有归一那一臂）。
    """
    builtin, builtin_name = _builtin_target()
    _nav_in_window(test_db, builtin, builtin_name)     # 补标那一路答得出、也真会答
    _nav_in_window(test_db, 'MAPPED01', '带前缀板块的标的')
    _stale_archive(test_db, code='DEAD09', name='停更的标的')
    test_db.add(SectorFundMapping(sector_name='RMAP黄金', fund_code='MAPPED01',
                                  fund_name='带前缀板块的标的', is_active=True,
                                  reviewed=True, confidence=0.95))
    pred = _prediction(test_db, sector='RMAP黄金', fund_code='DEAD09')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-rawlabel-1')
    test_db.refresh(pred)
    assert pred.fund_code == 'MAPPED01', \
        '库里那一行是按预测自己那个标签登记的，却被归一后的键查走 ⇒ 换到了内置表那一只（%s）' \
        % pred.fund_code
    assert result['predictions_updated'] == 1 and result['sectors_filled'] == 0, result


def test_a_window_that_has_not_arrived_is_not_reported_as_evidenced(test_db):
    """"还没到问的时候"与"自己那只标的给得出净值"是两个数，各说各话（第 67 轮复评 MAJOR-8）。

    那道门问 `calendar_gap` "判得出来吗"，而它返回 None 有**四种**来路：真给得出、
    窗口还没到期、刚建档一笔净值都没有、窗口起点说不清。上一版把四格并成一个
    `predictions_kept_own_target`，页面上那句话就成了"它们自己那只标的就给得出这段窗口的净值"
    —— 镜像补标那一路 830 条候选里 **189 条（22.8%）只是因为还没到期**
    （2026-09-29 现数，见 `docs/模块总览/板块与基金匹配.md` 末尾）。
    放行这件事一个字没改（没到期当然不动它），**说出口的话分开**。
    """
    from datetime import timedelta

    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    today = date.today()
    # 一条 90 天期的预测：窗口起点已过、终点还在未来 ⇒ 末笔净值不早于起点，但点数本来就该在后面
    _nav_in_window(test_db, 'FUT01', '还在等的标的', start=today - timedelta(days=10),
                   end=today - timedelta(days=1))
    _prediction(test_db, sector=GOLD, fund_code='FUT01',
                window=(today - timedelta(days=10), today + timedelta(days=80)))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    assert result['predictions_kept_window_not_due'] == 1, result
    assert result['predictions_kept_own_target'] == 0, \
        '这段窗口还没到期，却被数成"自己那只标的给得出净值"'

    body = prediction_routes.sync_sector_mapping(request=_request(), dry_run=True, db=test_db)
    assert '1 条这段窗口还没到期' in body['message'], body['message']
    assert '自己那只标的就给得出' not in body['message'], \
        'kept 为 0 却还说了一遍「给得出」那一档 ⇒ 0 不该占一句：\n%s' % body['message']
    assert '没必要换成板块标的' not in body['message'], \
        '那句"给得出净值所以没必要换"对这一格是假话：\n%s' % body['message']


def test_the_answer_ruler_always_hands_back_three_slots():
    """这把尺子的**每一条出口**都得交出三格 —— 少一格就是把 `cause` 这件事悄悄抹掉。

    这一格是本批我自己造出来的那个缺陷的形状：把 `return 'cannot', (… % (a, b), None)` 写成
    这样时，Python 读出的是"两个元素，第二个是一格二元组"，于是调用方
    `kind, reason, cause = …` 当场 `ValueError`，而"原因那一格"在另一条路上（`[1]`）
    会悄悄变成 `(句子, None)` 递到页面上。⇒ 判的是**每一档的形状**，不是"有没有这个函数"。

    `no_start` 这一格在库面上今天造不出来（`predictions.prediction_date` 是 NOT NULL，
    起点说不清只剩"`_as_date` 解不出那个值"一条路），所以它由**这把尺子**钉住，
    不靠服务层的夹具 —— 与仓库规矩"看不见就明说看不见"一致。
    """
    from src.services.prediction_lifecycle import evidence_answer

    today = lc_current_as_of()
    shapes = {
        'no_start': (([], None, None, date(2026, 7, 8)), ('unknown', 'no_start')),
        'no_nav': (([], None, date(2026, 7, 1), date(2026, 7, 8)), ('unknown', 'no_nav')),
        '停更在窗口之前': (([], date(2025, 1, 1), date(2026, 7, 1), date(2026, 7, 8)),
                          ('cannot', None)),
        '还没到期': (([], date(2026, 7, 8), date(2026, 7, 1), today + timedelta(days=60)),
                     ('not_due', None)),
        '点数不够': (([today - timedelta(days=1)], date(2026, 7, 8), today - timedelta(days=30), today),
                     ('cannot', None)),
        '终点太旧': (([today - timedelta(days=39)], today - timedelta(days=39),
                     today - timedelta(days=30), today), ('cannot', None)),
        '给得出': (([today - timedelta(days=n) for n in range(0, 25)], today,
                   today - timedelta(days=20), today), ('evidenced', None)),
    }
    for name, (args, want) in shapes.items():
        got = evidence_answer(*args)
        assert isinstance(got, tuple) and len(got) == 3, \
            '%s 那一档交回来的不是三格（%r）⇒ 调用方按三格解就崩' % (name, got)
        kind, reason, cause = got
        assert kind == want[0], '%s 那一档判成 %s' % (name, kind)
        assert cause == want[1], '%s 那一档的来路应是 %r，实为 %r' % (name, want[1], cause)
        if kind == 'cannot':
            assert isinstance(reason, str) and reason, \
                '%s：判不出来就必须给出**一句人话**，不能是 %r' % (name, reason)
        else:
            assert reason is None, '%s：放行那一档不该带原因，实为 %r' % (name, reason)


def test_the_unknown_answer_gets_its_own_sentence_and_is_never_called_not_due(test_db):
    """门放行的每一档各有各的一句，而**同一批数在同一条消息里只许出现一次**。

    第 67 轮 MAJOR-8 拆出"给得出 / 还没到期"，第 68 轮 MINOR-1 拆出"这把尺子答不出"，
    第 69 轮 MINOR-6 再把"答不出"按**病因**拆成两格 —— 因为两者的动作相反：
      · `no_nav`   有档案、库里一笔净值都没有 ⇒ 跑一次「更新基金」就有答案；
      · `no_start` 这条预测连窗口起点都说不清 ⇒ 补多少净值都不会变，要动的是那条预测自己。
    上一版两种病因共用一句"先跑一次「更新基金」"，对后一半是配错药。

    ⚠ 这一格同时钉 MINOR-4：`buckets_spoken` 那半个条件原本只在 `kept` 一腿有牙
    （摘掉 `waiting` / `no_nav` 腿上的它，那份判据文件一声不响 —— 评审席实测）
    ⇒ 每档各数一次 `count(...) == 1`，预览与执行两路都数。
    ⚠ `no_start` 在这一格是 0：`predictions.prediction_date` NOT NULL ⇒ 库面上造不出那种行，
    它由上面那张尺子表与 M53 负责（不许因为"页面那句今天走不到"就把它当成已验过）。
    """
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    # ① no_nav：有档案、库里一行净值都没有，而窗口**已经到期**
    _archive(test_db, 'EMPTY01', '刚建档还没同步的标的')
    _prediction(test_db, sector=GOLD, fund_code='EMPTY01')
    # ② evidenced：自己那只标的就给得出这段窗口（没有它 kept 恒为 0 ⇒ "只说一遍"那条
    # 结构上不可能红；且必须用另一只代码 —— 挂在板块同一只标的上的行会被先数成 `unchanged`）
    _archive(test_db, 'OWN01', '自己有好标的')
    _nav(test_db, 'OWN01', '自己有好标的')
    _prediction(test_db, sector=GOLD, fund_code='OWN01')
    # ③ not_due：窗口还没到期 ⇒ 也不是"给得出"
    _archive(test_db, 'OWN02', '窗口还没到的那只')
    _nav(test_db, 'OWN02', '窗口还没到的那只')
    _prediction(test_db, sector=GOLD, fund_code='OWN02',
                window=(date(2026, 7, 1), date(2099, 7, 8)))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    assert result['predictions_kept_own_target'] == 1, result
    assert result['predictions_kept_window_not_due'] == 1, result
    assert result['predictions_kept_no_nav'] == 1, result
    assert result['predictions_kept_no_start'] == 0, result
    assert result['predictions_kept_answer_unknown'] == 1, \
        '总键必须等于两格之和（1 + 0）：%s' % result
    assert result['predictions_via_gap_fill_planned'] == 0, \
        '三档都放行 ⇒ 一条都不该被补标：%s' % result

    preview = prediction_routes.sync_sector_mapping(
        request=_request(), dry_run=True, db=test_db)['message']
    for phrase in ('自己那只标的就给得出', '还没到期', '还没有一笔净值'):
        assert preview.count(phrase) == 1, '"%s" 在这条回执里出现了 %d 次：\n%s' % (
            phrase, preview.count(phrase), preview)
    assert preview.count('这把尺子答不出') == 1, '两格病因被分成两句各说一遍：\n%s' % preview
    assert '跑一次「更新基金」' in preview, preview
    assert '补净值不会变' not in preview, 'no_start 那一格今天为 0，这句不许出现：\n%s' % preview

    # 执行那一路（摘要那句不触发）必须由**分句**接手，各一次、不许沉默
    receipt = prediction_routes.sync_sector_mapping(
        request=_request(CONFIRM), dry_run=False, db=test_db)['message']
    for phrase in ('自己那只标的就给得出', '还没到期', '还没有一笔净值'):
        assert receipt.count(phrase) == 1, '"%s" 在执行回执里出现了 %d 次：\n%s' % (
            phrase, receipt.count(phrase), receipt)
    assert '跑一次「更新基金」' in receipt, receipt
    assert '连窗口起点都说不清' not in receipt, receipt

    for held in ('EMPTY01', 'OWN01', 'OWN02'):
        assert test_db.query(Prediction).filter_by(fund_code=held).one().fund_code == held, \
            '%s 那一档本不该动它' % held


def test_a_round_that_moves_nothing_never_points_at_a_line_that_never_printed(test_db):
    """四档全为 0 时那句不许自指屏幕上不存在的栏（第 69 轮 MINOR-7，第 53 轮 A-1 同形）。

    这一格**可达**，而且就在本文件最早踩到的那个形状上：预测的标的**正好等于**内置表给这个
    板块的那只 ⇒ 先被数成 `unchanged`，压根走不到证据门 ⇒ 板块仍然 `fillable`、四档计数全 0。
    上一版那句"（按上面三档各自的原因）"在这种回执里指向三句一句都没渲染的话。

    ⚠ 夹具换一块板块标签、净值也换一个日期区间：本文件的 `test_db` 会把前面几格的行留在库里，
    与 `(fund_code, nav_date)` 的唯一约束撞车（第一次就是这么红的）。
    """
    label = '券商'
    code, name = _builtin_target(label)
    _archive(test_db, code, name, with_nav_for=(date(2026, 8, 3), date(2026, 8, 12)))
    _prediction(test_db, sector=label, fund_code=code,
                window=(date(2026, 8, 3), date(2026, 8, 10)))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    assert result['predictions_unchanged'] >= 1, result
    assert result['predictions_kept_own_target'] == 0, result
    assert result['predictions_kept_window_not_due'] == 0, result
    assert result['predictions_kept_no_nav'] == 0, result
    assert result['predictions_kept_no_start'] == 0, result
    assert not result['sectors_to_fill'], (
        '这一格要的是「四档全 0 且没进补标名单」那一句，而本轮 %s 真进了补标 ⇒ '
        '把上面那块板块标签换成内置表里另一块没人要补的。⚠ 第 70 轮复评 J-2：这里原来写的是 '
        '`pytest.skip` —— 一放过就没人能答"这一格今天到底跑没跑"（屏幕上数得出 16 条 skipped，'
        '多一条少一条都看不见），而仓库的规矩是不可达/没走到的分支不许用跳过装成验过'
        % result['sectors_to_fill'])

    message = prediction_routes.sync_sector_mapping(
        request=_request(), dry_run=True, db=test_db)['message']
    assert '一块都不动' in message, message
    assert '这一轮没有一条挂在' in message, \
        '四档全 0 时这一句必须自己把话说完，不许留下空冒号：\n%s' % message
    for pointing in ('见上方', '按上面', '如上', '见明细'):
        assert pointing not in message, '这句指向一个不存在的栏：%s' % message


def test_a_target_with_no_archive_is_not_the_same_as_being_evidenced(test_db):
    """门认的是**档案表**里的那只标的：预测挂在一只库里没有档案的代码上 ⇒ 该动就动。

    第 67 轮复评 MAJOR-9②：`prediction.fund_code in archived` 这一腿当时**没有任何一格用例问过**
    （把 `in archived` 换成恒真，全套件 40 条一声不响）。档案与净值是两件事：
    `fund_history` 里有行而 `fund_info` 没档案是第 50 轮那族悬空行，
    "日历给得出点"不等于"这只标的真是库里登记的那只基金"。
    """
    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _prediction(test_db, sector=GOLD, fund_code='GHOST01')     # 档案表里没有这个代码
    # 悬空代码也要**点数够**：只给一笔的话，`calendar_gap` 因为点数不足本来就答"判不出来"，
    # 这一格就分不清是被档案那一腿拦下的、还是被阈值拦下的（第 67 轮复评 MAJOR-9② 的现场：
    # 我第一版就给了 1 笔，变异 `in archived` → 恒真时它仍动 ⇒ M44 报 GREEN）
    for offset in range(max(2, config.VERIFY_MIN_DATA_POINTS)):
        test_db.add(FundHistory(fund_code='GHOST01', fund_name='悬空代码',
                                 nav_date=date(2026, 7, 1) + timedelta(days=offset),
                                 nav=1.0 + offset * 0.01, day_growth=0.1))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id='t-noarch-1')
    pred = test_db.query(Prediction).one()
    assert pred.fund_code == code, \
        '它自己那只代码在库里压根没有档案，却被当成"给得出证据的自有标的"留下 ⇒ %s' % pred.fund_code
    assert result['predictions_kept_own_target'] == 0, result
    assert result['predictions_kept_window_not_due'] == 0, result


def test_the_execute_sentence_uses_the_recount_not_the_plan(test_db, monkeypatch):
    """完成时那句里的条数必须是**改完之后回查**的那一份（第 67 轮复评 MAJOR-9③）。

    `predictions_via_gap_fill_planned`（建候选时的计划）与 `predictions_via_gap_fill`
    （动完之后看行上代码数出来的）是两个键，而上一版那条用例里两者恰好都是 1 ⇒
    把路由改成念计划那一份，全套件 40 条全绿。这里把唯一入口换成"什么都不做"的桩，
    让两个数真的分叉（计划 1 / 做到 0），那句话必须念 0。
    """
    from src.fund import fund_sync_manager

    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    pred = _prediction(test_db)
    test_db.commit()

    monkeypatch.setattr(fund_sync_manager.FundSyncManager, 'retag_prediction',
                        staticmethod(lambda *a, **k: False))
    body = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                 dry_run=False, db=test_db)
    data = body['data']
    test_db.refresh(pred)
    assert data['predictions_via_gap_fill_planned'] == 1, data
    assert data['predictions_via_gap_fill'] == 0, data
    assert pred.fund_code == 'DEAD01', '桩什么都没做，行上代码却被改了'
    assert '把 0 条问不出证据的预测改到它身上' in body['message'], body['message']
    assert '把 1 条' not in body['message'], '一行都没动却拿计划数说"已经改了 1 条"'


def test_the_three_refusal_kinds_nobody_had_ever_asked_about(test_db):
    """六种拒收里有三种从没被断言过（第 67 轮复评 MINOR-10）：长名、同码、零净值。

    路由那句话是**按 `kind` 数出来**的（MA-3 那一次改的），而今天镜像现读的拒收分布只有
    `{no_static_hit: 10}` ⇒ 那三种分支的文案与计数从来没有一次真的被走到过。
    这里一次造齐三种，各数各的、各说各的：
      · `label_unusable` 板块名长过列宽（`String(50)`，长过它在生产 PostgreSQL 直接报错）；
      · `same_as_current` 内置表给的正是库里那只（换了等于没换，不能新建一行同码映射）；
      · `no_nav` 有档案、库里却一行净值都没有。
    """
    from src.constants.sector_fund_map import get_fund_for_sector

    liquor = '白酒'
    hit = get_fund_for_sector(liquor) or {}
    assert hit.get('code'), '内置表里 白酒 这一行没了 ⇒ 换一个板块标签'
    # ① 同一板块的既有行用的正是内置表那只（is_active=0 ⇒ 对同步器不可见 ⇒ 走补标那一路）
    _archive(test_db, hit['code'], hit.get('name') or hit['code'],
             with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db, code='DEAD30', name='停更的标的甲')
    test_db.add(SectorFundMapping(sector_name=liquor, fund_code=hit['code'],
                                  fund_name=hit.get('name') or hit['code'],
                                  is_active=False, reviewed=True))
    _prediction(test_db, sector=liquor, fund_code='DEAD30')
    # ② 内置表给得出、但那只在库里一行净值都没有
    steel = '钢铁'
    steel_hit = get_fund_for_sector(steel) or {}
    if not steel_hit.get('code'):      # 内置表没有这一行就换一个答得出的（判据不抄常量）
        steel, steel_hit = next((k, v) for k, v in
                                __import__('src.constants.sector_fund_map',
                                           fromlist=['SECTOR_FUND_MAP']).SECTOR_FUND_MAP.items()
                                if v.get('code')), steel_hit
        steel_hit = get_fund_for_sector(steel)
    _archive(test_db, steel_hit['code'], steel_hit.get('name') or steel_hit['code'])
    _stale_archive(test_db, code='DEAD31', name='停更的标的乙')
    _prediction(test_db, sector=steel, fund_code='DEAD31')
    # ③ 板块名长过列宽
    _prediction(test_db, sector='黄' * 51, fund_code='DEAD32')
    _stale_archive(test_db, code='DEAD32', name='停更的标的丙')
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)
    kinds = {item['sector']: item['kind'] for item in result['sectors_refused_to_fill']}
    assert kinds.get(liquor) == 'same_as_current', result['sectors_refused_to_fill']
    assert kinds.get(steel) == 'no_nav', result['sectors_refused_to_fill']
    assert 'label_unusable' in set(kinds.values()), kinds

    body = prediction_routes.sync_sector_mapping(request=_request(), dry_run=True, db=test_db)
    message = body['message']
    for phrase in ('内置表给的就是库里那只标的', '在库里一行净值都没有', '长过列宽'):
        assert phrase in message, '%s 那句话没被说出来：\n%s' % (phrase, message)

def test_the_verdict_count_is_promised_in_the_preview_not_claimed_in_the_receipt(test_db, monkeypatch):
    """「其中 N 条带着已判结论」是一句**预告**，不是收据（第 68 轮复评 MAJOR-1 + MINOR-2）。

    `predictions_with_verdict` 数的是建候选时那份**计划**；执行那一路真清掉几条由 `verified_reset`
    回查得出，而路由上方已经用它说过一句「重置 N 个已验证预测」。上一批这句按 `dry_run` 分了两支，
    **完成时那一支念的还是计划数** —— 与本批自己在同函数上方 60 行立的规矩②（完成时只配真做完的数）
    直接打脸，也正是 M45 刚治过的那一族。这里拿作者自己那格招（桩掉唯一入口）把两个数掰开：
    计划 1 / 做到 0 ⇒ 执行那一句不许出现"带着已判结论 / 已清掉"，预览那一句必须有。
    """
    from src.fund import fund_sync_manager

    code, name = _builtin_target()
    _archive(test_db, code, name, with_nav_for=(date(2026, 7, 1), date(2026, 7, 8)))
    _stale_archive(test_db)
    pred = _prediction(test_db)
    pred.is_correct = True          # 带着已判结论 ⇒ 改标要把旧结论清掉
    pred.verify_score = 40
    test_db.commit()

    preview = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                    dry_run=True, db=test_db)
    assert preview['data']['predictions_with_verdict'] == 1, preview['data']
    assert '其中 1 条带着已判结论' in preview['message'], preview['message']

    monkeypatch.setattr(fund_sync_manager.FundSyncManager, 'retag_prediction',
                        staticmethod(lambda *a, **k: False))
    body = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                 dry_run=False, db=test_db)
    data = body['data']
    assert data['predictions_with_verdict'] == 1 and data['verified_reset'] == 0, data
    assert '带着已判结论' not in body['message'] and '已清掉' not in body['message'],         '一条结论都没清（桩什么都没做），却拿计划数说"已清掉"：%s' % body['message']


def test_the_no_start_sentences_are_pinnable_without_a_library_row(monkeypatch):
    """`no_start` 那两句必须各有一格判据，而**造出那两句不需要库里那一行**（第 70 轮 J-1）。

    第 69 轮撤 M52 时写的理由是"`prediction_date` 是 NOT NULL ⇒ 夹具对它没有牙"。前半句是
    事实（`database.py:221`，且 `_as_date()` 只认 date/datetime），后半句**把两件事混成一句**：
    路由那句回执读的是服务交回的**那个计数**，而计数是一份字典 —— 把
    `PredictionMaintenanceService` 换成桩就能造（本文件
    `test_the_execute_sentence_uses_the_recount_not_the_plan` 早就是这个手段）。
    后果：路由里 `no_start` 那两句（预览摘要那一格 + 执行那一路的分句）零判据零变异。

    这一格两路各问各的：
      ① 预览那一格**说了**这一档，并且给的是**它自己的药**（补净值不会变，要动的是起点日期）；
      ② 同一句里 `no_nav` 那半给的是「更新基金」—— 两种药不许并成一句（第 69 轮 MINOR-6）；
      ③ 执行那一路（上面那句不渲染）由分句接手：两句都在、各说一次、不许缺席。
    """
    counts = {'predictions_updated': 0, 'verified_reset': 0, 'funds_added': 0,
              'funds_sector_updated': 0, 'predictions_unchanged': 0,
              'predictions_no_mapping': 0, 'predictions_skipped_unservable': 0,
              'would_update': 0,
              'predictions_via_gap_fill_planned': 0, 'predictions_via_gap_fill': 0,
              'predictions_with_verdict': 0,
              'sectors_to_fill': [], 'sectors_filled': 0,
              'sectors_fillable': ['A', 'B', 'C', 'D', 'E'],
              'sectors_refused_to_fill': [],
              # 四档都非零 ⇒ 预览那句"一块都不动"把四档一次说完（buckets_spoken 为真），
              # 而执行那一路这一句根本不渲染 ⇒ 同一份数两条腿两种说法，各判各的。
              'predictions_kept_own_target': 481,
              'predictions_kept_window_not_due': 155,
              'predictions_kept_no_nav': 1,
              'predictions_kept_no_start': 3,
              'predictions_kept_answer_unknown': 4}

    class _Stub:
        def __init__(self, db):
            pass

        def sync_sector_mappings(self, dry_run=True, run_id=None):
            return dict(counts)

    def say(dry):
        monkeypatch.setattr(prediction_routes, 'PredictionMaintenanceService', _Stub)
        body = prediction_routes.sync_sector_mapping(request=_request(CONFIRM),
                                                     dry_run=dry, db=None)
        assert body['success'] is True, body
        return body['message']

    # ---- 预览：这一档要在"一块都不动"那一格里说出**它自己的**药
    preview = say(True)
    assert '3 条连窗口起点都说不清' in preview, preview
    assert preview.count('连窗口起点都说不清') == 1, (
        '这一档在预览那句里被说了两遍（摘要格 + 分句）：' + preview)
    tail = preview.split('3 条连窗口起点都说不清', 1)[1].split('，')[0].split('、')[0]
    assert tail.startswith('⇒ 补净值不会变'), (
        '给「窗口起点说不清」配的药必须是"补净值不会变"，实得 %r' % tail)
    assert '更新基金' not in tail, '「起点说不清」那半行补多少净值都不会变 ⇒ 配错药' + tail
    nav_tail = preview.split('1 条那只标的库里还没有一笔净值', 1)[1].split('、')[0]
    assert '更新基金' in nav_tail and '补净值不会变' not in nav_tail, nav_tail
    assert '那条预测连窗口起点都说不清' not in preview, (
        '执行那一路那句分句不该在预览里出现（同一批数说两遍）')

    # ---- 执行：上面那句不渲染 ⇒ 四档分句各说一句，`no_start` 这一腿不许缺席
    done = say(False)
    assert done.count('那条预测连窗口起点都说不清') == 1, done
    assert '3 条预测也没动' in done, done
    assert '跑多少次「更新基金」都不会变' in done, done
    assert '要动的是那条预测自己的起点日期（重新分析那条帖子）' in done, done
    assert '那只标的在库里还没有一笔净值' in done, done
    assert '1 条预测也没动' in done, done
