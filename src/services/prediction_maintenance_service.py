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
    def _sector_labels(prediction) -> list:
        """一条预测身上其实挂着**两个**板块标签，查映射时按顺序都问一遍。

        为什么要有这一条（任务 #171，实测）：`predictions.sector` 是 LLM 从正文里抽出来的**说法**
        （`A股` / `粮食`），而 `predictions.sector_type` 是它同时给出的**归类**（`综合` / `科技`）。
        老写法 `raw = prediction.sector or prediction.sector_type` 只在 `sector` **为空**时才看第二个，
        于是"sector 非空、而它谁都对不上"的行永远走不到已经能答出标的的那一行
        （镜像 2026-09-29 现读：`sector_type='综合'` 有 `018536`、`'宽基'` 有 `510300`、
        内置表对 `科技` 给 `515000`、对 `农业` 给 `159825`，而那 6 条 `pre_inception` 的未判行
        全卡在 `predictions_no_mapping` 里 ⇒ 只能被"永远问不出来"关掉，改不了标的）。

        边界一（这条只做**加法**）：`sector` 自己能查到行的那些行，第一步就命中并 break，
        字节级与改前同一条路 —— 所以它不会把"按 sector 定价"改成"按 sector_type 定价"。
        边界二（不会因此硬凑）：第二个标签查出来的新标的**仍要过同一条证据门**
        （`calendar_gap`，补标那一路还多一道"只紧不松"）⇒ 新标的给不出这段窗口的净值时，
        行进 `skipped_unservable` 并带逐行原因，一个字都不动。
        """
        labels = []
        for label in (prediction.sector, prediction.sector_type):
            if label and label not in labels:
                labels.append(label)
        return labels

    @staticmethod
    def _gap_label(sector: Optional[str]) -> Optional[str]:
        """板块标签在**进 gap-fill 这条计划表之前**先归一，但只认"把前后缀摘掉"那一种归一。

        为什么要归一：同一块板块的两种拼法（`黄金` / `黄金行情`、`白酒` / `RMAP白酒`）各进一次
        计划、各写一行映射、各绑各的预测，是第 66 轮复评 MI-6 点出的形状。

        为什么**只认这一种**：`normalize_sector_name` 里还有一条别名替换（`SECTOR_ALIASES`），
        它会把标签改成**另一块板块**——那种"归一"若当写侧的键，
        后果不是省一行映射而是**硬凑**：内置表本来对 `债券` 答不出标的（`get_fund_for_sector('债券')`
        实测 None ⇒ 该走"不猜，交给人工/agent"那一档），归成 `券商` 就答得出 512000 ⇒
        给一块债券板块绑上一只券商 ETF，并且写出的行署名 `seed`、看起来像机器审过。
        比第 27 轮立第三态要拦的`核聚变→红利低波`更坏：那一档至少有字面关系，被这道门拦回的那
        三个词形（2026-09-30 现扫两个库的在册未判标签，**两库逐字同数**：`金融` → `黄金`、
        `贵金属` → `黄金`、`债券` → `券商`，合计 3 个标签 / 53 **行次**）字面一个都不共用，
        命令逐字写在 `docs/模块总览/板块与基金匹配.md` 末尾那一节，原始输出随仓库走
        `docs/迭代计划/run-20260930-gate-scan/`（在不在仓库用 `git ls-files` 那目录核）。
        ⚠ **单位是"行次"不是"行"**：那把扫描的 SQL 把 `sector` 与 `sector_type` 两列并在一起数，
        同一行两列都写同一个标签就被数两遍（全表行次镜像 64 / 生产 62）。这三个标签恰好
        `count(distinct id)` 与行次同数（35 / 16 / 2）⇒ **那是巧合不是定义**，报数必须带单位。

        ⚠ **这道门的谓词是"归一结果必须是原标签的字面"，不是"不许改成另一块板块"**。它拦不住
        **改词之后仍是子串**那一族（同一次扫描：`海外科技` → `科技` 3 行次、`大盘指数` → `大盘` 1 行次。
        生产 `sector_fund_mapping` 今天**没有** `科技` 那一行（现读只出 `大盘 510300` / `黄金 518880`），
        所以 `海外科技` 只能走补标那一路；而内置表给 `科技` 的是 `515000 科技ETF华宝`（A 股）——
        一个"海外"标签拿到 A 股标的，这是**跨市场**的语义漂移 ⇒ 记成**潜伏边界**而不是已封。
        要说"封死了改词"得先给它加一条词表级的判据，别拿这道门当那个意思。

        顺带一句边界（第 67 轮复评 MAJOR-3 逼出来的实测）：**别名同义词不会被这条合并**——
        `绿色电力` 与 `绿电` 各自归一后仍是自己（`normalize_sector_name` 只对词形动手），
        所以这块表是"词形去重"，不是"同义词字典"；同义词要合并得靠 `sector_alias` 表里人登记的行。
        归一失败或原样更靠谱（摘不出、或改成了别的词）都返回原样。
        """
        if not sector:
            return sector
        try:
            from src.constants.sector_fund_map import normalize_sector_name
            norm = normalize_sector_name(sector) or sector
        except Exception:
            return sector
        return norm if norm in sector else sector

    def _gap_fill_candidate(self, sector: str, blocked_codes) -> Dict:
        """这个板块能不能从**内置表**拿到一只"本库给得出净值"的标的。

        为什么要有这一步（2026-09-29 现读，两个库各数一遍，日期现算）：压在"库里没有可用映射行"
        的板块标签上的未判预测，镜像 **180 条**、生产 **287 条**；旧写法只把它们数成
        `predictions_no_mapping` 然后一个字不做。
        ⚠ **不许把这档说成"永远躺在「待验证到期」里"**（第 67 轮复评 MAJOR-1/2 驳回了我上一版的
        立论）：这一批里**到期**的只有镜像 6 条 / 生产 5 条，其余（镜像 174、生产 282）目标日还在
        未来，压根还没进到期队列。这一档真正说的是"到那天时它自己那只标的问不出证据、而库里
        又没有任何映射行可依据"，所以补标是**给将来准备的处置**，不是当场清掉一片存量。
        今天真落在这个形状上的：生产 1 条（id 2695，挂在停更的 `003033` 上）、镜像 0 条
        （`predictions_via_gap_fill_planned = 0`）⇒ 这机制今天的暴露面就是 1 条，别写成几百条。
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
        # 今天（北京）取一次，不在循环里逐条问 —— 与队列/分类同一把钟（第 52 轮 B-3 那一族）。
        from src.services.prediction_lifecycle import current_as_of, is_expired_computed

        today = current_as_of()

        def _waiting_for_a_verdict(row: Prediction) -> bool:
            """**到期了、而结论还没有**（含被重问锁压着的那一档）—— 只有这一档值得为它另找标的。
            "有没有结论"问共用的 `has_verdict_trace`，"到没到期"问共用的 `is_expired_computed`，
            这里不立第二把尺子。还没到期的不动它：它现在问不出结果，等到期那天自然会问。"""
            return not has_verdict_trace(row) and is_expired_computed(row, as_of=today)

        missing_labels = []
        for prediction in predictions:
            labels = self._sector_labels(prediction)
            # ⚠ 查映射一律用**原样标签**。`_lookup_mapping` 内部是"原样 → 归一 → 库内别名(原样)"
            # 三步，别名那一步的输入就是原样串；在这里先归一等于把别名那一步的键换掉
            # ⇒ 靠别名命中的行再也查不到（第 66 轮返修：`tests/unit/test_sector_remap.py`
            # 四条一起红，`predictions_updated` 全成 0）。归一那**两**臂（查映射与补标计划表）
            # 现在都只走 `_gap_label` 那道门：只认"把前后缀摘掉"，不认"改出字面"（任务 #172）。
            if not labels:
                continue
            answered = any(self._lookup_mapping(sector_map, label, alias_targets)
                           for label in labels)
            # 两个标签里只要有一个已经在库里答出标的 ⇒ 下面逐条时轮不到补标计划表，
            # 这里也就不为它立计划（否则预览会说"这块要补一只"而一条预测都不落到它身上）。
            # ⚠ 一个**例外**（任务 #171）：到期还没结论的行，库里那个答案可能就是它现在挂着的
            # 那只验不了的标的 ⇒ 另一个标签（库里没有行的那一个）得有机会进补标计划表，
            # 否则"粮食→158038 答出了、农业压根没有行"这种行永远换不出去。
            for raw in labels:
                if answered and not _waiting_for_a_verdict(prediction):
                    continue
                if self._lookup_mapping(sector_map, raw, alias_targets):
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
        deferred = []
        for prediction in predictions:
            labels = self._sector_labels(prediction)
            # 查映射一律用**原样标签**（`_lookup_mapping` 内部那三步的输入）；归一那一臂
            # 在两条腿上都只走 `_gap_label`（摘前后缀才算，改出字面一律回原样）
            # —— 见上面建计划那一侧同一条注释（第 66 轮返修：先归一 ⇒ `test_sector_remap.py` 四条一起红）。
            looked = [(raw, self._lookup_mapping(sector_map, raw, alias_targets))
                      for raw in labels]
            library_hits = [(raw, hit, False) for raw, hit in looked if hit is not None]
            # 内置表（补标计划表）只答**库里没行的那些标签**；键与建计划那一侧同一把尺子
            # （`_gap_label`），归一前后各试一次：拿不准的标签原样返回，两种拼法都得沾上。
            plan_hits = []
            for raw, hit in looked:
                if hit is not None:
                    continue
                for key in (self._gap_label(raw), raw):
                    if key in gap_targets:
                        plan_hits.append((key, gap_targets[key], True))
                        break
            # 备选池：库里答案里剩下的那几个，**加上**内置表给剩下的标签答出的那些。
            # 优先级仍是"任何标签的库里答案都排在内置表之前"（那块板块已有人定过价，机器不越它）：
            # 库里一题都没答时才让整份计划表顶上（`hits = plan_hits`），那种情况下备选只有
            # `plan_hits[1:]` —— 把 `plan_hits` 再并一次会把同一只标的数成两条候选。
            if library_hits:
                hits, alts = library_hits, library_hits[1:] + plan_hits
            else:
                hits, alts = plan_hits, plan_hits[1:]
            if not hits:
                no_mapping += 1
                continue
            if prediction.fund_code == hits[0][1].fund_code:
                # 第一个答案正是它现在挂着的那只。⚠ 但有一种行不能就此收工（任务 #171）：
                # **到期了、结论还没有**的那些（含被重问锁压着、`classify` 报「结构性不可验」的那一档）。
                # 它挂着的可能正是"这段窗口问不出净值"的那只，而另一个标签给出的**另一只**
                # 从没被问过 ⇒ 老板那句"抓取不到且确认没有办法 ⇒ 换成别的基金"落的正是这一格。
                # 备选**包括内置表那一档**：镜像现读 6 条这样的行里，`2303/2304`（A股→515440 挂着，
                # 第二个标签「科技」库里没行）与 `3076`（粮食→158038，第二个标签「农业」同理）
                # 只有内置表答得出，早期版本只看 `len(hits) > 1` 就把这三条数成 `unchanged`。
                # 这里只**收集候选**，换不换由下面 `deferred` 那段用共用那把尺子问：
                # 这种行按定义没有结论可清，最坏情况是"没换"，不会更坏。
                if alts and _waiting_for_a_verdict(prediction):
                    deferred.append((prediction, [hits[0]] + alts))
                else:
                    unchanged += 1
                continue
            key, mapping, via_gap = hits[0]
            # `sector` 交回给下面的是**这块板块在补标计划里的键**（不是预测自己那个标签）：
            # 只有走了补标那一路才有它。把它当"标签在不在计划表里"来判 `via_gap` 是不可的
            # ——一条命中库里映射行的预测，其归一标签可能恰好等于别的板块的键。
            pairs.append((prediction, mapping, key, via_gap))

        # 证据门：**预览与实跑必须问同一句话、给出同一个数**。第 100 轮那道门当时只装在
        # `retag_prediction` 里面，而 dry-run 那支根本不调它 ⇒ 2026-09-26 生产实测
        # 预览说「将更新 326 条」、真跑只会动 320 条，那 6 条（`158038`/`012765` 那几只
        # 首笔净值晚于窗口的新产品）当场会变成"到期永不判"。日历一次读全，别在循环里查。
        from src.services.prediction_lifecycle import (
            calendar_answer, calendar_gap, nav_calendar)

        # 日历里连**预测自己那只标的**一起读：下面"它现在问得出证据吗"那一问要用它，
        # 而分开两次查就是同一把尺子两腿两种待遇（第 54 轮那一族）。
        # ⚠ 只读**补标的那一路**要用的自有标的：命中库里映射行的那一路"要不要动"只看新标的那一只
        # （理由见下面那道门前的注释），把每条预测自己的代码都塞进这一次读，等于让全库预测
        # 各把自己的标的带上（第 66 轮复评 MI-6：生产那一路读取量翻倍，而这一档从来没被量过）。
        wanted_codes = set()
        for prediction, mapping, sector, via_gap in pairs:
            wanted_codes.add(mapping.fund_code)
            if via_gap and prediction.fund_code:
                wanted_codes.add(prediction.fund_code)
        # `deferred` 那一档要问的码一起读：它自己的那只（问"这段窗口答得出吗"）+ 各只备选。
        # 这一档今天只有"到期还没结论"那么几十行（镜像 2026-09-30 现读 18 条到期未判），
        # 不是"每条预测都把自己的标的带上"——第 66 轮复评 MI-6 量的正是后者（生产读取量翻倍）。
        for prediction, hits in deferred:
            if prediction.fund_code:
                wanted_codes.add(prediction.fund_code)
            for _key, alt, _gap in hits:
                wanted_codes.add(alt.fund_code)
        wanted = sorted(code for code in wanted_codes if code)
        calendar = nav_calendar(self.db, wanted)
        # 有没有档案要**一次读全**：逐条预测各查一次 `fund_info` 就是生产那次
        # 「预览 100 秒零字节」的同一个形状（第 51 轮）。
        archived = {row[0] for row in self.db.query(FundInfo.fund_code).filter(
            FundInfo.fund_code.in_(wanted)).all()} if wanted else set()

        # 现在才答得出上面留给它的那一问（日历与档案都在手上了）。
        for prediction, hits in deferred:
            # 先问**它自己现在挂着的那只标的**对这段窗口答的是什么（与门共用同一次切片、
            # 同一组 config 阈值）。只有 `cannot`（这段净值不会再来）才允许换：
            # `unknown` 的两格病因各是"补一次净值"与"改那条预测的起点日期"，
            # **都不是**"换一只基金"（第 69 轮 MINOR-6 那把尺子）。
            kind, _reason, _cause = calendar_answer(
                calendar, prediction.fund_code,
                prediction.prediction_date, prediction.target_date)
            moved = False
            if kind == 'cannot':
                for key, alt, via_gap in hits[1:]:
                    if not alt.fund_code or alt.fund_code == prediction.fund_code:
                        continue
                    # 换了也问不出来 ⇒ 不动它：这一档要的是"能判出来"，不是"换个标的继续验不了"。
                    if calendar_gap(calendar, alt.fund_code,
                                    prediction.prediction_date, prediction.target_date):
                        continue
                    pairs.append((prediction, alt, key, via_gap))
                    moved = True
                    break
            if not moved:
                # 与今天同一条归宿：它仍挂在自己那只标的上 ⇒ 数进 `unchanged`，
                # 回执里不会多出一条"动了但其实没动"的行。
                unchanged += 1
        kept_own_target = 0
        kept_window_not_due = 0
        kept_no_nav = 0
        kept_no_start = 0
        for prediction, mapping, sector, via_gap in pairs:
            # **只紧不松**的门，装在补标那一路，不装在有映射行那一路 —— 这一条是刻意的，
            # 不是漏了（第 67 轮复评 MAJOR-5 指出"同一把尺子两腿两种待遇"，答复写在这儿，
            # 免得下一轮把它当洞补成墙）：两腿问的不是同一个问题。
            # · 库里**有**映射行 ⇒ "这个板块该由哪只标的定价"已经有人（老板审查过 / agent 匹配过）
            #   答过一次；换标的要动的是那条**决定**，而旧结论会由 `retag_prediction` 清掉、
            #   再按新标的重新判出来（新标的给不出证据的那种已经被 #100/#105 那道门拦下）。
            #   反例是本仓自己的任务 #101：15 条挂在 `508031`（宽基指数挂错标的）上的预测
            #   被改指到 `510300` —— 那只挂错的标的**给得出**这段窗口的净值，若把这道门也套在
            #   这一路，这类"标的挂错了但净值好好的"的行就永远改不过来。
            # · 库里**没有**映射行 ⇒ 没人替这个板块做过决定，是机器自己从内置表挑一只。
            #   这时候把"自己那只标的问得出证据"的预测搬走，纯粹是拿一个新造的代理去替换
            #   一份还能自证的结论 ⇒ 老板那句"抓取不到且确认没有办法 ⇒ 才换成别的基金"圈的正是这一档。
            # 少了这道门，一次按钮就把**自己那只标的好好的**预测换成板块代理标的、顺手清掉
            # 已有结论（第 18 轮"一键清空 515 条结论"的同一个形状）。
            # 镜像 2026-09-29 02:39 同一份代码、只把这道门换成 `if False`，两趟预览各印：
            #   加门   would_update 28 / kept 481 给得出 + 155 还没到期 + 0 答不出 / via_planned 0 / skipped 1
            #          （"答不出"这一档第 69 轮再拆两格：0 条没净值 + 0 条说不清起点，两库今天都是 0）
            #   不加门 would_update 655 / kept 0 / via_planned 627 / skipped 10
            #              而那 627 条里 **470 条带着已判结论**（改标就会被清掉）
            # 两个口径合得起来：655 = 28 + 627，636 = 627 + 9（那 9 条即使改标也会被
            # 新标的的证据门拦下 ⇒ skipped 从 1 涨到 10）。复核命令在
            # `docs/模块总览/板块与基金匹配.md` 末尾那一节（数会随镜像数据走，别抄文本）。
            # 有档案、库里却一行净值都没有 ⇒ **不动**：那是"还没同步过"（跑一次「更新基金」
            # 就补上），不是"确认没办法"；`calendar_gap` 对这种窗口本来就是放行不拦。
            # 这条预测**自己那只标的**对这段窗口答的是什么（`calendar_answer` 与门共用一次切片、
            # 一组阈值）：`cannot` ⇒ 该动；`evidenced` ⇒ 不动且话是"给得出"；
            # `not_due` / `unknown` ⇒ 也不动，但话只能说"现在还没到问的时候/说不清"
            # （第 67 轮复评 MAJOR-8：把这三档一起说成"自己就给得出净值"是 189/830 条的假话）。
            kind, _reason, cause = calendar_answer(
                calendar, prediction.fund_code,
                prediction.prediction_date, prediction.target_date) \
                if (via_gap and prediction.fund_code and prediction.fund_code in archived) \
                else ('cannot', None, None)
            if kind in ('evidenced', 'not_due', 'unknown'):
                if kind == 'evidenced':
                    kept_own_target += 1
                elif kind == 'not_due':
                    kept_window_not_due += 1
                elif cause == 'no_nav':
                    # 有档案、库里一笔净值都没有 ⇒ 缺的是**净值行**，不是日历：
                    # 「更新基金」能答，等到期答不出（第 68 轮 MINOR-1）。
                    kept_no_nav += 1
                else:
                    # 窗口起点说不清 ⇒ 补净值也不会变，这一格缺的是那条预测自己的起点日期
                    # （第 69 轮 MINOR-6：上一版把它与上面那格共用一句"先跑一次「更新基金」"）。
                    kept_no_start += 1
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
            # 同一道门放行的**另一半**：这段窗口还没到期 ⇒ 也不动它，
            # 但那不是"它自己给得出净值"。分开数、分开说（第 67 轮复评 MAJOR-8）。
            "predictions_kept_window_not_due": kept_window_not_due,
            # 第三、四档：这把尺子**答不出**，两种病因各有各的动作
            # （第 68 轮 MINOR-1 拆出这一档，第 69 轮 MINOR-6 再按病因拆成两格）。
            # 总键 `predictions_kept_answer_unknown` 保留 = 两格之和，是老口径的下界，
            # 不是第三把尺子 —— 两格都由 `evidence_answer` 的 `cause` 决定。
            "predictions_kept_no_nav": kept_no_nav,
            "predictions_kept_no_start": kept_no_start,
            "predictions_kept_answer_unknown": kept_no_nav + kept_no_start,
            # 这一轮**真会被清掉结论**的条数：`reset_verified` 逐行早就带着，可从没人把它数成
            # 一句给老板看的话（第 67 轮复评 MAJOR-5）。预览里先说数、再让他点执行，
            # 这一档才叫"事前知道"，而不是事后翻台账。
            "predictions_with_verdict": sum(1 for candidate in candidates
                                            if candidate["reset_verified"]),
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
            # 归一那一腿用的是补标那一路同一道门（`_gap_label`）：谓词是"归一结果必须是原标签的
            # 字面"，所以只认"把前后缀摘掉"，不认"改出字面"（`金融 → 黄金` 那种）。
            # 它拦不住"改词之后仍是子串"那一族（`大盘指数 → 大盘`）—— 边界写在 `_gap_label` 的
            # docstring 里，别把这一句读成"这道门不许改成另一块板块"（第 85 轮 A-3：谓词管的是
            # **字面**，不是**语义**；要拦语义得另加词表级判据，任务 #172 结的是"门装在查映射这一路"，
            # 没有结那一半）。
            # 生产**旧构建** 2026-09-30 那份只读预览里 144 行待改标有 81 行是 `金融 → 黄金`
            # （共用一个"金"字的模糊别名）这一族 ⇒ 板块 `金融` 被绑到 `518880 黄金ETF华安`
            # 那一行映射上。第 67 轮 MAJOR-3 那道门当时只装在补标那一路，查映射这一路没有
            # ⇒ 同一个词形归一、两条路两种待遇。
            normalized = self._gap_label(sector)
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
