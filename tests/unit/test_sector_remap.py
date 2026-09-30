# -*- coding: utf-8 -*-
"""板块映射 → 预测纠偏的置信度门槛、别名命中、run_id 回滚。

这是 S4 第 4 步的安全网：批量改预测的 fund_code 会牵动准确率台账，
没有门槛与可回滚就等于拿老板的历史数据冒险。
"""
from datetime import date, timedelta

import pytest

from src.models.database import (
    Blogger, FundHistory, FundInfo, Post, Prediction, PredictionChangeLog,
    SectorAlias, SectorFundMapping,
)
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from scripts.restore_prediction_batch import apply_before_state


def _seed(db, *, sector='RMAP白酒', mapping_fund='RMAP01', confidence=None,
          reviewed_by=None, owner_locked=None, pred_fund='999999',
          verified=False, alias=None, sector_type=None, mapping_sector=None,
          window=None, next_verify_date=None):
    blogger = Blogger(name='回滚测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='测试帖子', post_date=date(2026, 6, 1))
    db.add(post)
    db.flush()
    for code in (mapping_fund, pred_fund):
        if not db.query(FundInfo).filter_by(fund_code=code).first():
            db.add(FundInfo(fund_code=code, fund_name='测试基金' + code))
    db.add(SectorFundMapping(sector_name=mapping_sector or sector, fund_code=mapping_fund,
                             fund_name='测试基金' + mapping_fund, reviewed=True,
                             is_active=True, confidence=confidence,
                             reviewed_by=reviewed_by, owner_locked=owner_locked))
    if alias:
        if not db.query(SectorAlias).filter_by(alias_name=alias).first():
            db.add(SectorAlias(alias_name=alias, sector_name=sector, source='agent'))
    start, target = window or (date(2026, 6, 1), date(2026, 6, 8))
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code=pred_fund,
        fund_name='测试基金' + pred_fund, sector=alias or sector, sector_type=sector_type,
        prediction_type='up',
        prediction_content='上涨', prediction_date=start, prediction_period='1周',
        target_date=target,
        next_verify_date=next_verify_date,
        status='success' if verified else 'pending',
        is_correct=True if verified else None,
        verify_count=1 if verified else 0,
        verify_score=80 if verified else None,
        start_nav=1.0 if verified else None,
        end_nav=1.2 if verified else None,
    )
    db.add(prediction)
    db.commit()
    return prediction


@pytest.fixture(autouse=True)
def _clean_remap_rows(db_session):
    """这些用例会 commit，测试之间会互相看见，用 RMAP 前缀隔离并跑完清理。"""
    yield
    for model in (PredictionChangeLog, Prediction, SectorFundMapping, SectorAlias, Post, Blogger):
        db_session.query(model).delete()
    db_session.commit()


def _service(db):
    return PredictionMaintenanceService(db)


def _stale_nav(db, code, last=date(2020, 12, 8)):
    """档案在、净值只到 `last` —— 生产上 `003033` 就是这个形状，任务 #171 那六条挂的就是它。

    对 `2026-06-01~06-08` 这段窗口，`calendar_answer` 答的是 `cannot`（末笔早于窗口起点 ⇒
    这段净值不会再来），这正是老板那句"抓取不到且确认没有办法 ⇒ 换成别的基金"圈的格子。
    """
    db.add(FundHistory(fund_code=code, fund_name='测试基金' + code, nav_date=last,
                       nav=1.0, day_growth=0.0))
    db.flush()


def _navs(db, code, dates):
    """给这只标的按日铺净值 —— 三个点（起点/中间/目标日）才过 `calendar_gap` 那道门。"""
    for day in dates:
        db.add(FundHistory(fund_code=code, fund_name='测试基金' + code,
                           nav_date=day, nav=1.0, day_growth=0.1))
    db.flush()


def _alternate_library_row(db, label='RMAP科技', code='RMAPAL0', *, dates=None):
    """第二根标签在库里**有**一行合格映射，并且那只标的回答得出这段窗口的净值。"""
    db.add(SectorFundMapping(sector_name=label, fund_code=code,
                             fund_name='测试基金' + code, reviewed=True,
                             is_active=True, confidence=0.95))
    db.add(FundInfo(fund_code=code, fund_name='测试基金' + code))
    _navs(db, code, dates if dates is not None
          else (date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 8)))
    db.commit()


def test_low_confidence_mapping_is_not_applied(db_session):
    _seed(db_session, confidence=0.5)
    result = _service(db_session).sync_sector_mappings(dry_run=False,
                                                       min_confidence=0.85,
                                                       run_id='t-low')
    assert result['predictions_updated'] == 0
    assert result['mappings_skipped_low_confidence'] == 1


def test_high_confidence_mapping_rewrites_and_resets(db_session):
    prediction = _seed(db_session, confidence=0.95, verified=True)
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-high')
    assert result['predictions_updated'] == 1
    assert result['verified_reset'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'
    assert prediction.status == 'pending'
    assert prediction.is_correct is None
    assert prediction.start_nav is None


def test_owner_locked_mapping_bypasses_confidence(db_session):
    prediction = _seed(db_session, confidence=None, owner_locked=True)
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-owner')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'


def test_alias_lets_synonym_sector_match(db_session):
    prediction = _seed(db_session, sector='RMAP绿电', confidence=0.95, alias='RMAP绿色电力')
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-alias')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'


def test_the_library_alias_arm_is_asked_with_the_raw_label_not_the_normalized_one(db_session):
    """库里别名那一臂**必须收原样串**（第 85 轮 A-5，变异 M88）。

    上一邻那条用例（`RMAP绿电` / `RMAP绿色电力`）**喂不出差别**，但原因不是"结构上无牙"
    （第 85 轮 MI-4 驳回了我上一版那句"两种情形都不红"）：它取决于**进程内那份别名缓存的跑序**。
    现读实测（只读门 + 把 `_DB_ALIASES_CACHE` 各摆成两种状态，命令逐字写在
    `docs/模块总览/板块与基金匹配.md` 那一节末尾）：
      缓存里有 `RMAP绿色电力→RMAP绿电` ⇒ 归一第 3 步命中 ⇒ 交回 `RMAP绿电`，
        而它不是原标签的字面 ⇒ `_gap_label` 打回原样 ⇒ ③ 换不换键**等价**（老判据不红）；
      缓存是空的 ⇒ 归一第 4 步用 `SECTOR_FUND_MAP` 的键 `电力` 做子串命中 ⇒ 交回 `电力`，
        而 `电力` **是**原标签的字面 ⇒ `_gap_label` 放行 ⇒ ③ 的键真变了、映射表里没有 `电力` 这一行
        ⇒ 老判据在这一格**会红**。
    ⇒ 老判据有没有牙由"谁先跑了那一次 `_load_db_aliases()`"决定，这正是要换夹具的理由：

    有牙的形状要让归一**改词**、而改出来的词与库里别名指向的那块板块**不是同一个**：
    `生物医药` 在硬编码的 `SECTOR_ALIASES` 里就压着一条 `→ 医药`（不依赖
    `_load_db_aliases()` 那份进程内缓存，所以不受用例先后影响），而库里登记的别名是
    `生物医药 → RMAP药`。于是 `normalized = '医药'` 在映射表里没有行 ⇒ ②交不出人，
    只有③拿**原样** `生物医药` 去问别名表才查得到 `RMAP药` ⇒
    把③的键改成 `normalized`，这一条当场从"绑到 RMAP01"变成"一条都没改"。
    """
    prediction = _seed(db_session, sector='RMAP药', confidence=0.95, alias='生物医药')
    from src.constants.sector_fund_map import normalize_sector_name
    assert normalize_sector_name('生物医药') == '医药', \
        '夹具前提：归一必须把标签改成另一块板块，否则这条用例又变成描述自己'
    # 夹具前提二（第 85 轮 MI-3）：②交不出人这件事必须由**这一条用例自己**证明，
    # 不能靠"别的文件恰好清了映射表"——那个前提由别人的跑序决定，跟缓存那一格同病。
    assert db_session.query(SectorFundMapping).filter_by(sector_name='医药').first() is None, \
        '夹具前提二：映射表里不许有 `医药` 这一行，否则②自己就命中、③根本走不到，这条又变成描述自己'
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-alias-raw')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'


def test_run_id_written_and_rollback_restores_previous_state(db_session):
    from src.utils.blogger_stats import recalculate_blogger_stats

    prediction = _seed(db_session, confidence=0.95, verified=True)
    pid, old_fund, old_status = prediction.id, prediction.fund_code, prediction.status
    _service(db_session).sync_sector_mappings(dry_run=False, min_confidence=0.85,
                                              run_id='t-rollback')
    logs = db_session.query(PredictionChangeLog).filter_by(run_id='t-rollback').all()
    assert len(logs) == 1, 'run_id 必须写进变更日志，否则无法整批回滚'
    assert logs[0].before_state['fund_code'] == old_fund

    # 回滚：取该 run 最早一条 before_state 全量还原（走脚本里那份真代码，
    # 别在测试里抄一遍遍历 —— 抄的那份永远测不出清单少字段）
    log = min(logs, key=lambda x: x.id)
    apply_before_state(prediction, log.before_state)
    db_session.flush()
    recalculate_blogger_stats(db_session, prediction.blogger_id, commit=False)
    db_session.commit()
    db_session.refresh(prediction)
    assert prediction.fund_code == old_fund
    assert prediction.status == old_status
    assert prediction.is_correct is True
    assert prediction.start_nav == 1.0


def test_sector_label_that_hits_nothing_falls_back_to_sector_type(db_session):
    """`sector` 那个词在库里问不出映射时，必须再问一次 `sector_type`。

    任务 #171（镜像现读，2026-09-30）：6 条"自己那只标的问不出这段窗口"的预测带着
    sector='A股'（另有 1 条 '粮食'），而 sector_type='综合'/'科技'/'宽基'/'农业' ——
    后头那几块板块**在库里有合格映射行**（018536 / 510300，`_mapping_eligible` 实测 True）、
    内置表也答得出标的（科技→515000、农业→159825）。旧写法 `raw = sector or sector_type`
    在 `sector` 非空时压根不看后者 ⇒ 这 6 行全落进 `predictions_no_mapping`（回执 14），
    永远走不到改标那一路，只能等"关进回收站"。老板要的是**换成其他好的基金**。
    """
    prediction = _seed(db_session, sector='RMAPA股', sector_type='RMAP科技',
                       mapping_sector='RMAP科技', mapping_fund='RMAP02', confidence=0.95)
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-fallback')
    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP02'


def test_the_first_sector_label_still_wins_when_it_has_its_own_row(db_session):
    """回落只许发生在"第一根标签什么都问不出"时 —— `sector` 命中就不许再看 `sector_type`。

    防的是把回落做成"永远拿 sector_type 定价"：那样板块页上逐行审过的 `sector` 那一行
    会被架空，第 44 轮那条"闸门建成墙/门建反了"同族。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_fund='RMAP01', confidence=0.95)
    db_session.add(SectorFundMapping(sector_name='RMAP科技', fund_code='RMAP09',
                                     fund_name='测试基金RMAP09', reviewed=True,
                                     is_active=True, confidence=0.95))
    db_session.add(FundInfo(fund_code='RMAP09', fund_name='测试基金RMAP09'))
    db_session.commit()
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-precedence')
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAP01'
    assert [d['new_fund_code'] for d in result['details']] == ['RMAP01']


def test_a_stuck_row_moves_to_the_second_label_when_the_first_one_cannot_be_judged(db_session):
    """第一根标签**有**映射行、但那只标的这段窗口永远问不出来 ⇒ 再问第二根标签。

    任务 #171 那一族里最隐蔽的一格：`sector` 命中就不再回落，这本身对（板块页逐行审过的
    那一行不能被架空，见 `test_the_first_sector_label_still_wins_when_it_has_its_own_row`）。
    可镜像现读那六条正是长这样：`515440` 挂在「A股/综合」那一行上、末条净值停在 2020-12-08，
    按第一根标签**永远**问不出这段窗口 —— 于是它既进不了改标那一路，也永远躺在「到期未判」。
    老板要的是「换成其他好的基金」，所以这里放行的是**备选**，判据仍是共用那把尺子。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPST1',
                       pred_fund='RMAPST1', confidence=0.95)
    _stale_nav(db_session, 'RMAPST1')
    _alternate_library_row(db_session, code='RMAPAL1')

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-stuck-to-alt')

    assert result['predictions_updated'] == 1
    assert result['predictions_unchanged'] == 0
    assert [d['via_gap_fill'] for d in result['details']] == [False], \
        '这一格走的是库里第二根标签的映射行，不是内置表补标那一路'
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPAL1'


def test_a_row_that_has_not_arrived_yet_is_not_moved_off_its_own_target(db_session):
    """上一条的控制组：形状一模一样，只把窗口搬到未来 ⇒ 一条都不许动。

    这一段窗口的净值**还没到期**，"问不出来"这件事今天还没成立（`evidence_answer` 先判
    `cannot` 再判 `not_due`，所以"能不能问出来"这一腿确实是 `cannot` —— 真正把这一行留在原地的
    是"它还没到该出结论的那天"，即 `_waiting_for_a_verdict`）。
    备选那只标的的净值也铺在未来这段窗口里，所以**假如**它被放行就一定走得掉：
    这一行留在原处只可能是"还没到期"那一腿的功劳，不是证据门拦的。
    """
    start = date.today() + timedelta(days=30)
    target = start + timedelta(days=7)
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPST2',
                       pred_fund='RMAPST2', confidence=0.95,
                       window=(start, target))
    _stale_nav(db_session, 'RMAPST2')
    _alternate_library_row(db_session, code='RMAPAL2',
                           dates=(start, start + timedelta(days=3), target))

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-not-due-yet')

    assert result['predictions_updated'] == 0
    assert result['predictions_unchanged'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPST2'


def test_a_row_whose_own_target_gives_the_evidence_is_never_swapped(db_session):
    """自己那只标的就给得出这段窗口 ⇒ 第二根标签再好也不许换（库里这一腿的"只紧不松"）。

    回落这一族最容易长成的样子是"永远拿第二个答案定价"。这一格钉死方向：
    备选是有净值、有映射行的合格标的，判据通过的却是**当前**那只 ⇒ 一条都不动。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPEV1',
                       pred_fund='RMAPEV1', confidence=0.95)
    _navs(db_session, 'RMAPEV1', (date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 8)))
    _alternate_library_row(db_session, code='RMAPAL3')
    db_session.commit()

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-own-evidence')

    assert result['predictions_updated'] == 0
    assert result['predictions_unchanged'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPEV1'


def test_no_nav_in_the_library_is_a_sync_job_not_a_reason_to_change_fund(db_session):
    """「库里一笔净值都没有」不许被当成"这段问不出来"⇒ 不换基金。

    `evidence_answer` 交回的第三格 `cause` 管的就是这件事：`no_nav` 的药是跑一次「更新基金」，
    `no_start` 的药是改那条预测的起点日期，**都不是**"换一只基金"（第 69 轮 MINOR-6 那把尺子）。
    只有 `cannot`（这段净值不会再来）才允许挪。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPNN1',
                       pred_fund='RMAPNN1', confidence=0.95)
    _alternate_library_row(db_session, code='RMAPAL4')

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-no-nav-not-a-move')

    assert result['predictions_updated'] == 0
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPNN1'


def test_a_row_under_a_re_ask_lock_still_moves_and_the_lock_goes_back(db_session):
    """压着「重问锁」的行也算"还在等结论"⇒ 该挪就挪，且挪的时候把锁退回目标日。

    `has_verdict_trace` 不读 `next_verify_date`（它问的是"这行有没有结论"），所以结构性不可验
    那一档的行在这里不会被误当成"已经判过"；而 `retag_prediction` 自带那句
    "压在旧标的上、晚于目标日的那根日期退回目标日" —— 换到新标的后第一次问就该问，
    不许让旧标的留下的锁把新标的也锁到 10 月。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPST5',
                       pred_fund='RMAPST5', confidence=0.95,
                       next_verify_date=date(2026, 10, 20))
    _stale_nav(db_session, 'RMAPST5')
    _alternate_library_row(db_session, code='RMAPAL5')

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-lock-travels')

    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPAL5'
    assert prediction.next_verify_date == date(2026, 6, 8), \
        '锁必须退回目标日，绝不能继续压在 2026-10-20'


def test_an_alternate_that_cannot_be_evidenced_either_is_not_a_way_out(db_session):
    """另一根标签给出的那只**也**问不出这段窗口 ⇒ 一条都不动。

    老板要的是"换成其他**好的**基金"，不是"从一只停更的换到另一只停更的"。这一格也是
    回落那一路唯一会写错的方向：只判"有没有第二个答案"就换，等于把行在两只验不了的标的
    之间搬来搬去，还清掉了原标的上那些本来就问不出的证据。
    """
    prediction = _seed(db_session, sector='RMAP白酒', sector_type='RMAP科技',
                       mapping_sector='RMAP白酒', mapping_fund='RMAPST6',
                       pred_fund='RMAPST6', confidence=0.95)
    _stale_nav(db_session, 'RMAPST6')
    _alternate_library_row(db_session, code='RMAPAL6',
                           dates=(date(2020, 12, 1), date(2020, 12, 8)))

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-alt-also-dead')

    assert result['predictions_updated'] == 0
    assert result['predictions_unchanged'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPST6'


def test_a_row_that_neither_label_can_answer_stays_put(db_session):
    """两根标签都问不出 ⇒ 仍然一条不动（回落不许把"没人定价的板块"硬凑一只标的）。"""
    prediction = _seed(db_session, sector='RMAP无人板块', sector_type='RMAP也没这块',
                       mapping_sector='RMAP白酒', mapping_fund='RMAP03', confidence=0.95)
    before = prediction.fund_code
    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-both-miss')
    assert result['predictions_updated'] == 0
    db_session.refresh(prediction)
    assert prediction.fund_code == before


def test_a_normalization_that_renames_the_sector_buys_no_mapping_row(db_session):
    """任务 #172：`normalize_sector_name` 把 `金融` 归成 `黄金`（共用一个"金"字的模糊别名），
    这种归一**不配**替一块板块选出映射行。

    生产 2026-09-30 实测：144 行待改标里 **81 行**是这一族（板块 `金融` 被绑到
    `518880 黄金ETF华安` 那一行映射上）。第 67 轮 MAJOR-3 那道"归一结果必须是原样标签的子串"
    当时只装在 `_gap_label`（补标那一路），没装在 `_lookup_mapping`（既有映射改标这一路）⇒
    同一个词形归一，两条路两种待遇。

    钱在 `SECTOR_CATEGORIES['金融']` 里明列着 `银行/券商/保险`，`get_fund_for_sector('金融')`
    答的是 `001594`（银行 ETF 联接）—— 所以这不是"没人给金融定价"，是**拿错行的定价**。
    """
    prediction = _seed(db_session, sector='金融', mapping_sector='黄金',
                       mapping_fund='RMAPGOLD', pred_fund='RMAPFIN', confidence=0.95)
    _navs(db_session, 'RMAPGOLD', (date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 8)))
    _navs(db_session, 'RMAPFIN', (date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 8)))

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-rename-not-buy')

    assert result['predictions_updated'] == 0, \
        '归一成了别的板块（金融 → 黄金），却拿那块板块的映射行改了标 ⇒ #172 那一族又开门了'
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPFIN'
    assert prediction.status == 'pending'


def test_an_affix_spelling_still_finds_the_row_it_normalized_to(db_session):
    """反面控制（不许把门建成墙）：`黄金行情` 归一成 `黄金`，`黄金` 是原样标签的子串 ⇒
    词形去重那一路照常命中库里那一行。

    少了这一格，上一条用例可以被"归一那一腿整条不要了"满足 —— 那会把
    `test_sector_remap.py` 里既有的一条（`RMAP白酒` 走前缀剥离）一起打死。
    """
    prediction = _seed(db_session, sector='黄金行情', mapping_sector='黄金',
                       mapping_fund='RMAPGOLD2', pred_fund='RMAPFIN2', confidence=0.95)
    _navs(db_session, 'RMAPGOLD2', (date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 8)))

    result = _service(db_session).sync_sector_mappings(
        dry_run=False, min_confidence=0.85, run_id='t-affix-still-works')

    assert result['predictions_updated'] == 1
    db_session.refresh(prediction)
    assert prediction.fund_code == 'RMAPGOLD2'
