"""
板块资金流向核心服务
负责：计算衍生指标、数据保存、排行榜查询、历史趋势、板块-基金联动

核心算法（v3 - 对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离，既不算主力也不算散户）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）
- 主力暗盘 = 主力净流入 − 散户净流入
- 主力强度 = (主力暗盘 / 板块成交额) × 100
- 行为判定：≥3抢筹 / 1~3建仓 / -1~1洗盘 / ≤-1出货
"""
import logging
from typing import Dict, List, Optional
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, asc, nulls_last

from src.models.database import SectorFundFlow, SectorFlowFetchRun, SectorFundMapping
from src.crawler.sector_flow_crawler import SectorFlowCrawler

logger = logging.getLogger(__name__)

# 行为判定阈值
BEHAVIOR_THRESHOLDS = {
    "grab": 3.0,    # ≥ 3 抢筹
    "build": 1.0,   # ≥ 1 建仓
    "wash_low": -1.0,  # > -1 洗盘
    # ≤ -1 出货 (sell)
}

# 行为中文映射
BEHAVIOR_CN = {
    "grab": "抢筹",
    "build": "建仓",
    "wash": "洗盘",
    "sell": "出货",
}


class SectorFlowService:
    """
    板块资金流向服务

    核心职责：
    1. 计算主力暗盘、主力强度、行为判定
    2. 触发数据抓取并保存到数据库
    3. 提供排行榜、历史趋势、板块-基金联动查询
    """

    def __init__(self, db: Session):
        self.db = db
        self.crawler = SectorFlowCrawler()

    # ==================== 计算核心 ====================

    @staticmethod
    def calculate_dark_pool(main_net: Optional[float], retail_net: Optional[float]) -> Optional[float]:
        """
        计算主力暗盘

        公式：主力暗盘 = 主力净流入 − 散户净流入

        Args:
            main_net: 主力净流入（元）
            retail_net: 散户净流入（元）

        Returns:
            主力暗盘（元），输入为 None 则返回 None
        """
        if main_net is None or retail_net is None:
            return None
        return main_net - retail_net

    @staticmethod
    def calculate_intensity(dark_pool: Optional[float], turnover: Optional[float]) -> Optional[float]:
        """
        计算主力强度

        公式：主力强度 = (主力暗盘 / 板块成交额) × 100

        Args:
            dark_pool: 主力暗盘（亿元）
            turnover: 板块成交额（亿元）

        Returns:
            主力强度（%），输入为 None 或成交额为 0 则返回 None
        """
        if dark_pool is None or turnover is None or turnover == 0:
            return None
        # 暗盘和成交额单位都是亿元，直接计算
        return (dark_pool / turnover) * 100

    @staticmethod
    def judge_behavior(intensity: Optional[float]) -> Optional[str]:
        """
        根据主力强度判定主力行为

        规则：
        - intensity >= 3  → grab (抢筹)
        - 1 <= intensity < 3 → build (建仓)
        - -1 < intensity < 1 → wash (洗盘)
        - intensity <= -1 → sell (出货)

        Args:
            intensity: 主力强度（%）

        Returns:
            行为标识字符串，输入为 None 返回 None
        """
        if intensity is None:
            return None
        if intensity >= 3.0:
            return "grab"
        if intensity >= 1.0:
            return "build"
        if intensity > -1.0:
            return "wash"
        return "sell"

    def enrich(self, record: Dict) -> Dict:
        """
        对单条原始数据计算所有衍生指标

        Args:
            record: 原始数据字典，需包含 main_net_flow, retail_net_flow, turnover

        Returns:
            添加了 dark_pool, main_intensity, behavior 的数据字典
        """
        main_net = record.get("main_net_flow")
        retail_net = record.get("retail_net_flow")
        turnover = record.get("turnover")

        dark_pool = self.calculate_dark_pool(main_net, retail_net)
        intensity = self.calculate_intensity(dark_pool, turnover)
        behavior = self.judge_behavior(intensity)

        record["dark_pool"] = dark_pool
        record["main_intensity"] = intensity
        record["behavior"] = behavior
        return record

    # ==================== 数据操作 ====================

    def run_fetch(self, trigger: str, categories: Optional[List[str]] = None) -> Dict:
        """
        统一抓取入口。

        GitHub Actions、Render Cron 和手动接口都调用此方法，确保抓取、计算、保存和日志口径一致。
        """
        flow_date = date.today()
        target_categories = categories or ["industry", "concept"]
        run = self._start_run(trigger, flow_date, target_categories)
        raw_data: List[Dict] = []
        errors: List[str] = []

        for category in target_categories:
            try:
                items = self.crawler.fetch_sector_list(category)
                if items:
                    raw_data.extend(items)
                else:
                    errors.append(f"{category} 返回空数据")
            except Exception as e:
                logger.error(f"[SectorFlowService] {category} 抓取异常: {e}")
                errors.append(f"{category}: {e}")

        if not raw_data:
            message = "; ".join(errors) if errors else "抓取返回空数据"
            result = self._finish_run(run, "failed", 0, 0, message)
            logger.warning(f"[SectorFlowService] 抓取失败: {message}")
            return result

        saved_count = 0
        save_errors: List[str] = []
        for item in raw_data:
            try:
                enriched = self.enrich(item)
                self._upsert(enriched, flow_date)
                saved_count += 1
            except Exception as e:
                sector_name = item.get("sector_name") or item.get("name") or "未知板块"
                save_errors.append(f"{sector_name}: {e}")
                logger.warning(f"[SectorFlowService] 保存失败 {sector_name}: {e}")

        status = "success"
        error_message = None
        if errors or save_errors:
            status = "partial" if saved_count > 0 else "failed"
            error_message = "; ".join(errors + save_errors)

        try:
            result = self._finish_run(run, status, len(raw_data), saved_count, error_message)
            logger.info(f"[SectorFlowService] 抓取完成: status={status}, fetched={len(raw_data)}, saved={saved_count}")
            return result
        except Exception as e:
            self.db.rollback()
            logger.error(f"[SectorFlowService] 提交失败: {e}")
            run_result = {
                "success": False,
                "status": "failed",
                "trigger": trigger,
                "flow_date": flow_date.isoformat(),
                "fetched_count": len(raw_data),
                "saved_count": 0,
                "run_id": getattr(run, "id", None),
                "error_message": str(e),
            }
            return run_result

    def fetch_and_save(self, turnover_limit: int = 100) -> int:
        """兼容旧接口：触发统一抓取并返回保存数量。"""
        result = self.run_fetch(trigger="service_compat")
        return result.get("saved_count", 0) if result.get("success") else 0

    def _start_run(self, trigger: str, flow_date: date, categories: List[str]) -> SectorFlowFetchRun:
        """创建抓取运行日志。"""
        run = SectorFlowFetchRun(
            trigger=trigger,
            status="running",
            flow_date=flow_date,
            categories=",".join(categories),
            started_at=datetime.now(),
            fetched_count=0,
            saved_count=0,
            data_source="eastmoney",
        )
        self.db.add(run)
        self.db.flush()
        return run

    def _finish_run(
        self,
        run: SectorFlowFetchRun,
        status: str,
        fetched_count: int,
        saved_count: int,
        error_message: Optional[str] = None,
    ) -> Dict:
        """完成抓取运行日志并提交事务。"""
        run.status = status
        run.finished_at = datetime.now()
        run.fetched_count = fetched_count
        run.saved_count = saved_count
        run.error_message = error_message
        self.db.commit()
        return {
            "success": status in {"success", "partial"},
            "status": status,
            "trigger": run.trigger,
            "flow_date": run.flow_date.isoformat() if run.flow_date else None,
            "fetched_count": fetched_count,
            "saved_count": saved_count,
            "run_id": run.id,
            "error_message": error_message,
        }

    def _upsert(self, item: Dict, flow_date: date):
        """
        单条 upsert 操作

        同 (flow_date, sector_name) 则更新，否则插入
        """
        sector_name = item.get("sector_name", "")
        sector_code = item.get("sector_code", "")

        query = self.db.query(SectorFundFlow).filter(SectorFundFlow.flow_date == flow_date)
        data_category = item.get("data_category")
        if sector_code:
            existing = query.filter(
                and_(
                    SectorFundFlow.sector_code == sector_code,
                    SectorFundFlow.data_category == data_category,
                )
            ).first()
        else:
            existing = query.filter(
                and_(
                    SectorFundFlow.sector_name == sector_name,
                    SectorFundFlow.data_category == data_category,
                )
            ).first()

        if existing:
            # 更新已有记录
            self._update_record(existing, item)
        else:
            # 插入新记录
            new_record = self._create_record(item, flow_date)
            self.db.add(new_record)

    def _create_record(self, item: Dict, flow_date: date) -> SectorFundFlow:
        """从字典创建 ORM 对象"""
        return SectorFundFlow(
            flow_date=flow_date,
            sector_name=item.get("sector_name", ""),
            sector_code=item.get("sector_code", ""),
            main_net_flow=item.get("main_net_flow"),
            retail_net_flow=item.get("retail_net_flow"),
            turnover=item.get("turnover"),
            sector_change_pct=item.get("change_pct"),
            dark_pool=item.get("dark_pool"),
            main_intensity=item.get("main_intensity"),
            behavior=item.get("behavior"),
            data_category=item.get("data_category"),
            data_source="eastmoney",
        )

    def _update_record(self, record: SectorFundFlow, item: Dict):
        """更新 ORM 对象字段"""
        record.sector_code = item.get("sector_code", record.sector_code)
        record.main_net_flow = item.get("main_net_flow")
        record.retail_net_flow = item.get("retail_net_flow")
        record.turnover = item.get("turnover")
        record.sector_change_pct = item.get("change_pct")
        record.dark_pool = item.get("dark_pool")
        record.main_intensity = item.get("main_intensity")
        record.behavior = item.get("behavior")
        record.data_category = item.get("data_category")

    # ==================== 查询 ====================

    def get_ranking(
        self,
        sort_by: str = "intensity",
        query_date: Optional[date] = None,
        behavior: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """
        获取板块资金流向排行榜

        Args:
            sort_by: 排序方式 - "turnover"(成交额) | "intensity"(主力强度) | "change_pct"(涨跌幅)
            query_date: 查询日期，默认今天
            behavior: 行为过滤 - "grab"|"build"|"wash"|"sell"
            limit: 返回条数上限

        Returns:
            排序后的数据列表
        """
        if query_date is None:
            query_date = date.today()

        query = self.db.query(SectorFundFlow).filter(
            SectorFundFlow.flow_date == query_date
        )

        if behavior:
            query = query.filter(SectorFundFlow.behavior == behavior)

        # 排序（NULL值排到最后）
        if sort_by == "turnover":
            query = query.order_by(nulls_last(desc(SectorFundFlow.turnover)))
        elif sort_by == "intensity":
            query = query.order_by(nulls_last(desc(SectorFundFlow.main_intensity)))
        elif sort_by == "change_pct":
            query = query.order_by(nulls_last(desc(SectorFundFlow.sector_change_pct)))
        else:
            query = query.order_by(nulls_last(desc(SectorFundFlow.main_intensity)))

        records = query.limit(limit).all()
        return [self._record_to_dict(r) for r in records]

    def get_history(self, sector_name: str, days: int = 30) -> List[Dict]:
        """
        获取某板块最近 N 天的历史趋势

        Args:
            sector_name: 板块名称
            days: 天数

        Returns:
            按日期升序的历史数据列表
        """
        start_date = date.today() - timedelta(days=days)

        records = self.db.query(SectorFundFlow).filter(
            and_(
                SectorFundFlow.sector_name == sector_name,
                SectorFundFlow.flow_date >= start_date,
            )
        ).order_by(asc(SectorFundFlow.flow_date)).all()

        return [self._record_to_dict(r) for r in records]

    def get_fund_link(
        self,
        query_date: Optional[date] = None,
        behavior: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """
        板块-基金联动分析

        逻辑：
        1. 查询当日主力强度排序的板块
        2. 通过 SectorFundMapping 表关联基金
        3. 返回板块+关联基金列表

        Args:
            query_date: 查询日期，默认今天
            behavior: 行为过滤
            limit: 返回条数

        Returns:
            [{sector_name, sector_code, behavior, main_intensity, dark_pool, turnover, change_pct, funds: [{code, name, reviewed}]}]
        """
        if query_date is None:
            query_date = date.today()

        query = self.db.query(SectorFundFlow).filter(
            SectorFundFlow.flow_date == query_date
        )

        if behavior:
            query = query.filter(SectorFundFlow.behavior == behavior)

        records = query.order_by(nulls_last(desc(SectorFundFlow.main_intensity))).limit(limit).all()

        result = []
        for record in records:
            # 查询关联基金
            mappings = self.db.query(SectorFundMapping).filter(
                and_(
                    SectorFundMapping.sector_name == record.sector_name,
                    SectorFundMapping.is_active == True,
                )
            ).all()

            result.append({
                "sector_name": record.sector_name,
                "sector_code": record.sector_code,
                "behavior": record.behavior,
                "behavior_cn": BEHAVIOR_CN.get(record.behavior, ""),
                "main_intensity": record.main_intensity,
                "dark_pool": record.dark_pool,
                "turnover": record.turnover,
                "change_pct": record.sector_change_pct,
                "main_net_flow": record.main_net_flow,
                "retail_net_flow": record.retail_net_flow,
                "funds": [
                    {
                        "code": m.fund_code,
                        "name": m.fund_name,
                        "reviewed": m.reviewed,
                    }
                    for m in mappings
                ],
            })

        return result

    def get_stats(self, query_date: Optional[date] = None) -> Dict:
        """
        获取统计摘要

        Args:
            query_date: 查询日期，默认今天

        Returns:
            {grab: N, build: N, wash: N, sell: N, total: N}
        """
        if query_date is None:
            query_date = date.today()

        records = self.db.query(SectorFundFlow).filter(
            SectorFundFlow.flow_date == query_date
        ).all()

        stats = {"grab": 0, "build": 0, "wash": 0, "sell": 0, "total": len(records)}
        for r in records:
            if r.behavior and r.behavior in stats:
                stats[r.behavior] += 1

        return stats

    # ==================== 工具方法 ====================

    def _get_latest_data_date(self) -> Optional[date]:
        """获取最近一次有数据的日期"""
        from sqlalchemy import func
        result = self.db.query(func.max(SectorFundFlow.flow_date)).scalar()
        return result

    def get_fetch_status(self) -> Dict:
        """获取最近抓取状态和数据可用性。"""
        today = date.today()
        latest_run = self.db.query(SectorFlowFetchRun).order_by(
            desc(SectorFlowFetchRun.started_at),
            desc(SectorFlowFetchRun.id),
        ).first()
        latest_data_date = self._get_latest_data_date()
        today_data_count = self.db.query(SectorFundFlow).filter(
            SectorFundFlow.flow_date == today
        ).count()

        return {
            "latest_run": self._run_to_dict(latest_run) if latest_run else None,
            "latest_data_date": latest_data_date.isoformat() if latest_data_date else None,
            "today_data_count": today_data_count,
            "displaying_stale_data": bool(latest_data_date and latest_data_date != today),
        }

    @staticmethod
    def _run_to_dict(run: SectorFlowFetchRun) -> Dict:
        """抓取运行日志转字典。"""
        return {
            "id": run.id,
            "trigger": run.trigger,
            "status": run.status,
            "flow_date": run.flow_date.isoformat() if run.flow_date else None,
            "categories": run.categories,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "fetched_count": run.fetched_count,
            "saved_count": run.saved_count,
            "error_message": run.error_message,
            "data_source": run.data_source,
        }

    @staticmethod
    def _record_to_dict(record: SectorFundFlow) -> Dict:
        """ORM 对象转字典"""
        return {
            "id": record.id,
            "flow_date": record.flow_date.isoformat() if record.flow_date else None,
            "sector_name": record.sector_name,
            "sector_code": record.sector_code,
            "main_net_flow": record.main_net_flow,
            "retail_net_flow": record.retail_net_flow,
            "turnover": record.turnover,
            "sector_change_pct": record.sector_change_pct,
            "dark_pool": record.dark_pool,
            "main_intensity": record.main_intensity,
            "behavior": record.behavior,
            "behavior_cn": BEHAVIOR_CN.get(record.behavior, ""),
            "data_category": record.data_category,
            "fetched_at": record.fetched_at.isoformat() if record.fetched_at else None,
        }
