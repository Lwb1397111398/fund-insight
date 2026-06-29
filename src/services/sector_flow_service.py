"""
板块资金流向核心服务
负责：计算衍生指标、数据保存、排行榜查询、历史趋势、板块-基金联动

核心算法：
- 主力 = 超大单 + 大单 + 中单（f66 + f72 + f78）
- 散户 = 小单（f84）
- 主力暗盘 = 主力净流入 − 散户净流入
- 主力强度 = (主力暗盘 / 板块成交额) × 100
- 行为判定：≥3抢筹 / 1~3建仓 / -1~1洗盘 / ≤-1出货
"""
import logging
from typing import Dict, List, Optional
from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, asc, nulls_last

from src.models.database import SectorFundFlow, SectorFundMapping
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
            dark_pool: 主力暗盘（元）
            turnover: 板块成交额（亿元）

        Returns:
            主力强度（%），输入为 None 或成交额为 0 则返回 None
        """
        if dark_pool is None or turnover is None or turnover == 0:
            return None
        # 暗盘单位是元，成交额单位是亿元，需要统一
        # 暗盘 / (成交额 * 1e8) * 100 = 暗盘 / 成交额 / 1e8 * 100
        return (dark_pool / (turnover * 1e8)) * 100

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

    def fetch_and_save(self, turnover_limit: int = 100) -> int:
        """
        触发抓取 → 计算衍生指标 → 保存到数据库

        如果东方财富 API 不可用（502/503/504），会尝试使用最近一次缓存数据。

        Args:
            turnover_limit: 取前 N 个板块补充成交额（默认 200）

        Returns:
            保存/更新的记录数，失败返回 0
        """
        raw_data = None
        try:
            raw_data = self.crawler.fetch_all(turnover_limit=turnover_limit)
        except Exception as e:
            logger.error(f"[SectorFlowService] 抓取异常: {e}")

        if not raw_data:
            logger.warning("[SectorFlowService] 抓取返回空数据，尝试使用最近缓存")
            # 尝试使用最近的缓存数据
            latest_date = self._get_latest_data_date()
            if latest_date and latest_date != date.today():
                cached = self.db.query(SectorFundFlow).filter(
                    SectorFundFlow.flow_date == latest_date
                ).limit(limit).all()
                if cached:
                    logger.info(f"[SectorFlowService] 使用 {latest_date} 的缓存数据 ({len(cached)} 条)")
                    # 将缓存数据日期更新为今天
                    for r in cached:
                        r.flow_date = date.today()
                    try:
                        self.db.commit()
                        return len(cached)
                    except Exception as e:
                        self.db.rollback()
                        logger.error(f"[SectorFlowService] 缓存更新失败: {e}")
            return 0

        saved_count = 0
        flow_date = date.today()

        for item in raw_data:
            try:
                enriched = self.enrich(item)
                self._upsert(enriched, flow_date)
                saved_count += 1
            except Exception as e:
                logger.warning(f"[SectorFlowService] 保存失败 {item.get('sector_name')}: {e}")
                continue

        try:
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.error(f"[SectorFlowService] 提交失败: {e}")
            return 0

        logger.info(f"[SectorFlowService] 保存 {saved_count} 条记录")
        return saved_count

    def _upsert(self, item: Dict, flow_date: date):
        """
        单条 upsert 操作

        同 (flow_date, sector_name) 则更新，否则插入
        """
        sector_name = item.get("sector_name", "")
        sector_code = item.get("sector_code", "")

        existing = self.db.query(SectorFundFlow).filter(
            and_(
                SectorFundFlow.flow_date == flow_date,
                SectorFundFlow.sector_name == sector_name,
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
