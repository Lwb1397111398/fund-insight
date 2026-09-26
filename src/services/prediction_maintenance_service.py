"""预测维护操作：默认只读预览，写入必须由路由显式确认。"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import (Blogger, FundInfo, Prediction, SectorAlias,
                             SectorFundMapping)
from src.services.prediction_change_log_service import (
    add_prediction_change_log,
    snapshot_prediction,
)
from src.utils.blogger_stats import recalculate_blogger_stats


from src.services.prediction_verify_service import has_verdict_trace
class PredictionMaintenanceService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _duplicate_keep_rank(prediction: Prediction) -> tuple:
        """同组重复候选中"保留哪一条"的排序键（值越小越优先保留）。

        优先级：已验证 > 验证次数多 > 录入时间早（id 小）。
        """
        verified = 1 if prediction.is_correct is not None else 0
        return (-verified, -(prediction.verify_count or 0), prediction.id)

    @staticmethod
    def _duplicate_bucket(prediction: Prediction) -> tuple:
        """重复判定的分组键：同一博主 + 同基金 + 同方向 + 同目标日。"""
        return (
            prediction.blogger_id,
            prediction.fund_code,
            prediction.prediction_type,
            prediction.target_date,
        )

    def scan_duplicate_groups(self) -> Dict:
        """查找同一博主的精确重复候选，不修改任何预测。"""
        predictions = self.db.query(Prediction).filter(
            Prediction.is_deleted == False,
            Prediction.fund_code.isnot(None),
            Prediction.fund_code != "",
            Prediction.target_date.isnot(None),
        ).order_by(Prediction.id.asc()).all()

        grouped: Dict[tuple, List[Prediction]] = defaultdict(list)
        for prediction in predictions:
            grouped[self._duplicate_bucket(prediction)].append(prediction)

        groups = []
        for key, values in grouped.items():
            if len(values) < 2:
                continue
            ordered = sorted(values, key=self._duplicate_keep_rank)
            groups.append({
                "blogger_id": key[0],
                "fund_code": key[1],
                "prediction_type": key[2],
                "target_date": key[3].isoformat(),
                "prediction_ids": [value.id for value in ordered],
                "keep_id": ordered[0].id,
                "remove_ids": [value.id for value in ordered[1:]],
                "count": len(values),
            })

        return {
            "dry_run": True,
            "duplicate_groups": len(groups),
            "candidate_predictions": sum(group["count"] for group in groups),
            "removable_predictions": sum(len(group["remove_ids"]) for group in groups),
            "groups": groups,
        }

    def deduplicate_predictions(self) -> Dict:
        """按扫描结果收敛重复预测：每组保留一条，其余软删除（可恢复）。"""
        scan = self.scan_duplicate_groups()
        removed = []
        affected_bloggers = set()
        affected_funds = set()
        try:
            for group in scan["groups"]:
                for prediction_id in group["remove_ids"]:
                    prediction = self.db.query(Prediction).filter(
                        Prediction.id == prediction_id,
                        Prediction.is_deleted == False,
                    ).first()
                    if not prediction:
                        continue
                    # 防止扫描后被外部修改：落库前重新校验分组键仍一致
                    current_key = self._duplicate_bucket(prediction)
                    if current_key != (
                        group["blogger_id"],
                        group["fund_code"],
                        group["prediction_type"],
                        date.fromisoformat(group["target_date"]),
                    ):
                        continue
                    before_state = snapshot_prediction(prediction)
                    affected_bloggers.add(prediction.blogger_id)
                    if prediction.fund_code:
                        affected_funds.add(prediction.fund_code)
                    prediction.is_deleted = True
                    prediction.deleted_at = datetime.now()
                    prediction.deleted_by = "maintenance"
                    prediction.delete_reason = f"duplicate_of_{group['keep_id']}"
                    prediction.restore_before = date.today() + timedelta(days=30)
                    add_prediction_change_log(
                        self.db,
                        prediction,
                        action="archived",
                        source="duplicate_cleanup",
                        before_state=before_state,
                    )
                    removed.append({
                        "prediction_id": prediction.id,
                        "keep_id": group["keep_id"],
                    })

            self.db.flush()
            for blogger_id in affected_bloggers:
                recalculate_blogger_stats(self.db, blogger_id, commit=False)
            self._refresh_fund_counts(affected_funds)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return {
            "dry_run": False,
            "duplicate_groups": scan["duplicate_groups"],
            "candidate_predictions": scan["candidate_predictions"],
            "removed_count": len(removed),
            "removed": removed,
        }

    def sync_sector_mappings(self, *, dry_run: bool = True,
                             min_confidence: float = 0.85,
                             run_id: Optional[str] = None) -> Dict:
        """使用已审核映射预览或同步预测基金关联。

        - `min_confidence`：只作为**兜底**门槛——没有 agent 审查章的行（人工勾的、
          历史遗留的）要改预测仍得到达这个置信度；带 `reviewed_by='agent'` 章的行
          按 agent 自己的标定门槛放行（见 `_mapping_eligible`，M2 修的就是这两套
          门槛打架留下的死区）。老板手工确认过的行（`reviewed_by='owner'` 或
          `owner_locked`）无条件有效——那是他说的"有意代理"。
        - `run_id`：写进 change log，`scripts/restore_prediction_batch.py` 才能整批回滚。
        - 板块匹配走别名归一（`sector_alias`），否则"绿电/绿色电力"这类同义板块会漏改。
        """
        from src.services.sector_identity_audit import servable_predicate
        if not dry_run and not run_id:
            # 真写却没带 run_id = 这批改动**永远无法整批回滚**
            # （`scripts/restore_prediction_batch.py` 就是按 run_id 过滤的；
            #  库里已经有 795 条这样的历史行，占 267 个预测）。以后一律自动生成一个。
            from datetime import datetime as _dt
            run_id = 'sync-%s' % _dt.now().strftime('%Y%m%d-%H%M%S')
        mappings = self.db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True,
            SectorFundMapping.reviewed == True,
            servable_predicate(),
        ).order_by(
            SectorFundMapping.updated_at.desc(),
            SectorFundMapping.id.desc(),
        ).all()
        sector_map = {}
        low_confidence = 0
        for mapping in mappings:
            if not self._mapping_eligible(mapping, min_confidence):
                low_confidence += 1
                continue
            sector_map.setdefault(mapping.sector_name, mapping)

        predictions = self.db.query(Prediction).filter(
            Prediction.is_deleted == False,
        ).order_by(Prediction.id.asc()).all()
        # 别名**一次读全表**再在内存里查。以前每条没直接命中的预测都单独查一次库 ——
        # 生产实测（2026-09-26）这一趟本地 3.5 秒、线上 100 秒不返回（curl 拿到 0 字节），
        # 差的就是 900+ 次远程往返。语义不变：仍是"本次跑批现读"，不吃进程内那份可能过期的缓存。
        alias_targets = {a.alias_name: a.sector_name
                         for a in self.db.query(SectorAlias).all()}
        candidates = []
        unservable = []
        unchanged = 0
        no_mapping = 0
        pairs = []
        for prediction in predictions:
            sector = prediction.sector or prediction.sector_type
            mapping = self._lookup_mapping(sector_map, sector, alias_targets)
            if not mapping:
                no_mapping += 1
                continue
            if prediction.fund_code == mapping.fund_code:
                unchanged += 1
                continue
            pairs.append((prediction, mapping, sector))

        # 证据门：**预览与实跑必须问同一句话、给出同一个数**。第 100 轮那道门当时只装在
        # `retag_prediction` 里面，而 dry-run 那支根本不调它 ⇒ 2026-09-26 生产实测
        # 预览说「将更新 326 条」、真跑只会动 320 条，那 6 条（`158038`/`012765` 那几只
        # 首笔净值晚于窗口的新产品）当场会变成"到期永不判"。日历一次读全，别在循环里查。
        from src.services.prediction_lifecycle import calendar_gap, nav_calendar

        calendar = nav_calendar(self.db, [m.fund_code for _, m, _ in pairs])
        for prediction, mapping, sector in pairs:
            # "这行还挂着结论吗"只有一个判据源（`has_verdict_trace`）：这里以前自己抄了一份，
            # 与 retag 用的 `is_correct is not None` 是同一件事的两套定义（第 18 轮 M-2）。
            row = {
                "prediction": prediction,
                "prediction_id": prediction.id,
                "sector": sector,
                "old_fund_code": prediction.fund_code,
                "old_fund_name": prediction.fund_name,
                "new_fund_code": mapping.fund_code,
                "new_fund_name": mapping.fund_name,
                "reset_verified": has_verdict_trace(prediction),
            }
            gap = calendar_gap(calendar, mapping.fund_code,
                               prediction.prediction_date, prediction.target_date)
            if gap:
                row["reason"] = gap
                unservable.append(row)
            else:
                candidates.append(row)

        details = [
            {key: value for key, value in candidate.items()
             if key not in ("prediction", "evidence")}
            for candidate in candidates
        ]
        result = {
            "dry_run": dry_run,
            "total_mappings": len(sector_map),
            "mappings_skipped_low_confidence": low_confidence,
            "min_confidence": min_confidence,
            "run_id": run_id,
            "would_update": len(candidates),
            "predictions_updated": 0,
            "predictions_unchanged": unchanged,
            "predictions_no_mapping": no_mapping,
            # 数出来就得说出口：这几条是"板块映射想改、但那只标的给不出证据"，
            # 不动它们才是对的，可"预览 326 / 实跑 320"那种差值必须在回执里看得见。
            "predictions_skipped_unservable": len(unservable),
            "skipped_unservable_details": [
                {key: value for key, value in row.items()
                 if key not in ("prediction", "evidence")}
                for row in unservable],
            "verified_reset": 0,
            "funds_added": 0,
            "funds_sector_updated": 0,
            "details": details,
        }
        if dry_run or not candidates:
            return result

        affected_bloggers = set()
        affected_funds = set()
        try:
            from src.fund.fund_sync_manager import FundSyncManager

            for candidate in candidates:
                prediction = candidate["prediction"]
                affected_funds.update(filter(None, [
                    candidate["old_fund_code"],
                    candidate["new_fund_code"],
                ]))
                # 改标的动作整体交给唯一入口：留痕、必要时清结论、把受影响博主登记进来
                # （原来这里自己写 `prediction.fund_code = ...` + 自己调 reset + 自己写日志，
                #  于是"唯一入口"这句承诺有第二个例外，第 18 轮 M-2）。
                days = calendar.get(candidate["new_fund_code"]) or []
                was_reset = FundSyncManager.retag_prediction(
                    self.db, prediction, candidate["new_fund_code"],
                    candidate["new_fund_name"], source="sector_mapping", run_id=run_id,
                    touched_bloggers=affected_bloggers,
                    evidence=(days, max(days) if days else None))
                # 回执只数**真的动了的行**：`retag_prediction` 那个布尔说的是"清没清结论"，
                # 而"已经是这个标的""被证据门拒了"回的都是 False —— 拿它当"改标成功"计数
                # 就会把什么都没做的行报成"更新 N 个预测"（第 51 轮 B-2 同一族，那次是
                # `update-all`，这一次是这里）。判"动没动"只看行上那个代码现在是什么。
                if prediction.fund_code != candidate["new_fund_code"]:
                    result["predictions_skipped_unservable"] += 1
                    continue
                result["predictions_updated"] += 1
                if was_reset:
                    result["verified_reset"] += 1

            self.db.flush()
            for blogger_id in affected_bloggers:
                recalculate_blogger_stats(self.db, blogger_id, commit=False)
            self._refresh_fund_counts(affected_funds)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _mapping_eligible(mapping: SectorFundMapping, min_confidence: float) -> bool:
        """老板手工确认/锁定的行永远有效；agent 自己盖过章的行按 agent 的门槛算。

        `confidence IS NULL` 且 `reviewed=True` 的行是本轮之前人工审查过的历史映射，
        它们本来就是人的结论，不能因为"没有置信度"被排除（排除会让 sync 静默变成空操作）。

        为什么这里不能再立第二个数字（S4a 第 6 轮 M2）：门槛曾同时存在两套——
        agent 用 `AUTO_REVIEW_CONFIDENCE`（direct 0.80 / proxy 0.68，由
        `scripts/calibrate_sector_agent.py` 在金标集上标定）决定"能不能自动置已审查"，
        本方法却写死 `min_confidence=0.85` 决定"能不能改预测"。于是 0.68~0.8499 区间
        成了死区：**同一行"审查通过"却"不够好到去修正结论"**。实测本地镜像库里
        12 条 agent 自批映射全部落在死区（人工智能 515070 0.8225、光伏 159857 0.8101、
        京A 012765 0.684、矿泉水 515170 0.7315……），其中 京A 那条的 pending 预测
        id 1238 至今挂着 `fund_code=000725`（`sector_identity_audit` 文件头那只
        "京东方Ａ"股票，也就是本轮迭代要修的"预测被别的品种验证"）。
        现在只有一套真值：达没达标由 agent 自己按 `match_kind` 判，本方法只认它盖的章。
        改完实测（本地镜像库，dry-run）：可改标映射 110 -> 122（+12 条全部来自死区），
        待改预测 0 -> 115 条，其中 pending 31 条、需重置旧结论 84 条。
        """
        from src.services.sector_identity_audit import row_unservable
        if row_unservable(mapping):
            # 镜像不变量的读侧兜底：agent 换标的时会把 is_fetchable 清成 NULL，
            # 只查列就会让"从没做过身份体检的新标的"直接驱动几百条预测改标
            return False
        if getattr(mapping, 'owner_locked', None) or \
                getattr(mapping, 'reviewed_by', None) == 'owner':
            return True
        confidence = getattr(mapping, 'confidence', None)
        if getattr(mapping, 'reviewed', False) and \
                getattr(mapping, 'reviewed_by', None) == 'agent' and confidence is not None:
            # agent 的章就是它自己按标定阈值盖的（`SectorDecision.auto_reviewable`），
            # 复核一遍同一个阈值即可；低于 agent 门槛还带着章 = 章是别处盖的（老板勾的、
            # 或阈值后来被调高过），退回调用方传入的 `min_confidence` 再判一次。
            from src.services.sector_fund_agent import (
                AUTO_REVIEW_CONFIDENCE, AUTO_REVIEW_PROXY_CONFIDENCE)
            gate = (AUTO_REVIEW_PROXY_CONFIDENCE
                    if getattr(mapping, 'match_kind', None) == 'proxy'
                    else AUTO_REVIEW_CONFIDENCE)
            if confidence >= gate:
                return True
        if confidence is None:
            return bool(getattr(mapping, 'reviewed', False))
        return confidence >= min_confidence

    def _lookup_mapping(self, sector_map: Dict, sector: Optional[str],
                        alias_targets: Optional[Dict] = None) -> Optional[SectorFundMapping]:
        """先精确命中，再走板块别名/归一化，避免同义板块漏改。

        别名从**库里**读，不用 `sector_fund_map._load_db_aliases()` 的进程内缓存 ——
        那个缓存可能在本次跑批之前就是空的，会让刚写入的别名"看不见"。
        `alias_targets` 是调用方一次读全表拿到的那份（生产实测每条预测各查一次会把这个
        按钮拖到 100 秒不返回）；没给才自己查一次，语义仍是"本次现读"。
        """
        if not sector:
            return None
        mapping = sector_map.get(sector)
        if mapping:
            return mapping
        try:
            from src.constants.sector_fund_map import normalize_sector_name
            normalized = normalize_sector_name(sector)
            if normalized in sector_map:
                return sector_map[normalized]
            if alias_targets is None:
                alias_targets = {a.alias_name: a.sector_name
                                 for a in self.db.query(SectorAlias).all()}
            if alias_targets.get(sector) in sector_map:
                return sector_map[alias_targets[sector]]
        except Exception:
            return None
        return None

    def _refresh_fund_counts(self, fund_codes) -> None:
        for fund_code in fund_codes:
            fund = self.db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if not fund:
                continue
            count = self.db.query(func.count(Prediction.id)).filter(
                Prediction.fund_code == fund_code,
                Prediction.is_deleted == False,
            ).scalar() or 0
            fund.active_predictions = count
            fund.can_delete = count == 0
