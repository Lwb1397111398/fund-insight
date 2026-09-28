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
镜像 dry-run 印 `would_update 657` —— 一次按钮会清掉几百条已有结论，正是第 18 轮
"一键清空 515 条结论"的形状。`test_a_row_that_can_already_be_evidenced_is_left_alone`
钉的就是这道门；把那道 `continue` 摘掉，它必须当场红。
"""

from datetime import date, timedelta

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

GOLD = '黄金'


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


def _prediction(db, *, sector=GOLD, fund_code='DEAD01', verified=False,
                window=None):
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
                       sector=sector, prediction_type='up', prediction_content='看涨',
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
    assert plan['predictions_via_gap_fill'] == run['predictions_via_gap_fill']
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
