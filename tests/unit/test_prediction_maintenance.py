from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.routes import predictions as prediction_routes
from src.models.database import (
    Blogger,
    FundInfo,
    Post,
    Prediction,
    PredictionChangeLog,
    SectorFundMapping,
)
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from src.services.prediction_verify_service import PredictionVerifyService


def _prediction(db, blogger, post, *, fund_code="DUP01", sector="白酒", verified=False):
    value = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund_code,
        fund_name="重复测试基金" if fund_code else None,
        sector=sector,
        prediction_type="up",
        prediction_content="未来一周上涨",
        confidence=80,
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=post.post_date + timedelta(days=7),
        status="success" if verified else "pending",
        is_expired=verified,
        is_correct=True if verified else None,
        verify_count=1 if verified else 0,
        verify_score=80 if verified else 0,
        verify_history=[{"date": "2026-07-08", "score": 80}] if verified else [],
        is_deleted=False,
    )
    db.add(value)
    db.flush()
    return value


def _blogger_post(db, name):
    blogger = Blogger(name=name, platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title=f"{name}的帖子",
        content="用于测试预测维护操作。",
        post_date=date(2026, 7, 1),
        analyzed=True,
    )
    db.add(post)
    db.flush()
    return blogger, post


def test_duplicate_scan_never_groups_cross_blogger_or_missing_fund(test_db):
    first_blogger, first_post = _blogger_post(test_db, "博主甲")
    second_blogger, second_post = _blogger_post(test_db, "博主乙")
    first = _prediction(test_db, first_blogger, first_post)
    second = _prediction(test_db, first_blogger, first_post)
    cross_blogger = _prediction(test_db, second_blogger, second_post)
    _prediction(test_db, first_blogger, first_post, fund_code=None)
    _prediction(test_db, first_blogger, first_post, fund_code=None)
    test_db.commit()

    result = PredictionMaintenanceService(test_db).scan_duplicate_groups()

    assert result["duplicate_groups"] == 1
    assert result["candidate_predictions"] == 2
    assert result["removable_predictions"] == 1
    assert result["groups"][0]["prediction_ids"] == [first.id, second.id]
    assert result["groups"][0]["keep_id"] == first.id
    assert result["groups"][0]["remove_ids"] == [second.id]
    assert cross_blogger.id not in result["groups"][0]["prediction_ids"]
    assert test_db.query(Prediction).filter(Prediction.is_deleted == True).count() == 0


def test_dedupe_keeps_verified_prediction_and_soft_deletes_rest(test_db):
    blogger, post = _blogger_post(test_db, "去重博主")
    plain = _prediction(test_db, blogger, post)
    verified = _prediction(test_db, blogger, post, verified=True)
    test_db.commit()

    scan = PredictionMaintenanceService(test_db).scan_duplicate_groups()
    assert scan["groups"][0]["keep_id"] == verified.id

    result = PredictionMaintenanceService(test_db).deduplicate_predictions()

    assert result["removed_count"] == 1
    assert result["removed"] == [{"prediction_id": plain.id, "keep_id": verified.id}]
    test_db.refresh(plain)
    test_db.refresh(verified)
    assert plain.is_deleted is True
    assert plain.delete_reason == f"duplicate_of_{verified.id}"
    assert verified.is_deleted is False
    assert verified.is_correct is True
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "archived"
    assert log.source == "duplicate_cleanup"

    # 幂等：再跑一次不再归档任何记录
    again = PredictionMaintenanceService(test_db).deduplicate_predictions()
    assert again["removed_count"] == 0
    assert test_db.query(PredictionChangeLog).count() == 1


def test_dedupe_route_requires_confirmation(test_db):
    with pytest.raises(HTTPException) as exc:
        prediction_routes.dedupe_duplicate_predictions(
            request=_request(),
            db=test_db,
        )

    assert exc.value.status_code == 403


def test_sector_mapping_preview_uses_only_reviewed_mapping_and_does_not_write(test_db):
    old_fund = FundInfo(fund_code="OLD01", fund_name="旧基金")
    reviewed_fund = FundInfo(fund_code="NEW01", fund_name="已审核基金")
    unreviewed_fund = FundInfo(fund_code="NEW02", fund_name="未审核基金")
    test_db.add_all([old_fund, reviewed_fund, unreviewed_fund])
    blogger, post = _blogger_post(test_db, "映射博主")
    reviewed_prediction = _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    ignored_prediction = _prediction(test_db, blogger, post, fund_code="OLD01", sector="医药")
    test_db.add_all([
        SectorFundMapping(
            sector_name="白酒", fund_code="NEW01", fund_name="已审核基金",
            reviewed=True, is_active=True,
        ),
        SectorFundMapping(
            sector_name="医药", fund_code="NEW02", fund_name="未审核基金",
            reviewed=False, is_active=True,
        ),
    ])
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)

    assert result["dry_run"] is True
    assert result["predictions_updated"] == 0
    assert result["would_update"] == 1
    assert result["details"][0]["prediction_id"] == reviewed_prediction.id
    test_db.refresh(reviewed_prediction)
    test_db.refresh(ignored_prediction)
    assert reviewed_prediction.fund_code == "OLD01"
    assert ignored_prediction.fund_code == "OLD01"
    assert test_db.query(PredictionChangeLog).count() == 0


def test_sector_mapping_execution_records_prediction_change(test_db):
    test_db.add_all([
        FundInfo(fund_code="OLD01", fund_name="旧基金"),
        FundInfo(fund_code="NEW01", fund_name="已审核基金"),
    ])
    blogger, post = _blogger_post(test_db, "正式映射博主")
    prediction = _prediction(
        test_db,
        blogger,
        post,
        fund_code="OLD01",
        sector="白酒",
        verified=True,
    )
    test_db.add(SectorFundMapping(
        sector_name="白酒",
        fund_code="NEW01",
        fund_name="已审核基金",
        reviewed=True,
        is_active=True,
    ))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=False)

    assert result["predictions_updated"] == 1
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "maintenance_sync"
    assert log.source == "sector_mapping"
    assert log.before_state["fund_code"] == "OLD01"
    assert log.after_state["fund_code"] == "NEW01"
    assert log.before_state["status"] == "success"
    assert log.after_state["status"] == "pending"


def test_rollback_invalid_dry_run_preserves_verification(monkeypatch, test_db):
    blogger, post = _blogger_post(test_db, "回溯博主")
    prediction = _prediction(test_db, blogger, post, verified=True)
    test_db.commit()
    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(service, "match_fund_for_prediction", lambda value: ("DUP01", "重复测试基金"))
    monkeypatch.setattr(service, "_check_fund_data_availability", lambda **kwargs: {
        "available": False,
        "message": "净值数据不足",
        "data_points": 1,
    })

    result = service.rollback_invalid_verifications(min_data_points=2, dry_run=True)

    assert result["data"]["would_rollback"] == 1
    assert result["data"]["rolled_back"] == 0
    test_db.refresh(prediction)
    assert prediction.status == "success"
    assert prediction.verify_count == 1
    assert prediction.verify_history == [{"date": "2026-07-08", "score": 80}]


def test_rollback_invalid_execution_records_prediction_change(monkeypatch, test_db):
    blogger, post = _blogger_post(test_db, "正式回溯博主")
    prediction = _prediction(test_db, blogger, post, verified=True)
    test_db.commit()
    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(
        service,
        "match_fund_for_prediction",
        lambda value: ("DUP01", "重复测试基金"),
    )
    monkeypatch.setattr(service, "_check_fund_data_availability", lambda **kwargs: {
        "available": False,
        "message": "净值数据不足",
        "data_points": 1,
    })

    # 第 10 轮 M-5：不限定 id 的**真写**必须显式声明，否则一行都不许动
    # （这个口径下会抹掉上千条历史结论，多数只是本地镜像缺那段历史）。
    refused = service.rollback_invalid_verifications(min_data_points=2, dry_run=False)
    assert refused["success"] is False and refused["data"]["rolled_back"] == 0
    assert "allow_full_sweep" in refused["message"]
    assert test_db.query(PredictionChangeLog).count() == 0, "被拒的调用不该留下任何改动"

    preview = service.rollback_invalid_verifications(min_data_points=2, dry_run=True)
    assert preview["data"]["would_rollback"] == 1, "dry-run 不受 allow_full_sweep 约束"

    result = service.rollback_invalid_verifications(
        min_data_points=2, dry_run=False, allow_full_sweep=True)

    assert result["data"]["rolled_back"] == 1
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "verification_rollback"
    assert log.source == "maintenance"
    assert log.before_state["status"] == "success"
    assert log.after_state["status"] == "pending"
    # 真写必须自带 run_id，否则 `restore_prediction_batch.py --run-id` 撤不回来
    # （第 12 轮 MAJOR-4；姊妹入口 sync-sector-mapping 早就有了）
    assert (log.run_id or '').startswith('rollback-'), log.run_id
    assert result["data"]["run_id"] == log.run_id
    # 第 13 轮 MAJOR-1：不往 verify_history 塞墓碑 —— 前端历史表按
    # `is_correct ? '正确' : '错误'` 与 `score || 0` 渲染，墓碑会凭空多出一条
    # "验证失败 / 0 分 / 错误"的假历史。回溯的审计只留在 change log 里。
    row = test_db.query(Prediction).filter(Prediction.id == prediction.id).first()
    assert all(not h.get("rolled_back") for h in (row.verify_history or [])), row.verify_history


def test_rollback_with_only_ids_reports_filtered_rows_separately(monkeypatch, test_db):
    """定向模式下"保留 N 个"只能数**真的评估过**的行（第 10 轮 MINOR-7）。"""
    blogger, post = _blogger_post(test_db, "定向回溯博主")
    keep = _prediction(test_db, blogger, post, verified=True)
    other = _prediction(test_db, blogger, post, verified=True, fund_code="OTHER9")
    test_db.commit()
    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(service, "match_fund_for_prediction",
                        lambda value: ("DUP01", "重复测试基金"))
    monkeypatch.setattr(service, "_check_fund_data_availability", lambda **kwargs: {
        "available": False, "message": "净值数据不足", "data_points": 1})

    dry = service.rollback_invalid_verifications(dry_run=True, only_ids=[keep.id])
    assert dry["data"]["total_checked"] == 1
    assert dry["data"]["skipped_by_filter"] == 1
    assert dry["data"]["would_rollback"] == 1


def test_sync_retags_with_an_agent_approved_proxy_mapping(test_db):
    """M2：agent 自己按 proxy 门槛（0.68）盖过章的映射必须能修正预测。

    旧写法在这里写死 0.85，于是 0.68~0.8499 区间"审查通过却不够格改结论"。
    实测本地镜像库 12 条 agent 自批映射全被卡在死区，其中 京A 那条的 pending
    预测（线上 id 1238）至今挂着 000725——本模块要修的那只"京东方Ａ"股票。
    """
    test_db.add_all([
        FundInfo(fund_code="OLD01", fund_name="旧基金"),
        FundInfo(fund_code="NEW01", fund_name="代理ETF"),
    ])
    blogger, post = _blogger_post(test_db, "死区博主")
    prediction = _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    test_db.add(SectorFundMapping(
        sector_name="白酒", fund_code="NEW01", fund_name="代理ETF",
        reviewed=True, reviewed_by="agent", match_kind="proxy",
        confidence=0.72, is_active=True,
    ))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=False)

    assert result["mappings_skipped_low_confidence"] == 0
    assert result["predictions_updated"] == 1
    test_db.refresh(prediction)
    assert prediction.fund_code == "NEW01"


def test_mapping_eligible_uses_the_agent_gate_not_a_second_number():
    """M2 的门禁本身：一套真值 = agent 的标定阈值，不再是第二个写死的 0.85。"""
    eligible = PredictionMaintenanceService._mapping_eligible

    def _row(**kw):
        kwargs = dict(sector_name="白酒", fund_code="NEW01", fund_name="某ETF",
                      is_active=True)
        kwargs.update(kw)
        return SectorFundMapping(**kwargs)

    # agent 盖过章 = 它已按 match_kind 的门槛（direct 0.80 / proxy 0.68）判过
    assert eligible(_row(reviewed=True, reviewed_by="agent",
                         match_kind="proxy", confidence=0.75), 0.85) is True
    assert eligible(_row(reviewed=True, reviewed_by="agent",
                         match_kind="direct", confidence=0.82), 0.85) is True
    # 没盖章的同样两行：仍然只认调用方传的 min_confidence
    assert eligible(_row(reviewed=False, reviewed_by=None,
                         match_kind="proxy", confidence=0.75), 0.85) is False
    assert eligible(_row(reviewed=False, reviewed_by=None,
                         match_kind="direct", confidence=0.82), 0.85) is False
    # 带着章但置信度低于 agent 门槛（阈值后来被调高过、或人工勾的"已审查"）
    # → 章不作数，回到 min_confidence 兜底
    assert eligible(_row(reviewed=True, reviewed_by="agent",
                         match_kind="direct", confidence=0.72), 0.85) is False
    assert eligible(_row(reviewed=True, reviewed_by="agent",
                         match_kind="direct", confidence=0.90), 0.85) is True
    # 老板的行永远作数；体检判不可服务的行永远不作数
    assert eligible(_row(reviewed=True, reviewed_by="owner", owner_locked=True,
                         confidence=0.10), 0.85) is True
    assert eligible(_row(reviewed=True, reviewed_by="agent", match_kind="direct",
                         confidence=0.95, is_fetchable=False), 0.85) is False
    # 历史人工行：没有置信度但有审查 = 人的结论
    assert eligible(_row(reviewed=True, reviewed_by=None, confidence=None), 0.85) is True


def _request(headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw_headers})


def test_mapping_execute_route_requires_confirmation(test_db):
    with pytest.raises(HTTPException) as exc:
        prediction_routes.sync_sector_mapping(
            request=_request(),
            dry_run=False,
            db=test_db,
        )

    assert exc.value.status_code == 403


def _counted_statements(db):
    """数一段代码里**真的发出几条 SQL** —— 生产慢下来的唯一可见形状就是它。"""
    from contextlib import contextmanager
    from sqlalchemy import event

    engine = db.get_bind()
    counter = {'n': 0}

    def _hit(conn, cursor, statement, parameters, context, executemany):
        counter['n'] += 1

    @contextmanager
    def scope():
        event.listen(engine, 'before_cursor_execute', _hit)
        try:
            yield counter
        finally:
            event.remove(engine, 'before_cursor_execute', _hit)
    return scope()


def test_the_mapping_preview_does_not_query_the_database_per_prediction(test_db):
    """「按板块对齐标的」的预览**不许**随预测条数线性查库。

    生产实测（2026-09-26）：同一趟代码在本地镜像 3.5 秒，在线上 **100 秒零字节**
    （`curl` 连上、请求发完、拿不到响应）⇒ 老板点这个按钮就是干等。
    根因是循环里每条没直接命中的预测各查一次 `sector_alias`（900+ 次远程往返）。
    判据形状：把预测从 5 条加到 405 条，**语句条数必须一模一样**。
    """
    blogger, post = _blogger_post(test_db, 'N+1 测试博主')
    test_db.add(SectorFundMapping(sector_name='白酒', fund_code='NEW01',
                                  fund_name='新标的', is_active=True, reviewed=True,
                                  reviewed_by='owner', owner_locked=True))
    test_db.commit()

    def seed(count):
        for i in range(count):
            _prediction(test_db, blogger, post, fund_code='X%04d' % i,
                        sector='没有映射的板块')
        test_db.commit()

    seed(5)
    svc = PredictionMaintenanceService(test_db)
    with _counted_statements(test_db) as small:
        first = svc.sync_sector_mappings(dry_run=True)
    seed(400)
    with _counted_statements(test_db) as big:
        second = svc.sync_sector_mappings(dry_run=True)

    assert small['n'] == big['n'], (
        '预览从 %d 条语句涨到 %d 条 ⇒ 循环里在按行查库（线上就是 100 秒不返回那一族）'
        % (small['n'], big['n']))
    assert first['would_update'] == second['would_update'] == 0
    assert second['predictions_no_mapping'] >= 400, '没走到"别名查不到"那一支 ⇒ 这条判据是空转'


def _mapping(sector, code, name):
    return SectorFundMapping(sector_name=sector, fund_code=code, fund_name=name,
                             reviewed=True, is_active=True)


def _nav(db, code, *days):
    from src.models.database import FundHistory
    for i, d in enumerate(days):
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0 + i / 100))


def test_the_evidence_gate_refuses_a_target_whose_history_starts_after_the_window(test_db):
    """预览与实跑必须问同一句话，并且给出**同一个数**（任务 #100 的第二半 / #105）。

    2026-09-26 生产实测：页面上那句「将更新 326 个预测」里有 6 条真跑时根本不会动 ——
    证据门当时只装在 `retag_prediction` 里面，而 dry-run 那支不调它。
    更糟的是那 6 条本来就该拒：`158038` 库里首笔净值 2026-09-07，而压在它身上的预测
    窗口是 08-28~09-14 ⇒ 绑过去就是一段问不出净值的窗口，验证器报
    `insufficient_points`（不是 `no_source_history`），任务 #8 那道重问锁接不住，
    于是它永远躺在「待验证到期」里 —— 正是老板点名要清零的那一档。
    """
    from datetime import date as _d

    test_db.add_all([
        FundInfo(fund_code="OLD01", fund_name="旧基金"),
        FundInfo(fund_code="LATE01", fund_name="净值来晚了的产品"),
        FundInfo(fund_code="GOOD01", fund_name="覆盖得住的产品"),
    ])
    blogger, post = _blogger_post(test_db, "证据门博主")     # 窗口 2026-07-01 ~ 07-08
    doomed = _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    fine = _prediction(test_db, blogger, post, fund_code="OLD01", sector="医药")
    # 首笔 09-07：晚于这段窗口一整天都没有 ⇒ 老门（只看末笔）会点头放过去
    _nav(test_db, "LATE01", _d(2026, 9, 7), _d(2026, 9, 8), _d(2026, 9, 24))
    _nav(test_db, "GOOD01", _d(2026, 7, 1), _d(2026, 7, 4), _d(2026, 7, 8))
    test_db.add_all([_mapping("白酒", "LATE01", "净值来晚了的产品"),
                     _mapping("医药", "GOOD01", "覆盖得住的产品")])
    test_db.commit()

    service = PredictionMaintenanceService(test_db)
    preview = service.sync_sector_mappings(dry_run=True)

    assert preview["would_update"] == 1, '预览把不给证据的那条也算进"将更新" ⇒ 与实跑不是一个数'
    assert preview["details"][0]["prediction_id"] == fine.id
    assert preview["predictions_skipped_unservable"] == 1
    skip = preview["skipped_unservable_details"][0]
    assert skip["prediction_id"] == doomed.id and skip["new_fund_code"] == "LATE01"
    assert "只发过 0 笔" in skip["reason"], '拒了却不写为什么 ⇒ 老板只会以为按钮坏了'

    applied = service.sync_sector_mappings(dry_run=False, run_id="unit-evidence-gate")
    assert applied["predictions_updated"] == preview["would_update"], \
        '预览 %d / 实跑 %d：回执说的和做的事不是同一件事' % (
            preview["would_update"], applied["predictions_updated"])
    test_db.refresh(doomed)
    test_db.refresh(fine)
    assert doomed.fund_code == "OLD01", '被拒的行照样写了库 ⇒ 那句拒绝是摆设'
    assert fine.fund_code == "GOOD01"
    logs = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == doomed.id).count()
    assert logs == 0


def test_the_receipt_counts_only_rows_that_actually_moved(test_db, monkeypatch):
    """「什么都没做」不许报成「更新了 N 个预测」（第 51 轮 B-2 的同一族，第二次长出来）。

    `retag_prediction` 那个布尔说的是"清没清结论"，"已经是这个标的"和"被门拒了"
    回的都是 False —— 拿它当"改标成功"计数，回执就会替一场空操作背书。
    这里直接把入口换成一个"什么都不做"的桩，看回执怎么报。
    """
    from src.fund import fund_sync_manager

    test_db.add_all([FundInfo(fund_code="OLD01", fund_name="旧基金"),
                     FundInfo(fund_code="NEW01", fund_name="已审核基金")])
    blogger, post = _blogger_post(test_db, "回执博主")
    target = _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    test_db.add(_mapping("白酒", "NEW01", "已审核基金"))
    test_db.commit()

    monkeypatch.setattr(fund_sync_manager.FundSyncManager, 'retag_prediction',
                        staticmethod(lambda *a, **k: False))
    result = PredictionMaintenanceService(test_db).sync_sector_mappings(
        dry_run=False, run_id="unit-stub")

    assert result["predictions_updated"] == 0, '一行都没动却报"更新 1 个预测"'
    assert result["verified_reset"] == 0
    assert result["predictions_skipped_unservable"] == 1, '"没动"这件事必须有个数'
    test_db.refresh(target)
    assert target.fund_code == "OLD01"


def test_the_route_says_out_loud_how_many_rows_it_left_alone(test_db):
    """回执那句话要把"有几条没动"说到屏幕上，不能只留在 JSON 里。

    页面上只有 `message` 这一处会被读；`predictions_skipped_unservable` 没人印出来
    等于没说（第 34 轮那族"算了必须说出口"）。
    """
    test_db.add_all([FundInfo(fund_code="OLD01", fund_name="旧基金"),
                     FundInfo(fund_code="LATE01", fund_name="净值来晚了的产品")])
    blogger, post = _blogger_post(test_db, "那句话博主")
    _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    _nav(test_db, "LATE01", date(2026, 9, 7), date(2026, 9, 8))
    test_db.add(_mapping("白酒", "LATE01", "净值来晚了的产品"))
    test_db.commit()

    body = prediction_routes.sync_sector_mapping(
        request=_request(), dry_run=True, db=test_db)

    assert body["data"]["predictions_skipped_unservable"] == 1
    assert "1 条没动" in body["message"], '只报"预览完成"却不提那 1 条 ⇒ 老板以为全都会改'
