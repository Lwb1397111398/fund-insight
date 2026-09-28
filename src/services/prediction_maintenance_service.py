"""预测维护操作：默认只读预览，写入必须由路由显式确认。"""

import json
from collections import defaultdict
from datetime import date
from types import SimpleNamespace
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import (Blogger, FundInfo, Prediction,
                                 SectorAlias, SectorFundMapping)
from src.services.prediction_change_log_service import (
    add_prediction_change_log,
    snapshot_prediction,
)
from src.services.prediction_lifecycle import archive_stamp
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
                    # 归档那一对时间戳不许自己算（第 54 轮 A-1 / B-2）：上一批把
                    # `_soft_archive` 改成北京钟时漏了这条活路（页面「合并相似预测」真在走它），
                    # 于是"合并掉的那批行"在 Render 上仍然少一天。
                    prediction.deleted_at, prediction.restore_before = archive_stamp()
                    prediction.deleted_by = "maintenance"
                    prediction.delete_reason = f"duplicate_of_{group['keep_id']}"
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

    GAP_FILL_SOURCE = 'seed_builtin'

    @staticmethod
    def _gap_label(sector: Optional[str]) -> Optional[str]:
        """板块标签在**进 gap-fill 这条计划表之前**先归一。

        为什么必须先归一：`_lookup_mapping` 读的那一侧走的是"原样 → 归一 → 库内别名"三步
        （`:575-588`），而这一侧原来拿原样标签当键 ⇒ `绿色电力` 与 `绿电` 各进一次计划、
        各写一行映射、各绑各的预测（第 66 轮复评 MI-6）。同一个键的两种拼法不该有两种待遇，
        而"怎么归一"这条尺子全仓只有 `normalize_sector_name` 一把 —— 这里只是把它的结果
        也用作**写**侧的键。归一失败（原样更靠谱）就用原样。
        """
        if not sector:
            return sector
        try:
            from src.constants.sector_fund_map import normalize_sector_name
            return normalize_sector_name(sector) or sector
        except Exception:
            return sector

    def _gap_fill_candidate(self, sector: str, blocked_codes) -> Dict:
        """这个板块能不能从**内置表**拿到一只"本库给得出净值"的标的。

        为什么要有这一步（2026-09-28 实测）：镜像上 33 个板块标签压着 200 条未判预测，
        它们在库里**一行映射都没有**（`sector_alias` 0 行），旧写法只把它们数成
        `predictions_no_mapping` 然后一个字不做 ⇒ 这些预测永远躺在「待验证到期」里。
        老板要的正是这一档的处置："板块对应的基金抓取不到且确认没有办法 ⇒ 把该板块
        变成其他好的基金"，而板块→基金那条链早就有了（`get_fund_for_sector`：内置表
        + 硬编码/库内别名 + "不许硬凑"名单），这里只是把它接进同步器。

        只回答三件事，都在**本库**现读，不打网络：
          ① 内置表说得出这只标的（名单里的板块答不出 ⇒ 不猜，交给 agent/人工）；
          ② 本库有 `fund_info` 档案（`sector_fund_mapping.fund_code` 有外键，
             没档案插进去就是 IntegrityError，第 50 轮那族悬空行）；
          ③ 档案至少有一笔净值。
        **窗口够不够证据不在这里判** —— 那一问仍由 `calendar_gap` 一把尺子逐条回答，
        本方法不立第二个数字（否则会重演第 56 轮"门与队列两种判法"）。
        `blocked_codes` 让这个板块**库里已有**的行用过的代码：内置表给的就是那只
        已确认拿不到净值的标的 ⇒ 换了等于没换，要说出来而不是新建一行同码的映射。
        """
        if not sector or len(sector) > 50:
            # 列宽 String(50)：长过它 INSERT 在生产 PostgreSQL 上会直接报错
            return {'refused': '板块名空着或长过列宽，不猜', 'kind': 'label_unusable'}
        from src.constants.sector_fund_map import get_fund_for_sector
        from src.services.prediction_lifecycle import nav_calendar
        hit = get_fund_for_sector(sector) or {}
        code = (hit.get('code') or '').strip()
        if not code:
            return {'refused': '内置表也说不出这个板块的对口标的 ⇒ 交给人工或 agent 匹配',
                    'kind': 'no_static_hit'}
        if code in blocked_codes:
            return {'code': code, 'name': hit.get('name') or code,
                    'refused': '内置表给的正是库里那只标的（%s）⇒ 换个标的得先有人给出更好的答案' % code,
                    'kind': 'same_as_current'}
        info = self.db.query(FundInfo).filter(FundInfo.fund_code == code).first()
        if not info:
            return {'code': code, 'name': hit.get('name') or code,
                    'refused': '本库还没有 %s 的档案 ⇒ 先跑一次「更新基金」再对齐' % code,
                    'kind': 'no_archive'}
        # "这只标的最末一笔在哪天"问的是**共用那把日历**（`nav_calendar`），不在这里
        # 再造一份 `max(nav_date)`：那道"全库最新净值日只许一个出处"的棘轮
        # （`test_the_nav_cutoff_date_has_exactly_one_implementation`）第一次跑就把这一腿
        # 点红了 —— 登记成"另一把尺子"是把它当借口，接上共用那把才是修。
        days = nav_calendar(self.db, [code]).get(code) or []
        if not days:
            return {'code': code, 'name': info.fund_name or code,
                    'refused': '%s 在库里一行净值都没有 ⇒ 现在绑上也验证不了' % code,
                    'kind': 'no_nav'}
        return {'code': code, 'name': info.fund_name or hit.get('name') or code,
                'last_nav': str(max(days))}

    def _apply_gap_fill(self, sector: str, cand: Dict,
                        rows: List[SectorFundMapping]) -> None:
        """把某板块的新标的落到映射表上：已有行就**改写那一行**，一行都没有才新增。

        为什么已有行不能"另加一行"：生产库里有一条模型没声明的
        `sector_fund_mapping_sector_name_key UNIQUE(sector_name)`（2026-09-22 直连
        `pg_constraint` 实测，镜像没有）⇒ 同一板块再插一行在生产直接撞约束，
        镜像却静默成功（第 49 轮那条"镜像演练通过不等于生产能过"的同一个坑）。

        为什么这算"机器审过"而不是绕过审查：这一档的标的来自 `SECTOR_FUND_MAP`，
        那张表由 `scripts/audit_static_sector_map.py` 按"板块↔名册官方名"字面相关
        逐行体检（退码 0 才算干净，新增不登记就红，见 `tests/unit/test_sector_map_guard.py`）。
        ⚠ **不要把这句扩大成"标的过了一道咽喉"**：建档身份门
        （`SectorFundService.ensure_fund_info_exists`）第 50 轮已量清**不是**所有写 `fund_info`
        的活路的共同门（`git grep -c "FundInfo(" -- src scripts` 数出 8 处构造点都不经过它）。
        这一档的凭据只有"内置表逐行体检过 + 本库现读它有档案有净值"，别的一律不承诺。
        `reviewed_by='seed'` 不是老板署名：`row_unservable()` 的 owner 例外不认它，
        下一次身份体检照样能把这一行判下去。
        """
        from src.services.prediction_lifecycle import current_as_of
        evidence = json.dumps({
            'source': self.GAP_FILL_SOURCE, 'sector': sector,
            'code': cand['code'], 'name': cand['name'],
            'last_nav': cand.get('last_nav'), 'decided_at': str(current_as_of()),
            'why': '这个板块在库里没有可用的映射行 ⇒ 按内置板块表补上它答得出的对口标的',
        }, ensure_ascii=False)
        note = '库里没有可用的映射行 ⇒ 按内置板块表补上对口标的'
        if cand['mode'] == 'update' and rows:
            row = rows[0]
            row.fund_code, row.fund_name = cand['code'], cand['name']
            row.reviewed, row.reviewed_by = True, 'seed'
            row.match_source, row.match_kind = self.GAP_FILL_SOURCE, 'direct'
            # 换标的就把"可服务"那一列清回"从没体检过"：留着旧标的的结论替新标的代言，
            # 正是第 8/9 轮那族幽灵行的成因（agent 换标的时做的也是同一件事）。
            row.is_fetchable = None
            # **改完之后这一行必须重新被同步器自己认到**，否则"补上标的"是一句空话：
            # 只有"本来就对同步器不可见"的行才会走到这里（`missing_labels` 的来路），
            # 而不可见有三个原因，这里三个都要一次抹掉 ——
            # ① `is_active=False`：`:257` 那条取行条件第一句就把它挡在 `sector_map` 外，
            #    留着它 ⇒ 下一次跑批这个板块又是"没有可用映射"，而 `blocked_codes` 此时
            #    正好等于刚写进去的那只 ⇒ 从此永久回"内置表给的就是库里那只"，谁都不再动它
            #    （第 66 轮复评 MA-1，内存 sqlite 两遍跑批实测）；
            # ② `confidence`：旧标的那一个分数不替新标的说话（与 ① 那条 `is_fetchable=None`
            #    同一个道理）。留着 agent 给的 0.70 而把署名改成 `seed`，就正好落回
            #    `_mapping_eligible` 自己 docstring 写明"本轮修的就是"的那个死区
            #    （MA-2：署章的人变了、门槛还按 agent 那一臂算 ⇒ 补完仍不合格）；
            # ③ `reviewed`：这一行的新标的来自逐行体检过的内置表，`confidence is None`
            #    那一条出口按"没有机器分数可核"处理（`_mapping_eligible:537`），
            #    所以 `reviewed=True` 就是它合格的凭据 —— 这也是"人工审查过的历史映射"
            #    那一档走的同一条出口，不是这里新立的门槛。
            row.is_active = True
            row.confidence = None
            row.evidence, row.verify_message = evidence, note
            return
        self.db.add(SectorFundMapping(
            sector_name=sector, fund_code=cand['code'], fund_name=cand['name'],
            is_active=True, reviewed=True, reviewed_by='seed',
            match_source=self.GAP_FILL_SOURCE, match_kind='direct',
            verify_message=note, evidence=evidence))

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

        # 没有可用映射的板块**按板块**算一遍，而不是按预测：一条预测各查一次库正是
        # 生产那次「100 秒零字节」的根因（第 51 轮）。第二遍逐条判证据时只在内存里查。
        missing_labels = []
        for prediction in predictions:
            raw = prediction.sector or prediction.sector_type
            # ⚠ 查映射一律用**原样标签**。`_lookup_mapping` 内部是"原样 → 归一 → 库内别名(原样)"
            # 三步，别名那一步的输入就是原样串；在这里先归一等于把别名那一步的键换掉
            # ⇒ 靠别名命中的行再也查不到（第 66 轮返修：`tests/unit/test_sector_remap.py`
            # 四条一起红，`predictions_updated` 全成 0）。归一**只**用作补标计划表的键。
            if not raw or self._lookup_mapping(sector_map, raw, alias_targets):
                continue
            label = self._gap_label(raw)
            if label and label not in missing_labels:
                missing_labels.append(label)
        existing_rows = {}
        if missing_labels:
            # 排序与上面建 sector_map 那一条**同一把尺子**（更新的在前）：真要改写已有行时，
            # 改的必须就是"这个板块当前代表它的那一行"，不是随便捞到的某一行。
            for row in self.db.query(SectorFundMapping).filter(
                    SectorFundMapping.sector_name.in_(missing_labels)).order_by(
                    SectorFundMapping.updated_at.desc(),
                    SectorFundMapping.id.desc()).all():
                existing_rows.setdefault(row.sector_name, []).append(row)
        gap_plan = {}
        for label in missing_labels:
            rows = existing_rows.get(label, [])
            cand = self._gap_fill_candidate(label, {r.fund_code for r in rows})
            # 库里已经有行、而且那一行是老板署名挑定的（"有意代理"）⇒ 不自动换标的。
            # 生产上还有一条模型没声明的 `sector_name UNIQUE` 约束（2026-09-22 实测），
            # 所以"同一板块再加一行"在生产会直接撞约束 —— 已有行只能改、不能添。
            owner_backed = [r for r in rows if getattr(r, 'owner_locked', None)
                            or getattr(r, 'reviewed_by', None) == 'owner']
            if owner_backed:
                cand = {'code': cand.get('code'),
                        'kind': 'owner_locked',
                        'refused': '这个板块库里那行的标的是老板署名挑定的 ⇒ 不自动换，要换请你在板块匹配页改'}
            gap_plan[label] = dict(cand, mode=('update' if rows and not cand.get('refused')
                                               else 'insert'))
        # 预览不写库，但"这块要补哪只标的、为什么补不了"必须当场看得见；
        # 实跑这边只**记计划**，真的动库排在这里之后 —— 只有真有预测落到某个板块的
        # 新标的上才写那一行（否则会留下"一行映射建了、一条预测都没动"的孤儿行）。
        gap_targets = {}
        for label, cand in gap_plan.items():
            if cand.get('refused'):
                continue
            gap_targets[label] = SimpleNamespace(
                sector_name=label, fund_code=cand['code'], fund_name=cand['name'])

        candidates = []
        unservable = []
        unchanged = 0
        no_mapping = 0
        pairs = []
        for prediction in predictions:
            raw = prediction.sector or prediction.sector_type
            mapping = self._lookup_mapping(sector_map, raw, alias_targets)
            gap_key = None
            if mapping is None:
                # 库里没有行 ⇒ 才轮到补标计划表。键与建计划那一侧同一把尺子（`_gap_label`），
                # 归一前后各试一次：拿不准的标签原样返回，两种拼法都得沾上。
                for key in (self._gap_label(raw), raw):
                    if key in gap_targets:
                        mapping, gap_key = gap_targets[key], key
                        break
            if not mapping:
                no_mapping += 1
                continue
            if prediction.fund_code == mapping.fund_code:
                unchanged += 1
                continue
            # `sector` 交回给下面的是**这块板块在补标计划里的键**（不是预测自己那个标签）：
            # 只有走了补标那一路才有它。把它当"标签在不在计划表里"来判 `via_gap` 是不可的
            # ——一条命中库里映射行的预测，其归一标签可能恰好等于别的板块的键。
            pairs.append((prediction, mapping, gap_key or raw, gap_key is not None))

        # 证据门：**预览与实跑必须问同一句话、给出同一个数**。第 100 轮那道门当时只装在
        # `retag_prediction` 里面，而 dry-run 那支根本不调它 ⇒ 2026-09-26 生产实测
        # 预览说「将更新 326 条」、真跑只会动 320 条，那 6 条（`158038`/`012765` 那几只
        # 首笔净值晚于窗口的新产品）当场会变成"到期永不判"。日历一次读全，别在循环里查。
        from src.services.prediction_lifecycle import calendar_gap, nav_calendar

        # 日历里连**预测自己那只标的**一起读：下面"它现在问得出证据吗"那一问要用它，
        # 而分开两次查就是同一把尺子两腿两种待遇（第 54 轮那一族）。
        # ⚠ 只读**补标的那一路**要用的自有标的：其余预测"要不要动"只看新标的那一只，
        # 把每条预测自己的代码都塞进这一次读，等于让全库预测各把自己的标的带上
        # （第 66 轮复评 MI-6：生产那一路读取量翻倍，而这一档从来没被量过）。
        wanted_codes = set()
        for prediction, mapping, sector, via_gap in pairs:
            wanted_codes.add(mapping.fund_code)
            if via_gap and prediction.fund_code:
                wanted_codes.add(prediction.fund_code)
        wanted = sorted(code for code in wanted_codes if code)
        calendar = nav_calendar(self.db, wanted)
        # 有没有档案要**一次读全**：逐条预测各查一次 `fund_info` 就是生产那次
        # 「预览 100 秒零字节」的同一个形状（第 51 轮）。
        archived = {row[0] for row in self.db.query(FundInfo.fund_code).filter(
            FundInfo.fund_code.in_(wanted)).all()} if wanted else set()
        kept_own_target = 0
        for prediction, mapping, sector, via_gap in pairs:
            # 补标的这一档多一道**只紧不松**的门：板块本来没有可用映射时，只动"问不出
            # 这段窗口净值"的那些 —— 老板那句话圈定的是「抓取不到且确认没有办法」。
            # 少了这道门，一次按钮就把**自己那只标的好好的**预测换成板块代理标的、顺手清掉
            # 已有结论（第 18 轮"一键清空 515 条结论"的同一个形状）。
            # 镜像 2026-09-29 02:39 同一份代码、只把这道门换成 `if False`，两趟预览各印：
            #   加门   would_update 28 / kept_own_target 636 / via_planned 0 / skipped 1
            #   不加门 would_update 655 / kept 0 / via_planned 627 / skipped 10
            #              而那 627 条里 **470 条带着已判结论**（改标就会被清掉）
            # 两个口径合得起来：655 = 28 + 627，636 = 627 + 9（那 9 条即使改标也会被
            # 新标的的证据门拦下 ⇒ skipped 从 1 涨到 10）。复核命令在
            # `docs/模块总览/板块与基金匹配.md` 末尾那一节（数会随镜像数据走，别抄文本）。
            # 有档案、库里却一行净值都没有 ⇒ **不动**：那是"还没同步过"（跑一次「更新基金」
            # 就补上），不是"确认没办法"；`calendar_gap` 对这种窗口本来就是放行不拦。
            if via_gap and prediction.fund_code and \
                    prediction.fund_code in archived and not calendar_gap(
                    calendar, prediction.fund_code,
                    prediction.prediction_date, prediction.target_date):
                kept_own_target += 1
                continue
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
                "via_gap_fill": via_gap,
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
        # 只有"真有预测要动"的板块才会为它建/改映射行 —— 预览与实跑用同一个集合，
        # 否则预览说补 50 块、实跑只动 1 块，又是一次"预览与实跑不同数"（第 51 轮 B-2 那族）。
        gap_used = sorted({candidate["sector"] for candidate in candidates
                           if candidate["via_gap_fill"]})
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
            # "板块新补的标的"没抢走任何一条自己就问得出证据的预测 —— 这个数要说出口，
            # 它是那道"只紧不松"的门真的在挡事的凭据。
            "predictions_kept_own_target": kept_own_target,
            # 板块没有标的这一档：数出来就必须说出口"补几块、各补哪只、哪些补不了以及为什么"
            "sectors_without_target": len(gap_plan),
            "sectors_fillable": [
                {"sector": label, "fund_code": cand["code"], "fund_name": cand["name"],
                 "last_nav": cand.get("last_nav"), "mode": cand["mode"]}
                for label, cand in gap_plan.items() if not cand.get("refused")],
            "sectors_to_fill": [
                {"sector": label, "fund_code": gap_plan[label]["code"],
                 "fund_name": gap_plan[label]["name"],
                 "last_nav": gap_plan[label].get("last_nav"),
                 "mode": gap_plan[label]["mode"]} for label in gap_used],
            # **计划**与**做到**是两个数，各占一个键（第 66 轮复评 MA-4：原来这一格
            # 拿 `candidates` 定形时的数当"改到它身上 M 条"报，那是"要不要做"不是"做了"，
            # 而 `sectors_filled` 更是排在 retag 循环之前 —— 同一族第 51 轮 B-2 已定过性）。
            # 实跑那两个 `_planned` 键仍然留着：预览与实跑的差值要在回执里看得见。
            "predictions_via_gap_fill_planned": sum(1 for candidate in candidates
                                                    if candidate["via_gap_fill"]),
            "predictions_via_gap_fill": 0,
            "sectors_refused_to_fill": [
                {"sector": label, "fund_code": cand.get("code"),
                 "kind": cand.get("kind"), "reason": cand["refused"]}
                for label, cand in gap_plan.items() if cand.get("refused")],
            "sectors_filled": 0,
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

            # 映射行与预测改标写在**同一个事务**里：只落一半就是"映射说这块是 518880、
            # 预测还挂在死码上"那种自相矛盾的行；任何一步抛错，下面 except 整批回滚。
            for label in gap_used:
                self._apply_gap_fill(label, gap_plan[label], existing_rows.get(label, []))
            result["sectors_filled"] = len(gap_used)

            for candidate in candidates:
                prediction = candidate["prediction"]
                affected_funds.update(filter(None, [
                    candidate["old_fund_code"],
                    candidate["new_fund_code"],
                ]))
                # 改标的动作整体交给唯一入口：留痕、必要时清结论、把受影响博主登记进来
                # （原来这里自己写 `prediction.fund_code = ...` + 自己调 reset + 自己写日志，
                #  于是"唯一入口"这句承诺有第二个例外，第 18 轮 M-2）。
                # 证据交给**窗口内那一段**：`retag_prediction` 收的 `evidence` 第一元
                # 按契约是"这段窗口里的净值日"（`window_evidence:363-387`、`calendar_gap:394-401`
                # 都是这么切的），而这里原来直接把 `nav_calendar` 的**全量**历史递进去
                # ⇒ 同一把尺子在预览那一腿收到切片、在实跑这一腿收到超集，只会更松
                # （第 66 轮复评 MA-4/MI-4：于是"候选=动成"是偶然成立，不是构造成立）。
                # 切片这件事仍然只在 `window_from_calendar` 一处做，不在这里抄 `start<=d<=end`。
                from src.services.prediction_lifecycle import window_from_calendar

                days, latest = window_from_calendar(
                    calendar, candidate["new_fund_code"],
                    prediction.prediction_date, prediction.target_date)
                was_reset = FundSyncManager.retag_prediction(
                    self.db, prediction, candidate["new_fund_code"],
                    candidate["new_fund_name"], source="sector_mapping", run_id=run_id,
                    touched_bloggers=affected_bloggers,
                    evidence=(days, latest))
                # 回执只数**真的动了的行**：`retag_prediction` 那个布尔说的是"清没清结论"，
                # 而"已经是这个标的""被证据门拒了"回的都是 False —— 拿它当"改标成功"计数
                # 就会把什么都没做的行报成"更新 N 个预测"（第 51 轮 B-2 同一族，那次是
                # `update-all`，这一次是这里）。判"动没动"只看行上那个代码现在是什么。
                if prediction.fund_code != candidate["new_fund_code"]:
                    result["predictions_skipped_unservable"] += 1
                    continue
                result["predictions_updated"] += 1
                if candidate["via_gap_fill"]:
                    # "补标的这条路真的把 N 条换过去了"这一句用的是**改完之后**的数，
                    # 不是建候选时那份计划（见上面 `_planned` 那对键）。
                    result["predictions_via_gap_fill"] += 1
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
