"""
预测路由
处理预测相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from sqlalchemy.orm import Session
from typing import Annotated, Optional, List
from datetime import date, datetime, timedelta
import logging

from src.api.deps import get_db
from src.models.database import Prediction
from src.services.prediction_service import PredictionService
from src.services.prediction_verify_service import PredictionVerifyService
from src.services.prediction_verify_task import prediction_verify_task
from src.api.schemas.prediction import PredictionUpdate
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from src.services.prediction_query_service import PredictionQueryService

router = APIRouter(prefix="/predictions", tags=["预测"])

# 定向回溯一次的上限：预览口径是全库扫描，不设界的话"确认执行"就等于
# 第 10 轮 M-5 明令禁止的 allow_full_sweep（第 12 轮 MAJOR-4）
MAX_ROLLBACK_IDS = 200
logger = logging.getLogger(__name__)




@router.get("")
def get_predictions(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Optional[str] = None,
    blogger_id: Optional[int] = None,
    fund_code: Optional[str] = None,
    sector: Optional[str] = None,
    prediction_type: Optional[str] = None,
    status: Optional[str] = None,
    result: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    archive: str = "active",
    is_expired: Optional[bool] = None,
    lifecycle: Optional[str] = None,
    sort: Optional[str] = None,
    exclude_flat: bool = False,
    skip: Annotated[Optional[int], Query(ge=0)] = None,
    limit: Annotated[Optional[int], Query(ge=1)] = None,
    db: Session = Depends(get_db)
):
    """分页查询预测列表；默认按到期优先排序，保留 skip/limit 作为兼容参数。"""
    if limit is not None:
        page_size = min(limit, 200)
    if skip is not None:
        page = skip // page_size + 1

    result_page = PredictionQueryService(db).search(
        page=page,
        page_size=page_size,
        search=search,
        blogger_id=blogger_id,
        fund_code=fund_code,
        sector=sector,
        prediction_type=prediction_type,
        status=status,
        result=result,
        start_date=start_date,
        end_date=end_date,
        archive=archive,
        is_expired=is_expired,
        lifecycle=lifecycle,
        sort=sort,
        exclude_flat=exclude_flat,
    )
    
    return {
        "success": True,
        "data": result_page["data"],
        "meta": result_page["meta"],
    }


@router.get("/{prediction_id}")
def get_prediction_detail(prediction_id: int, db: Session = Depends(get_db)):
    """获取预测详情"""
    prediction = PredictionQueryService(db).get_detail(prediction_id)
    
    if not prediction:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {
        "success": True,
        "data": prediction
    }


@router.put("/{prediction_id}")
def update_prediction(prediction_id: int, req: PredictionUpdate, db: Session = Depends(get_db)):
    """编辑预测（前端编辑表单）：验证依据已生效的预测不可原地覆盖。"""
    service = PredictionService(db)
    try:
        prediction = service.update_prediction_fields(
            prediction_id,
            req.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if prediction is None:
        raise HTTPException(status_code=404, detail="预测不存在或已删除")
    return {"success": True, "message": "预测已更新", "data": {"id": prediction.id}}


@router.delete("/{prediction_id}")
def delete_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """删除预测"""
    service = PredictionService(db)
    success = service.delete_prediction(prediction_id)

    if not success:
        raise HTTPException(status_code=404, detail="预测不存在")

    return {"success": True, "message": "预测已归档，可在回收站恢复"}


@router.post("/{prediction_id}/restore")
def restore_prediction(prediction_id: int, db: Session = Depends(get_db)):
    """恢复归档预测。"""
    if not PredictionService(db).restore_prediction(prediction_id):
        raise HTTPException(status_code=404, detail="未找到可恢复的预测")
    return {"success": True, "message": "预测已恢复"}


@router.post("/rollback-invalid")
def rollback_invalid_verifications(
    request: Request,
    dry_run: bool = Query(True),
    ids: str = Query(None, description='逗号分隔的 prediction id；真写必须给'),
    db: Session = Depends(get_db),
):
    """预览或回溯数据不足的已验证预测。

    真写**必须**带 `ids`（定向回溯）。不限定 id 的全库回溯会抹掉上千条历史结论，
    其中多数只是本地镜像缺那段历史（第 10 轮 M-5、第 11 轮 M-F），
    因此那个口径只能由脚本显式 `allow_full_sweep=True` 发起，不给 HTTP 入口。
    以前这里没有定向参数 ⇒ 按钮"确认执行"其实一行都撤不了，
    却返回 200 让前端表现得像成功了 —— 所以缺 ids 现在直接 400。
    """
    only_ids = None
    if ids:
        try:
            only_ids = tuple(int(x) for x in ids.split(',') if x.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail='ids 必须是逗号分隔的预测 id')
    if only_ids is not None and len(only_ids) > MAX_ROLLBACK_IDS:
        raise HTTPException(
            status_code=400,
            detail=f'一次最多定向回溯 {MAX_ROLLBACK_IDS} 条（当前 {len(only_ids)} 条）；'
                   f'更大范围请走脚本并显式 allow_full_sweep',
        )
    if not dry_run and not only_ids:
        raise HTTPException(
            status_code=400,
            detail='执行回溯必须指定预测 id（ids=…）；整库回溯请走脚本并显式 allow_full_sweep',
        )
    if not dry_run and request.headers.get("X-Danger-Confirm") != "rollback-predictions":
        raise HTTPException(
            status_code=403,
            detail="执行回溯需要确认头 X-Danger-Confirm: rollback-predictions",
        )
    service = PredictionVerifyService(db)
    result = service.rollback_invalid_verifications(
        min_data_points=2, dry_run=dry_run, only_ids=only_ids)

    return result


@router.post("/sync-sector-mapping")
def sync_sector_mapping(
    request: Request,
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
):
    """使用已审核映射预览或同步预测基金关联。"""
    if not dry_run and request.headers.get("X-Danger-Confirm") != "sync-prediction-mapping":
        raise HTTPException(
            status_code=403,
            detail="执行同步需要确认头 X-Danger-Confirm: sync-prediction-mapping",
        )
    try:
        run_id = None
        if not dry_run:
            # 没 run_id 的写入等于**不可回滚**：`scripts/restore_prediction_batch.py`
            # 按 run_id 过滤，UI 这一路以前不传，整批改标只能靠备份文件救
            from datetime import datetime as _dt
            run_id = 'ui-sync-%s' % _dt.now().strftime('%Y%m%d-%H%M%S')
        result = PredictionMaintenanceService(db).sync_sector_mappings(
            dry_run=dry_run, run_id=run_id)
        result['run_id'] = run_id

        # 构建详细消息
        parts = []
        if result['predictions_updated'] > 0:
            parts.append(f"更新 {result['predictions_updated']} 个预测")
        if result['verified_reset'] > 0:
            parts.append(f"重置 {result['verified_reset']} 个已验证预测")
        if result['funds_added'] > 0:
            parts.append(f"新增 {result['funds_added']} 个基金")
        if result['funds_sector_updated'] > 0:
            parts.append(f"更新 {result['funds_sector_updated']} 个基金板块")
        # "有几行没动"必须自己说出口：不然预览报 326、实跑只动 320，
        # 老板看到的是一句"同步完成"，那 6 条为什么没改、改过去会变成什么，一个字都没有。
        skipped = result.get('predictions_skipped_unservable') or 0
        if skipped:
            parts.append(f"{skipped} 条没动：板块映射挑的那只标的给不出这段窗口的净值证据"
                         f"（绑过去会变成到期也判不了的预测）")

        # 板块压根没有标的这一档（2026-09-28 老板授权："可以把该板块对应的基金变成其他
        # 好的基金"）：预览要说"给哪几块补哪只"，实跑要说"补了几块、真改了几条"。
        # 补不了的也要点名说为什么补不了 —— 只报"同步完成"就是把"这块还是没标的"藏起来。
        # ⚠ 两句关于"话从哪来"的规矩（第 66 轮复评 MA-3/MA-4 各抓到一处）：
        # ① **这一路页面上只印 `message` 这一句**（预览面板只有一行"预计更新 N 条"，
        #    真跑那一路是 `alert(response.data.message)）⇒ 不许写"见明细"，那是不存在的栏
        #    （第 53 轮 A-1 那句"哪两种原因见上方…"同族）；原因要么自己说出口，要么别说。
        # ② 完成时只配配真做完的数：`predictions_via_gap_fill` 是**改完之后**回查行上代码
        #    数出来的，计划那份另有一个 `_planned` 键 —— 拿计划数说"已经改了"是第 51 轮 B-2。
        to_fill = result.get('sectors_to_fill') or []
        fillable = result.get('sectors_fillable') or []
        filled = result.get('sectors_filled') or 0
        via_planned = result.get('predictions_via_gap_fill_planned') or 0
        via_done = result.get('predictions_via_gap_fill') or 0
        sample = "、".join(f"{item['sector']}→{item['fund_code']} {item['fund_name']}"
                           for item in to_fill[:3])
        if dry_run and to_fill:
            parts.append(f"{len(to_fill)} 个板块本来没有任何可用标的，而它们身上 {via_planned} 条预测"
                         f"用自己那只标的问不出这段窗口的净值 ⇒ 会按内置板块表给这些板块补上"
                         f"对口标的（{sample}{'……' if len(to_fill) > 3 else ''}）")
        if not dry_run and filled:
            parts.append(f"已给 {filled} 个原本没有可用标的的板块补上标的，"
                         f"并把 {via_done} 条问不出证据的预测改到它身上")
        if dry_run and fillable and not to_fill:
            parts.append(f"{len(fillable)} 个板块内置表说得出对口标的，但它们身上的预测"
                         f"现在那只标的就给得出净值 ⇒ 这一轮一块都不动")
        refused = result.get('sectors_refused_to_fill') or []
        if refused:
            # "内置表也说不出对口品种"原来是一句**写死的**诊断，而服务侧实测有六种拒收：
            # 板块名长过列宽 / 内置表答不出 / 内置表给的正是库里那只 / 本库没这只的档案 /
            # 这只在库里一行净值都没有 / 那一行是老板署名挑定的。六种里只有第二种对得上
            # 那句写死的话 ⇒ 按种类各数各的，话从数里长出来（不认识的种类照实说"原因各异"）。
            labels = {
                'label_unusable': '板块名空着或长过列宽',
                'no_static_hit': '内置表也说不出对口品种',
                'same_as_current': '内置表给的就是库里那只标的',
                'no_archive': '本库还没有那只标的的档案',
                'no_nav': '那只标的在库里一行净值都没有',
                'owner_locked': '那行标的是老板署名挑定的',
            }
            counts: dict = {}
            for item in refused:
                key = item.get('kind') or 'other'
                counts[key] = counts.get(key, 0) + 1
            detail = "、".join(f"{labels.get(kind, '其它原因')} {n} 块"
                               for kind, n in sorted(counts.items(),
                                                     key=lambda kv: -kv[1]))
            parts.append(f"{len(refused)} 个板块仍然没有可用标的：{detail}"
                         + ("（要换署名那只请你去板块匹配页改，机器不动它）"
                            if counts.get('owner_locked') else ""))
        kept = result.get('predictions_kept_own_target') or 0
        if kept:
            parts.append(f"{kept} 条预测没动：它们自己那只标的就给得出这段窗口的净值，"
                         f"没必要换成板块标的（换了反而白清一次已有结论）")

        if parts:
            message = f"{'预览' if dry_run else '同步'}完成：" + "，".join(parts)
        else:
            pending = result.get("would_update", 0)
            if dry_run and pending:
                message = f"预览完成：将更新 {pending} 个预测"
            else:
                message = f"{'预览' if dry_run else '同步'}完成：无需更新（{result['predictions_unchanged']} 个预测未变，{result['predictions_no_mapping']} 个无映射）"

        return {
            "success": True,
            "message": message,
            "data": result
        }
    except Exception as e:
        logger.error(f"同步板块映射失败: {e}")
        return {
            "success": False,
            "message": f"同步失败: {str(e)}",
            "data": None
        }


def _count_due_predictions(db: Session, today: date) -> int:
    """页面上那个按钮的分母：**与批次跑的队列同一把尺子**。

    旧写法自己抄了一套条件（`status == 'pending'` + 目标日到期），而批次实际走
    `filter_due_for_verify`（`is_correct is null` + 重问锁 + 排除 flat）⇒ 两个数天生
    会对不上，进度条的 `X / total` 与"还有几条没跑到"都跟着说谎。`status` 还是历史
    遗留列（生产实测 success 591 / pending 559 / failed 466，与"已判/未判"不是一回事）。
    """
    from src.services.prediction_lifecycle import filter_due_for_verify

    return len(filter_due_for_verify(db, as_of=today))


def _due_skipped_predictions(db: Session, today: date) -> list:
    """已到期但不会进入验证队列的预测及原因（观望）。"""
    from src.services.prediction_lifecycle import due_skip_reason

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,
        Prediction.target_date.isnot(None),
        Prediction.is_correct.is_(None),
        Prediction.target_date <= today,
    ).order_by(Prediction.target_date.asc()).all()

    skipped = []
    for prediction in rows:
        reason = due_skip_reason(prediction, as_of=today)
        if reason:
            skipped.append({"prediction_id": prediction.id, "reason": reason})
    return skipped


def _verify_all_background(task_id: int):
    """后台验证所有待验证预测（逐条上报进度，供前端进度条轮询）"""
    from src.models.database import SessionLocal
    db = SessionLocal()
    result = None
    try:
        service = PredictionVerifyService(db)

        def on_progress(processed, success_count, failed_count, *_args):
            prediction_verify_task.update_progress(
                processed,
                success_count,
                failed_count,
                db=db,
                task_id=task_id,
            )

        result = service.verify_all_pending(progress_callback=on_progress)
        print(f"[Verify All] 后台验证完成: {result.get('message')}")
    except Exception as e:
        result = {"success": False, "message": f"后台验证失败: {e}"}
        print(f"[Verify All] {result['message']}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            prediction_verify_task.finish(result, db=db, task_id=task_id)
        finally:
            db.close()


@router.post("/verify-all")
def verify_all_predictions(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """验证所有待验证的预测（异步模式，跳过通道未开放的）"""
    status = prediction_verify_task.status(db=db)
    if status["in_progress"]:
        return {"success": True, "message": "验证正在进行中，请稍候...", "data": status}

    # 与批次同一个"今天"：`verify_all_pending` 用北京时区的 `current_as_of()`，
    # 这里用 `date.today()` 会在 UTC 与北京交界的那几小时数出另一个队列。
    from src.services.prediction_lifecycle import current_as_of

    today = current_as_of()
    pending_count = _count_due_predictions(db, today)

    if pending_count == 0:
        skipped = _due_skipped_predictions(db, today)
        if skipped:
            preview = "；".join(
                f"预测#{item['prediction_id']}：{item['reason']}" for item in skipped[:3]
            )
            more = f" 等 {len(skipped)} 条" if len(skipped) > 3 else ""
            return {
                "success": True,
                "message": f"没有可验证的到期预测。{len(skipped)} 条到期未验证：{preview}{more}",
                "data": {"total": 0, "skipped": skipped},
            }
        return {"success": True, "message": "没有需要验证的预测", "data": {"total": 0}}
    
    start_result = prediction_verify_task.start(pending_count, db=db)
    if not start_result["success"]:
        return start_result

    task_id = start_result["data"]["task_id"]
    background_tasks.add_task(_verify_all_background, task_id)
    
    return {"success": True, "message": f"已开始后台验证 {pending_count} 个预测，请稍后等待完成", "data": start_result["data"]}


@router.get("/verify-all/status")
def get_verify_all_status(db: Session = Depends(get_db)):
    """获取批量预测验证后台任务状态"""
    return {
        "success": True,
        "data": prediction_verify_task.status(db=db)
    }




@router.post("/merge-similar")
def merge_similar_predictions(db: Session = Depends(get_db)):
    """兼容旧入口：只扫描重复候选，不再删除原始预测。"""
    result = PredictionMaintenanceService(db).scan_duplicate_groups()
    return {
        "success": True,
        "message": f"重复检查完成：发现 {result['duplicate_groups']} 组候选，未修改任何预测",
        "data": result,
    }


@router.post("/dedupe-duplicates")
def dedupe_duplicate_predictions(request: Request, db: Session = Depends(get_db)):
    """收敛重复预测：每组保留一条，其余软删除（回收站可恢复）。"""
    if request.headers.get("X-Danger-Confirm") != "dedupe-predictions":
        raise HTTPException(
            status_code=403,
            detail="执行去重需要确认头 X-Danger-Confirm: dedupe-predictions",
        )
    result = PredictionMaintenanceService(db).deduplicate_predictions()
    return {
        "success": True,
        "message": (
            f"去重完成：{result['duplicate_groups']} 组重复，"
            f"已归档 {result['removed_count']} 条，每组保留一条（可在回收站恢复）"
        ),
        "data": result,
    }


