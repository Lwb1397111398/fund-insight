"""
Render Cron 一次性定时任务入口
"""
import argparse
import logging
import os
import sys
from datetime import datetime

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import init_db
from src.tasks.scheduler import TaskScheduler
from src.services.viewpoint_workflow_service import ViewpointWorkflowService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _normalize_task_result(result) -> dict:
    if result is None:
        return {"success": True}
    if isinstance(result, dict):
        normalized = dict(result)
        normalized.setdefault("success", True)
        return normalized
    return {"success": bool(result), "result": result}


def run_daily_tasks() -> dict:
    started_at = datetime.now()
    init_db()
    scheduler = TaskScheduler()
    tasks = {}
    failed_tasks = []

    def run_step(name: str, func, *args, **kwargs):
        try:
            result = _normalize_task_result(func(*args, **kwargs))
        except Exception as e:
            logger.exception("Scheduled subtask failed: %s", name)
            result = {"success": False, "error": str(e)}
        tasks[name] = result
        if not result.get("success", True):
            failed_tasks.append(name)
        return result

    try:
        run_step("fund_update", scheduler._run_fund_update)
        run_step("prediction_verify", scheduler._run_prediction_verify)
        # 结论证据体检（只读、不改数据）：每天报一次"端点净值在当前标的的净值表里
        # 复现不出来"的条数。**这一步是报告，不是闸门**：`run_step` 只看 success，
        # 所以它永远不会让日任务失败（第 18 轮 M-3 指出我把它写成了"构成阻塞"，
        # 那是不成立的）。要卡住合入请手动跑 `scripts/audit_verdict_evidence.py`
        # （默认阈值 0 ⇒ 有失效就退码 3）。
        def _verdict_evidence_audit():
            from datetime import datetime, timedelta

            from src.models.database import SessionLocal
            from src.services.verdict_evidence import stale_counts

            db = SessionLocal()
            try:
                counts = stale_counts(db)
                # "改标门"只在体检判过不可服务之后才会触发。日任务不跑体检 ⇒ 结论
                # 可能早就过期，而页面看起来一切正常（第 20 轮 MAJOR-2）。这里至少把
                # 结论的新鲜度报出来：多老的算过期，看 `SWEEP_STALE_DAYS`。
                from src.models.database import SectorFundMapping
                stamps = [r[0] for r in db.query(SectorFundMapping.verified_at).filter(
                    SectorFundMapping.is_active == True).all()      # noqa: E712
                    if r[0]]
                never_audited = db.query(SectorFundMapping).filter(
                    SectorFundMapping.is_active == True,             # noqa: E712
                    SectorFundMapping.verified_at.is_(None)).count()
            finally:
                db.close()
            total = sum(counts.values())
            if total:
                logger.warning("结论证据失效 %s 条：%s（前端已标 ⚠，处置见 "
                               "docs/模块总览/预测验证与准确率统计.md）", total, counts)
            age_days = None
            stale_limit = int(os.environ.get("SWEEP_STALE_DAYS", "14"))
            if stamps:
                age_days = (datetime.now() - max(stamps)).days
            # 第 21 轮 MINOR：以前 `if stamps:` 之外一律不响 —— 全表从未体检时 age=None，
            # 恰恰是最该告警的那种"从来没查过"，被静默掉了；`never_audited` 自己也不触发。
            active_total = len(stamps) + never_audited
            if active_total and (age_days is None or age_days > stale_limit
                                 or never_audited):
                logger.warning(
                    "板块映射的身份体检结论不新鲜：最新一条 verified_at 距今 %s，"
                    "另有 %d/%d 行从未体检（阈值 %d 天）。"
                    "\"改标门\"依赖这个结论，结论过期＝映射身份没被复核过；"
                    "要刷新请跑 scripts/sweep_sector_mappings.py"
                    "（那个脚本自己的净值新鲜度阈值是 NAV_STALE_DAYS，与这里无关）",
                    ('无记录' if age_days is None else '%d 天' % age_days),
                    never_audited, len(stamps) + never_audited, stale_limit)
            return {"success": True, "stale_total": total, "stale_by_kind": counts,
                    "identity_audit_age_days": age_days,
                    "mapping_rows_never_audited": never_audited}

        run_step("verdict_evidence_audit", _verdict_evidence_audit)
        # 观点每日汇总：默认关闭，生产确认 Supabase 备份后设 ENABLE_VIEWPOINT_SUMMARY=true
        if os.environ.get("ENABLE_VIEWPOINT_SUMMARY", "false").lower() == "true":
            from src.services.viewpoint_workflow_service import ViewpointWorkflowService
            run_step("viewpoint_summary", ViewpointWorkflowService.run_daily_summary_task)
        else:
            tasks["viewpoint_summary"] = {
                "success": True,
                "skipped": True,
                "reason": "ENABLE_VIEWPOINT_SUMMARY not set to true",
            }
        return {
            "success": not failed_tasks,
            "tasks": tasks,
            "failed_tasks": failed_tasks,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.exception("定时任务执行失败")
        return {
            "success": False,
            "error": str(e),
            "started_at": started_at.isoformat(),
        }


def run_weekly_retention() -> dict:
    """周清理：三桶 execute + cap/guard + 多轮直到 dry-run 归零或达上限。

    默认开启；设 ENABLE_THREE_BUCKET_RETENTION=false 可跳过。
    """
    started_at = datetime.now()
    if os.environ.get("ENABLE_THREE_BUCKET_RETENTION", "true").lower() == "false":
        return {
            "success": True,
            "skipped": True,
            "reason": "ENABLE_THREE_BUCKET_RETENTION=false",
            "started_at": started_at.isoformat(),
        }

    init_db()
    from pathlib import Path

    from src.models.database import SessionLocal
    from src.services.retention_three_buckets import (
        CONFIRM_TOKEN,
        ThreeBucketRetentionService,
    )

    max_rounds = int(os.environ.get("THREE_BUCKET_MAX_ROUNDS", "5"))
    report_dir = Path(
        os.environ.get("THREE_BUCKET_REPORT_DIR", "data/retention_reports")
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%S")
    rounds = []
    session = SessionLocal()
    try:
        service = ThreeBucketRetentionService(session)
        for i in range(1, max_rounds + 1):
            plan = service.build_plan()
            dry_path = report_dir / f"three_buckets_{stamp}_r{i}_dryrun.json"
            service.write_dry_run_report(dry_path, plan)
            counts = {k: len(v) for k, v in plan.candidate_ids.items()}
            total = plan.total
            round_info = {
                "round": i,
                "dry_run_counts": counts,
                "dry_run_total": total,
                "protected_counts": plan.protected_counts,
                "dry_run_report": str(dry_path),
            }
            if total == 0:
                round_info["execute"] = None
                round_info["message"] = "dry-run zero; stop"
                rounds.append(round_info)
                break
            result = service.execute(
                dry_run=False,
                confirm_token=CONFIRM_TOKEN,
                plan=plan,
                # 每轮删完就 VACUUM 太重，统一放到最后一次
                reclaim_space=False,
            )
            round_info["execute"] = {
                "deleted_counts": result.get("deleted_counts"),
                "cascade_counts": result.get("cascade_counts"),
                "total_deleted": result.get("total_deleted"),
                "total_rows_removed": result.get("total_rows_removed"),
                "cleanup_log_id": result.get("cleanup_log_id"),
                "protected_counts": result.get("protected_counts"),
            }
            rounds.append(round_info)
            logger.info(
                "weekly retention round %s deleted=%s cascade=%s protected=%s",
                i,
                result.get("deleted_counts"),
                result.get("cascade_counts"),
                plan.protected_counts,
            )
        else:
            # 达到 max_rounds 仍可能有残留：再记一笔 dry-run
            final_plan = service.build_plan()
            rounds.append(
                {
                    "round": max_rounds + 1,
                    "dry_run_counts": {
                        k: len(v) for k, v in final_plan.candidate_ids.items()
                    },
                    "dry_run_total": final_plan.total,
                    "message": "max_rounds reached; residual may remain",
                }
            )

        summary_path = report_dir / f"three_buckets_{stamp}_summary.json"
        total_deleted = sum(
            (r.get("execute") or {}).get("total_deleted") or 0 for r in rounds
        )
        # 所有轮次删完后统一回收空间：Postgres 不 VACUUM 文件不会变小
        space_reclaim = None
        if total_deleted:
            tables = sorted(
                {
                    table
                    for buckets in ThreeBucketRetentionService.BUCKET_TABLES.values()
                    for table in buckets
                }
            )
            space_reclaim = service.reclaim_space(tables)
            logger.info("weekly retention space reclaim: %s", space_reclaim)
        payload = {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
            "rounds": rounds,
            "total_deleted": total_deleted,
            "total_rows_removed": sum(
                (r.get("execute") or {}).get("total_rows_removed") or 0 for r in rounds
            ),
            "space_reclaim": space_reclaim,
        }
        summary_path.write_text(
            __import__("json").dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        payload["summary_report"] = str(summary_path)
        payload["success"] = True
        return payload
    except Exception as e:
        logger.exception("weekly retention failed")
        return {
            "success": False,
            "error": str(e),
            "started_at": started_at.isoformat(),
            "rounds": rounds,
        }
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Fund Insight 一次性定时任务")
    parser.add_argument(
        "job",
        choices=["daily", "weekly-retention"],
        help="任务类型：daily=基金/验证；weekly-retention=三桶清理",
    )
    args = parser.parse_args()

    # 这条跑在 Render Cron 上，默认连的就是 `.env` 里那个库（生产）。
    # 第 28 轮 F-MINOR-6 的教训：能写数据的入口必须把"写到哪个库"印在回执第一行，
    # 否则事后翻日志只能靠猜（净值停在 09-13 那 9 天就是这么糊过去的）。
    from src.models.database import SessionLocal
    from src.services.verdict_evidence import database_label
    _probe_db = SessionLocal()
    try:
        logger.info("[库] 本次定时任务连的是：%s", database_label(_probe_db))
    finally:
        _probe_db.close()

    if args.job == "daily":
        result = run_daily_tasks()
        logger.info("定时任务结果: %s", result)
        return 0 if result.get("success") else 1
    if args.job == "weekly-retention":
        result = run_weekly_retention()
        logger.info("周清理结果: %s", result)
        return 0 if result.get("success") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
