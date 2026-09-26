"""
预测验证服务
支持所有预测周期的验证，包括超短期预测（1-3天）
支持过程验证和峰值验证
"""
import datetime as _dt
from datetime import date, timedelta, datetime
from typing import Dict, Optional, List, Tuple
from sqlalchemy.orm import Session, attributes
from sqlalchemy import and_
import logging

from src.models.database import Prediction, FundInfo, FundHistory, Blogger
from src.analyzer.llm_analyzer import get_analyzer
from src.fund.fund_api import FundAPI
from src.fund import backfill_proofs
from src.utils.prediction_utils import PERIOD_MAP, ULTRA_SHORT_PERIODS, parse_period_to_days
from src.analyzer.local_trend_analyzer import get_local_trend_analyzer
from src.core.config import config
from src.services.prediction_change_log_service import (
    add_prediction_change_log,
    snapshot_prediction,
)

logger = logging.getLogger(__name__)


def weekdays_between_exclusive(after_date, through_date) -> int:
    """(`after_date`, `through_date`] 里周一~周五有几天 —— 0 表示这段"本就不该有净值"。

    用来区分两种"端点早于目标日"：目标日是周六/周日而前一个交易日就是端点（日历可证，
    可以直接判），与中间还空着若干个工作日（可能只是本地缺行，见第 16 轮 BLOCKER-2）。
    法定节假日也算在内，所以这里只给"日历上应有几天"，剩下要靠证据判据去区分。
    """
    if after_date is None or through_date is None or through_date <= after_date:
        return 0
    count, day = 0, after_date + timedelta(days=1)
    while day <= through_date:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def has_verdict_trace(prediction) -> bool:
    """这行上**是否还挂着一条结论** —— 唯一判据，改标/撤结论的地方都问它。

    取的是保守的并集：结论文本、计数、状态、到期旗标任一项像"判过"就算。
    以前这个判断有两份（维护服务用 `verify_count>0 or status in (...) or is_expired`，
    retag 用 `is_correct is not None`），同一行在两处得到不同答案 ⇒
    "改标必清结论"就有漏网的一条（第 18 轮 M-2）。
    注意与 `verdict_evidence.has_verdict` 区分：那个问的是"有没有端点证据可核对"，
    这个问的是"要不要撤下来"。
    """
    return (getattr(prediction, 'is_correct', None) is not None
            or (getattr(prediction, 'verify_count', 0) or 0) > 0
            or getattr(prediction, 'status', None) in ('success', 'failed', 'verified')
            or bool(getattr(prediction, 'is_expired', False)))


def clear_verification_fields(prediction) -> None:
    """把一条预测退回"未验证"，字段清单只有这一处定义。

    `scripts/resync_verdict_scalars.py` 与还原清单都以本函数为准（少一个字段就会
    在还原之后留下"结论与台账打脸"的半成品）。
    共用同一份清单：`FundSyncManager.retag_prediction`（改标即清结论，**主写方**）、
    `rollback_invalid_verifications`（数据不再支撑结论）、
    `scripts/revert_degenerate_verdicts.py`（结论本身是退化/未来函数判出来的）。
    第 20 轮 MINOR-9：原来这里还虚指第三个 —— 维护服务的 `_reset_verification`
    早已退化成纯转发、没有任何生产调用点，已连同它的"两条路径等价"用例一起删掉。
    以前脚本靠调 service 的批量方法顺带清零，结果只能撤"今天已不可验"的行 ——
    用未来数据判出来、但今天仍可验的那批根本撤不掉（第 13 轮实测 94 条只撤了 1 条）。
    """
    prediction.status = 'pending'
    prediction.is_expired = False
    prediction.has_active_prediction = True
    prediction.verify_count = 0
    prediction.verify_score = None
    prediction.actual_change = None
    prediction.is_correct = None
    # AI 复核那一腿的判词也是结论的一部分：结论撤了还留着判词，等于让一条
    # status=pending 的预测挂着"预测下跌…方向判断正确"这种话（第 15 轮 m-2，
    # 与 `prediction_maintenance_service._reset_verification` 是同一份清单，
    # 现在那边直接调本函数）。
    prediction.ai_judgment = None
    prediction.current_nav = None
    prediction.current_nav_date = None
    prediction.end_nav = None
    prediction.end_nav_date = None
    prediction.start_nav = None
    prediction.start_nav_date = None
    prediction.verified_at = None
    prediction.last_verify_date = None


class PredictionVerifyService:
    """预测验证服务"""

    # 缓存最大条目数，防止内存溢出
    MAX_CACHE_SIZE = 10000

    def __init__(self, db: Session):
        self.db = db
        self._llm_analyzer = None
        self.fund_api = FundAPI()
        # 实例级净值缓存，用于批量验证时避免 N+1 查询
        # 结构: {(fund_code, date_str): nav, '_history': {fund_code: [FundHistory,...]}}
        # 注意：使用 LRU 机制限制大小，防止内存溢出
        self._nav_cache: Dict = {}
        self._cache_order: list = []  # 记录缓存插入顺序，用于 LRU 淘汰

    @property
    def llm_analyzer(self):
        """只在边界评分需要 AI 辅助时初始化 LLM 客户端。"""
        if self._llm_analyzer is None:
            self._llm_analyzer = get_analyzer()
        return self._llm_analyzer
    
    def get_verify_config(self, period_days: int) -> Dict:
        """
        根据预测周期获取验证配置

        Args:
            period_days: 预测周期天数

        Returns:
            验证配置字典
        """
        flat_short = config.VERIFY_FLAT_THRESHOLD_SHORT
        flat_medium = config.VERIFY_FLAT_THRESHOLD_MEDIUM
        flat_long = config.VERIFY_FLAT_THRESHOLD_LONG

        if period_days <= 1:
            return {
                'window_days_before': 0,
                'window_days_after': 1,
                'nav_start_days': 0,
                'flat_threshold': flat_short,
                'is_ultra_short': True
            }
        elif period_days <= 3:
            return {
                'window_days_before': 1,
                'window_days_after': period_days,
                'nav_start_days': 1,
                'flat_threshold': flat_short * 1.6,
                'is_ultra_short': True
            }
        elif period_days <= 14:
            return {
                'window_days_before': 3,
                'window_days_after': period_days,
                'nav_start_days': 2,
                'flat_threshold': flat_medium,
                'is_ultra_short': False
            }
        elif period_days <= 30:
            return {
                'window_days_before': 4,
                'window_days_after': period_days,
                'nav_start_days': 3,
                'flat_threshold': flat_medium * 1.5,
                'is_ultra_short': False
            }
        elif period_days <= 90:
            return {
                'window_days_before': 5,
                'window_days_after': period_days,
                'nav_start_days': 4,
                'flat_threshold': flat_long,
                'is_ultra_short': False
            }
        else:
            return {
                'window_days_before': 6,
                'window_days_after': period_days,
                'nav_start_days': 5,
                'flat_threshold': flat_long * 1.5,
                'is_ultra_short': False
            }
    
    def parse_period_days(self, period_str: str) -> int:
        return parse_period_to_days(period_str)
    
    def _add_to_cache(self, key, value):
        """
        添加条目到缓存，使用 LRU 淘汰策略

        Args:
            key: 缓存键
            value: 缓存值
        """
        # 如果 key 已存在，先删除旧的顺序记录
        if key in self._nav_cache:
            self._cache_order.remove(key)
        # 如果缓存已满，淘汰最早的条目
        elif len(self._cache_order) >= self.MAX_CACHE_SIZE:
            oldest_key = self._cache_order.pop(0)
            del self._nav_cache[oldest_key]

        # 添加新条目
        self._nav_cache[key] = value
        self._cache_order.append(key)

    def _invalidate_fund_cache(self, fund_code: str):
        """失效某只基金的全部净值缓存（历史补拉入库后必须调用）。

        缓存分两部分：
        1. '_history' 预热缓存（批量验证开始时一次性装载）
        2. (fund_code, date_str, strict) -> nav 的单点缓存（LRU）
        补拉新数据后若不清理，验证会继续读到"数据不足/无净值"的旧结论。
        """
        history_cache = self._nav_cache.get('_history')
        if isinstance(history_cache, dict):
            history_cache.pop(fund_code, None)

        stale_keys = [
            key for key in self._cache_order
            if isinstance(key, tuple) and key and key[0] == fund_code
        ]
        for key in stale_keys:
            self._nav_cache.pop(key, None)
            try:
                self._cache_order.remove(key)
            except ValueError:
                pass

    @staticmethod
    def _parse_api_nav_date(raw_value) -> Optional[date]:
        """解析外部 API 返回的净值日期，失败时返回 None。

        使用 datetime 模块真实类型，避免测试 monkeypatch 模块内 date 名称后误判。
        """
        if raw_value is None:
            return None
        if isinstance(raw_value, _dt.datetime):
            return raw_value.date()
        if isinstance(raw_value, _dt.date):
            return raw_value
        if isinstance(raw_value, str):
            text = raw_value.strip()
            if not text:
                return None
            try:
                return _dt.datetime.strptime(text[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
        return None

    def get_nav_by_date(
        self,
        fund_code: str,
        target_date: date,
        strict_as_of: bool = False,
    ):
        """获取指定日期的基金净值（优先读缓存）。

        Args:
            fund_code: 基金代码
            target_date: 请求的净值日期
            strict_as_of: 为 True 时，API fallback 也必须满足净值日期 <= target_date；
                日期缺失、无法解析或晚于请求日时返回 None，避免把“当前最新净值”
                当作历史日期净值使用。
        """
        cache_key = (fund_code, target_date.isoformat(), bool(strict_as_of))
        if cache_key in self._nav_cache:
            # 更新 LRU 顺序
            self._cache_order.remove(cache_key)
            self._cache_order.append(cache_key)
            return self._nav_cache[cache_key]

        nav_record = self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date <= target_date
        ).order_by(FundHistory.nav_date.desc()).first()

        if nav_record:
            self._add_to_cache(cache_key, nav_record.nav)
            return nav_record.nav

        fund_info = self.fund_api.get_fund_info(fund_code)
        if fund_info:
            nav = fund_info.get('nav')
            if nav is None:
                self._add_to_cache(cache_key, None)
                return None

            if strict_as_of:
                api_nav_date = self._parse_api_nav_date(fund_info.get('nav_date'))
                if api_nav_date is None or api_nav_date > target_date:
                    logger.info(
                        f"[Verify] 严格 as-of 拒绝 API 净值: fund={fund_code}, "
                        f"request_date={target_date.isoformat()}, "
                        f"api_nav_date={api_nav_date}"
                    )
                    self._add_to_cache(cache_key, None)
                    return None

            self._add_to_cache(cache_key, nav)
            return nav

        self._add_to_cache(cache_key, None)
        return None
    
    def get_nav_history(self, fund_code: str, start_date: date, end_date: date) -> List[Dict]:
        """
        获取净值历史数据（优先读缓存）

        Args:
            fund_code: 基金代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            净值历史列表 [{date, nav}, ...]
        """
        # 不读 `_history` 缓存：那个切片只有 [今天-120d, 今天+14d]，窗口**部分命中**时
        # 这里会返回"非空但残缺"的序列 —— 而它是 `calculate_process_metrics` 的输入，
        # `peak_hit_ratio` / `daily_direction_hit_ratio` 会直接决定 `is_correct`
        # ⇒ 同一条预测在 Cron 与手动按钮下能判出相反结论（第 12 轮 MAJOR-6）。
        # 点数（`_check_fund_data_availability`）已经全走 DB，这里也必须同源才可比。
        records = self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date >= start_date,
            FundHistory.nav_date <= end_date
        ).order_by(FundHistory.nav_date.asc()).all()

        if records:
            return [{"date": r.nav_date, "nav": r.nav} for r in records]

        return []
    
    @staticmethod
    def _as_date(value) -> Optional[date]:
        """统一把 date/datetime 转成 date，便于自然日比较。

        使用 datetime 模块真实类型，避免测试 monkeypatch 模块内 date 名称后误判。
        """
        if value is None:
            return None
        if isinstance(value, _dt.datetime):
            return value.date()
        if isinstance(value, _dt.date):
            return value
        return None

    def _real_nav_date(self, fund_code: str, day: date) -> Optional[date]:
        """该基金**实际**有净值的那一天：`<= day` 的最近一条，取不到返回 None。

        为什么要单独问一遍：`get_nav_by_date` 只回数值不回日期，而目标日落在休市日时它会
        悄悄回退到目标日之前那条 —— 于是 `end_nav_date` 被写成请求的周末日，库里看起来
        "已按目标日验证"，其实用的是周五那条净值。S7-2 的退化终点判据也依赖真实的两端日期。
        缓存窗口只有 ±120 天，所以缓存里查不到时要回落到 DB（否则会把"有历史"看成"没有"）。
        """
        if not fund_code or day is None:
            return None
        cached = self._nav_cache.get('_history', {}).get(fund_code) or []
        # 缓存条目是 `(nav_date, nav)` 元组（见 `_warm_cache` 的 MAJOR-2 说明）
        dates = [self._as_date(item[0]) for item in cached if item]
        dates = [d for d in dates if d is not None and d <= day]
        if dates:
            return max(dates)
        row = self.db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date <= day,
        ).order_by(FundHistory.nav_date.desc()).first()
        return self._as_date(row[0]) if row else None

    def _endpoint_dates(self, fund_code: str, nav_start_date: date,
                        window_end: date) -> tuple:
        """起点/终点各自**实际**取到的净值日（任一取不到时返回 None，交给后续分支）。"""
        return (self._real_nav_date(fund_code, nav_start_date),
                self._real_nav_date(fund_code, window_end))

    def _check_fund_data_availability(
        self,
        fund_code: str,
        nav_start_date: date,
        window_end: date,
        min_data_points: int = None,
        today: date = None,
        target_date: date = None,
        skip_wait: bool = False,
        data_wait_days: int = None,
        max_end_nav_age_days: int = None,
    ) -> Dict:
        """
        检查基金数据是否充足以进行验证（优先读缓存）。

        成功条件：
        1. 区间 [nav_start_date, window_end] 内数据点 >= min_data_points
        2. 区间内最新净值日期 latest_nav_date 相对 target/window_end 的年龄合规
           - 已有目标日当天净值：立即就绪
           - 周末目标日：可用最近前值，但年龄不得超过 max_end_nav_age_days
           - 工作日缺当天净值：data_wait_days 内等待；超时后可用年龄合规前值
           - 年龄超过 max_end_nav_age_days：拒绝（force 也不能跳过）

        Args:
            fund_code: 基金代码
            nav_start_date: 净值起始日期
            window_end: 验证窗口结束日期（应等于 target_date）
            min_data_points: 最少数据点数
            today: 当前自然日（默认 date.today()）
            target_date: 预测目标日（默认 window_end）
            skip_wait: True 时跳过工作日等待期（force 可触发重试），但仍受最大陈旧期限约束
            data_wait_days: 工作日等待自然日数
            max_end_nav_age_days: 结束净值最大允许陈旧自然日数
        """
        if min_data_points is None:
            min_data_points = config.VERIFY_MIN_DATA_POINTS
        if data_wait_days is None:
            data_wait_days = config.VERIFY_DATA_WAIT_DAYS
        if max_end_nav_age_days is None:
            max_end_nav_age_days = config.VERIFY_MAX_END_NAV_AGE_DAYS

        nav_start_date = self._as_date(nav_start_date)
        window_end = self._as_date(window_end)
        today = self._as_date(today) or date.today()
        target_date = self._as_date(target_date) or window_end

        # 窗口内的净值点数**一律走 DB**，不吃 `_history` 缓存：缓存只装
        # [今天-120d, 今天+14d]，命中但**只盖住窗口一半**时会算出偏小的点数
        # —— 同一条预测"手动验得过、Cron 里永远数据不足"（第 11 轮 M-D）。
        # 第 10 轮我只补了"过滤后为空才回落"，那只堵了一半；这里干脆全走 DB：
        # `data_points` 只是一个比较用的标量，走缓存省不下多少，却把偏差留在判据里。
        records = self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date >= nav_start_date,
            FundHistory.nav_date <= window_end
        ).order_by(FundHistory.nav_date.asc()).all()

        data_points = len(records)
        latest_date = self._as_date(records[-1].nav_date) if records else None

        def _fail(message: str, reason: str, **extra) -> Dict:
            payload = {
                'available': False,
                'message': message,
                'data_points': data_points,
                'latest_date': latest_date,
                'reason': reason,
            }
            payload.update(extra)
            return payload

        if data_points < min_data_points:
            # 点数不足。缺的可能是三种东西，按"最具体"的顺序判：退化终点 → 问过且源端没有 → 单纯不够。
            # （`backfill_proofs` 现在从模块顶层 import：函数内 import 会让这个名字变成本地变量，
            #  端点证据那一支先用到它就会 UnboundLocalError —— 第 16 轮 BLOCKER-2 的新代码踩到过）
            # (1) 退化终点：窗口里只有起点那一天 ⇒ 起点与终点是同一条净值、涨跌幅恒为 0，
            # 那不是"验过了"而是"没有信息"（1709 实测：起 07-10 周五、目标 07-11 周六）。
            # 必须同时满足"目标日之后已有净值"才这么判 —— 那才证明目标日真的是休市日；
            # 否则只是"目标日净值还没发布"（每个工作日 1 天期预测的常态），明天自愈，
            # 不能写成永久不可验、更不能建议把目标日往后挪（那是取目标日之后的行情，违反防未来函数）。
            start_real, end_real = self._endpoint_dates(fund_code, nav_start_date, window_end)
            nav_after_target = self.db.query(FundHistory.nav_date).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date > window_end).order_by(
                FundHistory.nav_date.asc()).first()
            # `today` 必须一路传到底：TTL 分档（窗口终点距今 <30 天 ⇒ 只信 1 天）与
            # "未来时间戳"防线都按天算，历史回放/固定日期用例里不传就会拿墙上时钟
            # 判出一个"当时并不存在的宽限"（第 15 轮 m-3；上一轮只修到 covering_probe 那层）。
            proven_empty = backfill_proofs.fresh(self.db, fund_code, nav_start_date,
                                                 window_end, today=today)
            # 两条合法证据，都必须建立在"问过"之上（第 12 轮 MAJOR-3：不能拿"到期很久了"
            # 这种纯日历推断替代凭据，那等于把 S7-b 的硬约束从后门放掉）：
            # ① 目标日之后已有净值 ⇒ 目标日确实是休市日；
            # ② 已按区间问过数据源、它给不出足够净值 ⇒ 再等也不会有。
            permanently_degenerate = (nav_after_target is not None
                                      or proven_empty is not None)
            if (data_points >= 1 and permanently_degenerate
                    and start_real is not None and end_real is not None
                    and end_real <= start_real):
                return _fail(
                    f"目标日 {window_end} 及之前只有 {start_real} 这一条净值，起点与终点是同一条 ⇒ "
                    f"涨跌幅必然为 0，无法判定方向"
                    + (f"（本地库里该基金在 {window_end} 之后已有净值 ⇒ 目标日那天没有独立净值行）"
                       if nav_after_target is not None
                       else f"（已按区间问过数据源：{backfill_proofs.describe(proven_empty)}）")
                    + f"。本系统按防未来函数策略不取目标日之后的行情；"
                      f"要判这条只能在录入时把目标日落到交易日，历史目标日不自动改",
                    reason='same_nav_endpoint',
                    start_nav_date=start_real,
                    end_nav_date=end_real,
                )

            # (2) 问过数据源且它给不出这段 ⇒ 结构性不可验，提示语里带凭据原文。
            if proven_empty:
                proof_text = backfill_proofs.describe(proven_empty)
                if (proven_empty.get('source_rows') or 0) > 0:
                    # 源端给过几条、但本地窗口仍凑不够：措辞不能写成"拿不到"（第 11 轮 MINOR-5）
                    tail = ('已按区间向数据源要过（%s），仍凑不够 ⇒ 需按区间重放补拉本地历史'
                            % proof_text)
                else:
                    tail = ('%s ⇒ 属**结构性不可验**，补拉最新数据不会改变结论。'
                            '出口只有两个：人工处置这条预测，或等这段历史被补录后自动重验'
                            % proof_text)
                return _fail(
                    f"目标日附近这段历史净值不足（窗口 [{nav_start_date} ~ {window_end}] 内 "
                    f"{data_points} 条，需 {min_data_points} 条）：" + tail,
                    reason='no_source_history',
                    source_proof=proven_empty,
                )

            # (3) 条数不够：分清"数据太旧该更新"与"缺目标日附近那段历史"。
            # 全局最新那条也走 DB（同上，缓存切片不能代表"这只基金最新到哪一天"）。
            latest_record = self.db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code
            ).order_by(FundHistory.nav_date.desc()).first()

            if latest_record:
                latest_date = self._as_date(latest_record.nav_date)
                days_behind = (window_end - latest_date).days if latest_date else None
                if days_behind is not None and days_behind < 0:
                    # 本地最新净值**晚于**窗口终点（实测 515440/158038）：缺的是目标日附近那段
                    # 历史，不是"数据没更新"。这时候说"请更新基金数据"会把人带去跑同步，
                    # 而同步只补最近端，永远补不到这个窗口。
                    return _fail(
                        f"目标日附近缺历史净值：窗口 [{nav_start_date} ~ {window_end}] 内只有 "
                        f"{data_points} 条（需 {min_data_points} 条），"
                        f"本地最新净值 {latest_date} 已晚于窗口终点 {-days_behind} 天，"
                        f"补拉最新数据补不到这个窗口，需按区间回补历史或改判不可验",
                        reason='insufficient_points',
                        days_behind=days_behind,
                    )
                return _fail(
                    f"基金数据不足，最新数据为 {latest_date}，落后 {days_behind} 天，请更新基金数据后再验证",
                    reason='insufficient_points',
                    days_behind=days_behind,
                )
            return _fail(
                f"基金 {fund_code} 无历史数据，请先更新基金数据",
                reason='no_history',
            )

        # 数据点足够后，再检查结束净值是否“就绪且不过旧”
        end_nav_age_days = (target_date - latest_date).days if latest_date else None
        days_since_target = (today - target_date).days

        if latest_date == target_date:
            return {
                'available': True,
                'message': f"数据充足，共 {data_points} 个数据点，已含目标日净值",
                'data_points': data_points,
                'latest_date': latest_date,
                'end_nav_age_days': 0,
                'reason': 'exact_target',
            }

        if end_nav_age_days is not None and end_nav_age_days > max_end_nav_age_days:
            return _fail(
                (
                    f"目标日期前最近净值已超过允许陈旧期限"
                    f"（最新 {latest_date}，距目标日 {end_nav_age_days} 天，"
                    f"上限 {max_end_nav_age_days} 天）"
                ),
                reason='end_nav_too_old',
                end_nav_age_days=end_nav_age_days,
                max_end_nav_age_days=max_end_nav_age_days,
            )

        # 端点落在目标日**之前**时，先证明"目标日那几天确实没有净值可取"，再允许下终局结论。
        # 为什么这是硬门（第 16 轮 BLOCKER-2）：`is_correct` 一旦非空就永不重判
        # （`filter_due_for_verify` 只捞 NULL），拿一条早于目标日的净值把结论落死，
        # 等于把"本地缺那一行"当成"市场上那一天没有净值"。实测镜像里 4 条已判死的行
        # （3267/3283/3304/3334，端点比目标日早 2~4 个工作日、中间都是工作日、
        # 库里也没有目标日之后的行），其中 515070 那条按前值判"错 0 分"，
        # 而数据源其实有目标日净值 —— 现取回来后判对 100 分。
        # 三条合法证据，与"点数不足"那一支同源（第 12 轮 MAJOR-3 不许纯日历推断）：
        #   ① 端点到目标日之间全是周末 ⇒ 日历上本就不该有净值（周六目标日用周五端点）；
        #   ② 库里已有目标日之后的净值 ⇒ 那些空着的工作日确实是法定节假日；
        #   ③ 已按区间问过数据源并留下未过期凭据。
        # 第 17 轮 BLOCKER-1（两份复评同一发现）：这道门原先只挡在 `waited_previous` 之前，
        # `weekend_previous` 在它就整块绕过 —— 于是"目标日周六、端点停在周三"（周四周五
        # 本地缺行）仍然纯日历判死。内存库对照实测：同一份稠密数据，目标 09-19(六) 放行、
        # 09-18(五) 拒判。周末目标日靠证据①天然免证（周五端点 gap=0），不会因此误伤。
        gap_weekdays = weekdays_between_exclusive(latest_date, target_date)
        lag_evidence_missing = False
        if gap_weekdays:
            after_target_row = self.db.query(FundHistory.nav_date).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date > target_date).order_by(
                FundHistory.nav_date.asc()).first()
            endpoint_proof = backfill_proofs.fresh(self.db, fund_code, nav_start_date,
                                                   window_end, today=today)
            # 证据②单用会放过"本地缺行"（第 18 轮 MAJOR-1）：这只基金在目标日之后
            # 有行，只说明它**后来**有净值，不证明中间那几天休市。实测镜像 218 只基金
            # 里 169 只在自身序列中间有空洞（合计 2984 个缺失工作日）；活体两例是
            # 158038 只有 09-07 与 09-11，而 09-08/09-09 分别有 175/176 只**别的基金**
            # 有净值 ⇒ 市场开门，是我们本地缺行。所以要加一条跨基金核验：
            # 缺口里只要有任何一天"全市场有行、我们没行"，②就不成立。
            holiday_confirmed = after_target_row is not None
            if holiday_confirmed:
                open_days = {self._as_date(r[0]) for r in self.db.query(
                    FundHistory.nav_date).filter(
                    FundHistory.nav_date > latest_date,
                    FundHistory.nav_date <= target_date).distinct().all()}
                probe = latest_date
                while probe < target_date:
                    probe += timedelta(days=1)
                    if probe.weekday() < 5 and probe in open_days:
                        holiday_confirmed = False
                        break
            lag_evidence_missing = not (holiday_confirmed or endpoint_proof is not None)

        def _refuse_lag() -> Dict:
            return _fail(
                f"端点 {latest_date} 早于目标日 {target_date}，中间还有 {gap_weekdays} 个"
                f"工作日没有净值行；那几天既没有\u300c全市场都没有净值\u300d的证据"
                f"（别的基金当天有净值 ⇒ 市场开门，是我们本地缺行），"
                f"也没有「已按区间问过数据源」的凭据 ⇒ 无法区分「市场没有」与"
                f"「本地缺行」，不能拿这条前值下终局结论。"
                f"先按区间回补该基金历史或更新基金数据，到位后会自动重验",
                reason='endpoint_lag_unproven',
                end_nav_date=latest_date,
                gap_weekdays=gap_weekdays,
            )

        is_weekend_target = target_date.weekday() >= 5
        if is_weekend_target:
            if lag_evidence_missing:
                return _refuse_lag()
            return {
                'available': True,
                'message': (
                    f"目标日为周末，使用此前最近净值 {latest_date}"
                    f"（距目标日 {end_nav_age_days} 天）"
                ),
                'data_points': data_points,
                'latest_date': latest_date,
                'end_nav_age_days': end_nav_age_days,
                'reason': 'weekend_previous',
            }

        # 工作日（含无法识别的法定节假日）缺少目标日当天净值
        if not skip_wait and days_since_target < data_wait_days:
            return _fail(
                (
                    f"等待目标日期净值更新"
                    f"（目标日 {target_date}，最新 {latest_date}，"
                    f"已过 {days_since_target} 天，等待期 {data_wait_days} 个自然日）"
                ),
                reason='waiting_target_nav',
                days_since_target=days_since_target,
                data_wait_days=data_wait_days,
                end_nav_age_days=end_nav_age_days,
            )

        if lag_evidence_missing:
            return _refuse_lag()
        return {
            'available': True,
            'message': (
                f"等待期已过，使用目标日前最近净值 {latest_date}"
                f"（距目标日 {end_nav_age_days} 天）"
            ),
            'data_points': data_points,
            'latest_date': latest_date,
            'end_nav_age_days': end_nav_age_days,
            'reason': 'waited_previous',
        }
    
    def calculate_process_metrics(
        self, 
        nav_history: List[Dict], 
        start_nav: float,
        prediction_type: str,
        flat_threshold: float = 1.0
    ) -> Dict:
        """
        计算过程指标
        
        Args:
            nav_history: 净值历史
            start_nav: 起始净值
            prediction_type: 预测类型
            flat_threshold: 震荡阈值（动态，根据预测周期调整）
            
        Returns:
            过程指标字典
        """
        if not nav_history or not start_nav:
            return {"data_sufficient": False}
        
        changes = []
        for record in nav_history:
            change = (record["nav"] - start_nav) / start_nav * 100
            changes.append({
                "date": record["date"],
                "nav": record["nav"],
                "change": change
            })
        
        if not changes:
            return {"data_sufficient": False}
        
        max_change = max(c["change"] for c in changes)
        min_change = min(c["change"] for c in changes)
        final_change = changes[-1]["change"]
        
        max_record = max(changes, key=lambda x: x["change"])
        min_record = min(changes, key=lambda x: x["change"])
        
        peak_date = max_record["date"]
        peak_nav = max_record["nav"]
        trough_date = min_record["date"]
        trough_nav = min_record["nav"]
        
        daily_changes = []
        previous_nav = float(start_nav)
        direction_records = nav_history
        if direction_records and abs(float(direction_records[0]["nav"]) - previous_nav) < 1e-9:
            direction_records = direction_records[1:]

        for record in direction_records:
            nav = float(record["nav"])
            if previous_nav:
                daily_changes.append((nav - previous_nav) / previous_nav * 100)
            previous_nav = nav

        if prediction_type == 'up':
            peak_hit = max_change > 0
            peak_hit_days = sum(1 for c in changes if c["change"] > 0)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
            daily_direction_hit_days = sum(1 for change in daily_changes if change > 0)
        elif prediction_type == 'down':
            peak_hit = min_change < 0
            peak_hit_days = sum(1 for c in changes if c["change"] < 0)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
            daily_direction_hit_days = sum(1 for change in daily_changes if change < 0)
        else:
            peak_hit = abs(max_change) < flat_threshold and abs(min_change) < flat_threshold
            peak_hit_days = sum(1 for c in changes if abs(c["change"]) < flat_threshold)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
            daily_direction_hit_days = sum(1 for change in daily_changes if abs(change) < flat_threshold)

        daily_direction_hit_ratio = (
            daily_direction_hit_days / len(daily_changes) if daily_changes else 0
        )
        
        max_drawdown = 0
        if max_change > 0:
            peak_nav_val = start_nav * (1 + max_change / 100)
            if final_change < max_change:
                max_drawdown = max_change - final_change
        
        return {
            "data_sufficient": True,
            "max_change": round(max_change, 2),
            "min_change": round(min_change, 2),
            "final_change": round(final_change, 2),
            "peak_date": peak_date.isoformat() if isinstance(peak_date, _dt.date) else peak_date,
            "peak_nav": peak_nav,
            "trough_date": trough_date.isoformat() if isinstance(trough_date, _dt.date) else trough_date,
            "trough_nav": trough_nav,
            "peak_hit": peak_hit,
            "peak_hit_days": peak_hit_days,
            "peak_hit_ratio": round(peak_hit_ratio, 2),
            "daily_direction_hit_days": daily_direction_hit_days,
            "daily_direction_total_days": len(daily_changes),
            "daily_direction_hit_ratio": round(daily_direction_hit_ratio, 2),
            "max_drawdown": round(max_drawdown, 2),
            "total_days": len(changes)
        }
    
    def comprehensive_verify(
        self,
        prediction_type: str,
        final_change: float,
        process_metrics: Dict,
        flat_threshold: float = 1.0
    ) -> Dict:
        """
        综合验证判断
        
        Args:
            prediction_type: 预测类型
            final_change: 最终涨跌幅
            process_metrics: 过程指标
            flat_threshold: 震荡阈值
            
        Returns:
            验证结果
        """
        data_sufficient = process_metrics.get("data_sufficient", True)
        
        if not data_sufficient:
            final_correct = False
            if prediction_type == 'up':
                final_correct = final_change > 0
            elif prediction_type == 'down':
                final_correct = final_change < 0
            else:
                final_correct = abs(final_change) < flat_threshold
            
            return {
                "is_correct": final_correct,
                "verify_type": "simple",
                "score": 100 if final_correct else 0,
                "analysis": f"历史数据不足，仅验证最终结果：涨跌{final_change:+.2f}%，{'预测正确' if final_correct else '预测错误'}"
            }
        
        max_change = process_metrics.get("max_change", 0)
        min_change = process_metrics.get("min_change", 0)
        peak_hit = process_metrics.get("peak_hit", False)
        peak_hit_ratio = process_metrics.get("peak_hit_ratio", 0)
        max_drawdown = process_metrics.get("max_drawdown", 0)
        
        final_correct = False
        peak_correct = False
        
        if prediction_type == 'up':
            final_correct = final_change > 0
            peak_correct = max_change > 0
        elif prediction_type == 'down':
            final_correct = final_change < 0
            peak_correct = min_change < 0
        else:
            final_correct = abs(final_change) < flat_threshold
            peak_correct = peak_hit_ratio >= 0.5
        
        if final_correct:
            return {
                "is_correct": True,
                "verify_type": "final",
                "score": 100,
                "analysis": f"最终涨跌{final_change:+.2f}%，预测正确"
            }
        
        if peak_correct and peak_hit_ratio >= 0.5:
            score = int(60 + peak_hit_ratio * 30)
            return {
                "is_correct": True,
                "verify_type": "process",
                "score": min(score, 90),
                "analysis": f"过程中{int(peak_hit_ratio*100)}%交易日相对起点方向正确，判定过程正确"
            }
        
        if peak_hit_ratio >= 0.3:
            score = int(40 + peak_hit_ratio * 30)
            return {
                "is_correct": False,
                "verify_type": "partial",
                "score": min(score, 60),
                "analysis": f"过程中{int(peak_hit_ratio*100)}%交易日相对起点方向正确，判定部分正确"
            }
        
        return {
            "is_correct": False,
            "verify_type": "failed",
            "score": 0,
            "analysis": f"预测方向错误，最终涨跌{final_change:+.2f}%"
        }
    
    def fund_code_is_servable(self, fund_code: str) -> bool:
        """这个代码有没有被身份体检判成"不可服务"—— 只认体检的结论，不另起一套判据。

        规则刻意保守：**没有任何映射行提到这个代码时返回 True**（体检对这只基金没有意见，
        不该因此停掉验证）。只有"所有带这个代码的映射行都被判不可服务"才算数，
        避免一条不相关的行把正常的历史预测卡死。
        """
        if not fund_code:
            return True
        from src.models.database import SectorFundMapping
        from src.services.sector_fund_service import SectorFundService

        rows = self.db.query(SectorFundMapping).filter(
            SectorFundMapping.fund_code == fund_code,
            SectorFundMapping.is_active == True            # noqa: E712
        ).all()
        if not rows:
            return True
        return not all(SectorFundService.is_unservable(row) for row in rows)

    def match_fund_for_prediction(self, prediction: Prediction) -> Tuple:
        """为预测匹配基金

        匹配优先级：
        1. prediction 自带 fund_code
        2. prediction 自带 fund_name 查 FundInfo
        3. sector 查 SectorFundMapping 表（与创建时共享映射源）
        4. sector 查 FundInfo 精确匹配
        5. sector 查 FundInfo 模糊匹配
        """
        if prediction.fund_code:
            # 自带代码不是免检通道（第 17 轮 MAJOR-2）：本函数下面三条分支都过身份体检，
            # 只有这一条直接 return。板块映射行被填成另一只真基金、后来被体检判不可服务时，
            # 新预测不再命中它，但**已入库的预测仍按这个代码验证** ⇒ 结论挂到错标的上
            # （`scripts/audit_verdict_evidence.py` 里 `verdict_under_other_fund` 那一族的成因之一）。
            if self.fund_code_is_servable(prediction.fund_code):
                return prediction.fund_code, prediction.fund_name
            logger.warning(
                '[Verify] 预测 %s 自带代码 %s 被身份体检判为不可服务，改按板块重新解析标的',
                prediction.id, prediction.fund_code)

        if prediction.fund_name:
            fund = self.db.query(FundInfo).filter(
                FundInfo.fund_name == prediction.fund_name
            ).first()
            if fund:
                # 第 20 轮 MAJOR-1：这一步以前**不过体检**。名字是老板/LLM 从帖子里抄来的，
                # 而 `fund_info` 里"名字→代码"通常正好指回自带的那只不可服务基金 ⇒
                # 第 1 步刚说"改按板块重新解析标的"，这里立刻把同一个代码原样返回：
                # 不改标、不清结论、不打 ⚠，新加的改标门 86% 的行都到不了（镜像实测）。
                if self.fund_code_is_servable(fund.fund_code):
                    return fund.fund_code, fund.fund_name
                logger.warning('[Verify] 预测 %s 按名字解析回不可服务的 %s，继续按板块解析',
                               prediction.id, fund.fund_code)

        sector = prediction.sector or prediction.sector_type
        if sector:
            excluded_keywords = ['债券', '债', '货币', '理财', '短债', '纯债', '利率债', '信用债']

            # 第3步：查 SectorFundMapping 表（与 LLM 分析器共享同一映射源）
            from src.models.database import SectorFundMapping
            from src.constants.sector_fund_map import normalize_sector_name
            from src.services.sector_identity_audit import servable_predicate as _servable_predicate
            standard_sector = normalize_sector_name(sector)
            candidates = self.db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == standard_sector,
                SectorFundMapping.is_active == True,          # noqa: E712
                _servable_predicate(),                        # SQL 粗筛（便宜，只看列）
            ).order_by(SectorFundMapping.reviewed.desc().nulls_last(),   # 已审查优先
                       SectorFundMapping.id.asc()).all()
            # 粗筛之后必须再过一次**唯一判据**：`servable_predicate()` 把 `is_fetchable IS
            # NULL` 一律当可服务，而"不可服务"还有第二个事实源（`evidence.identity.verdict`）
            # ⇒ 不过一遍的话，"改标的去处"可能正是那只被 verdict 否掉的代码（第 22 轮 MAJOR-2）。
            from src.services.sector_identity_audit import row_unservable as _row_unservable
            for mapping in candidates:
                if not mapping.fund_code or _row_unservable(mapping):
                    continue
                fund_name = mapping.fund_name or ''
                if not any(kw in fund_name for kw in excluded_keywords):
                    return mapping.fund_code, fund_name

            # 第4步：硬编码表——同样要过体检拒绝集，否则在这里降级等于没降
            from src.services.sector_identity_audit import static_fund_for_sector
            hardcoded = static_fund_for_sector(standard_sector, db=self.db)
            if hardcoded:
                return hardcoded['code'], hardcoded['name']

            # 第5步：FundInfo 精确匹配
            all_funds = self.db.query(FundInfo).filter(
                FundInfo.sector_type == sector
            ).all()
            for f in all_funds:
                fund_name = f.fund_name or ''
                if any(kw in fund_name for kw in excluded_keywords):
                    continue
                if not self.fund_code_is_servable(f.fund_code):
                    # 同一条规矩：`sector_type` 是自由文本，这里挑中的代码也可能被判不可服务
                    continue
                return f.fund_code, f.fund_name

            # 6) removed: fuzzy FundInfo match. sector_type is free text, so a
            #    fuzzy hit silently verifies a prediction against an unrelated fund.
            #    Re-matching now goes through sector_fund_agent (LLM + fetch verify).

        return None, None
    
    def retag_if_drifted(self, prediction: Prediction, fund_code: str, fund_name: str,
                         dry_run: bool = False, commit: bool = True,
                         run_id: str = None, touched_bloggers: set = None,
                         source: str = 'verify_unservable_code') -> bool:
        """"行上挂 A、解析出 B"的唯一处置：经 `retag_prediction` 改标 + 清结论 + 重算统计。

        两个入口共用（`verify_prediction` 与 `rollback_invalid_verifications`），因为
        只写在一处时另一处就会把同类行"跳过然后没人管"（第 20 轮 MAJOR-2：回溯审计只扫
        已判行，而到期队列只要 `is_correct IS NULL` ⇒ 被跳过的行永远回不到改标那条腿）。

        **提交权在调用方**（第 21 轮 BLOCKER）：回溯审计是"整批要么成、失败就回滚"的语义，
        它的错误分支写着"未保存任何修改"；这里无条件 commit 会让那句话当场为假
        （评审用第二连接实测：后一行抛错时前一行的改标已经落库）。
        `commit=False` 时把博主登记进 `touched_bloggers`，由调用方统一重算 + 提交。

        返回是否真的动了（dry_run 时返回"会动"）。
        """
        from src.fund.fund_sync_manager import FundSyncManager

        if not prediction.fund_code or fund_code == prediction.fund_code:
            return False
        if dry_run:
            return True
        old_code = prediction.fund_code
        if not fund_name:
            # 以前是 `fund_name or prediction.fund_name` ⇒ 把**旧那只的名字**写到新代码上
            # （第 21 轮 MINOR：行上"代码 = B、名字 = A"，人眼与后续按名字解析都会再错一次）
            from src.models.database import FundInfo
            row = self.db.query(FundInfo).filter(
                FundInfo.fund_code == fund_code).first()
            fund_name = (row.fund_name if row else '') or ''
        cleared = FundSyncManager.retag_prediction(
            self.db, prediction, fund_code, fund_name,
            source=source, run_id=run_id, touched_bloggers=touched_bloggers)
        if not commit:
            # 返回的是"这次动不动了"，不是"有没有清结论"：调用方拿它计数，
            # 口径必须和 dry-run / commit=True 两条一致（第 22 轮 MINOR-4）
            return True
        # 单条验证：改标是一次真实的决定，不取决于本轮验证能不能判完
        # （可能因为"净值没出"提前返回），所以这里必须自己提交。
        self.db.commit()
        if cleared and prediction.blogger_id:
            # 清掉一条结论 = 统计的分子分母都变了，而这里不走增量回退：
            # `blogger_stats` 按 `verify_count>0` 现算，少了这次重算，后面判完只会
            # `verified_delta=+1` ⇒ 同一行计两次（第 19 轮 MAJOR-1，实测 87 vs 真值 86）。
            from src.utils.blogger_stats import recalculate_blogger_stats
            recalculate_blogger_stats(self.db, prediction.blogger_id)
        logger.warning('[Verify] 预测 %s 标的由 %s 改为体检可服务的 %s %s，%s后按新标的判定',
                       prediction.id, old_code, fund_code, fund_name or '',
                       '旧结论已清除' if cleared else '本来没有结论')
        return True

    def verify_prediction(self, prediction_id: int, force: bool = False) -> Dict:
        """
        验证单个预测（支持过程验证）

        Args:
            prediction_id: 预测 ID
            force: 强制验证模式，跳过目标日净值的等待期（用于 verify_expired_pending）；验证本身无过期关闭

        Returns:
            验证结果
        """
        prediction = self.db.query(Prediction).filter(
            Prediction.id == prediction_id
        ).first()
        
        if not prediction:
            return {"success": False, "message": "预测不存在"}

        # 中性预测（flat/震荡）不参与验证和准确率计算
        if prediction.prediction_type == 'flat':
            return {
                "success": True,
                "message": "中性预测（观望）不参与验证",
                "skipped": True,
                "skip_reason": "neutral"
            }

        logger.info(f"[Verify] 开始验证预测 {prediction_id}: fund_code={prediction.fund_code}, fund_name={prediction.fund_name}, sector={prediction.sector}, target_date={prediction.target_date}")
        
        fund_code, fund_name = self.match_fund_for_prediction(prediction)
        if not fund_code:
            logger.warning(f"[Verify] 无法匹配基金: sector={prediction.sector}, fund_name={prediction.fund_name}")
            return {"success": False, "message": f"无法匹配基金：{prediction.sector}"}

        if prediction.fund_code and fund_code != prediction.fund_code:
            # 走到这里说明**行上挂着 A、这一轮要按 B 判**（自带代码被体检否掉，退到板块解析）。
            # 上一版直接拿 B 算结论、一个字都不回写 ⇒ 判完就是新一族"结论按别的基金判、
            # 行上挂另一只"（`verdict_under_other_fund` 的成因，第 18 轮 MAJOR-5），
            # 而且因为回写从不发生，那个徽章在新数据上永远测不到 = 假装有闸门。
            self.retag_if_drifted(prediction, fund_code, fund_name)
            # 单条入口：commit 默认 True，见 helper 的说明
        
        logger.info(f"[Verify] 匹配到基金: {fund_code} - {fund_name}")
        
        period_days = self.parse_period_days(prediction.prediction_period)
        config = self.get_verify_config(period_days)
        
        today = date.today()
        target_date = prediction.target_date
        
        if target_date:
            days_to_target = (target_date - today).days
            logger.info(f"[Verify] days_to_target={days_to_target}, window_days_before={config['window_days_before']}, window_days_after={config['window_days_after']}")

            if days_to_target > config['window_days_before']:
                return {
                    "success": False,
                    "message": f"验证通道尚未开放，请于目标日期前{config['window_days_before']}天验证"
                }

            # 无「过期补救期」：目标日再久远，只要区间内净值数据在就能验。
            # 数据就绪与否由下方 _check_fund_data_availability 判断。
        
        # 验证窗口固定截止到 target_date，禁止使用目标日之后的行情。
        # 目标日为非交易日时，由 get_nav_by_date(nav_date <= target_date)
        # 自动回退到目标日或之前最近有效净值，不取下一个交易日。
        window_end = target_date

        # 检查验证时间窗口：只有目标日期已过期才允许验证
        if today < target_date:
            return {
                "success": False,
                "message": f"预测周期尚未结束，请等待至 {target_date.isoformat()} 后再验证"
            }

        # 所有预测都使用预测日期作为净值起始点，确保覆盖完整周期
        nav_start_date = prediction.prediction_date

        # 自动补拉：库内最早净值晚于预测起始日时（基金录入晚于预测、历史只留近期等），
        # 先按区间从数据源补齐再检查；补拉失败不阻断，继续用现有数据走原判断。
        try:
            from src.fund.fund_api import fund_data_manager
            backfilled = fund_data_manager.backfill_history_range(
                fund_code, nav_start_date, window_end, db=self.db, today=today
            )
            if backfilled:
                self._invalidate_fund_cache(fund_code)
            # `proof_writes` 必须**无条件取出**：写成 `backfilled or pop(...) or ...`
            # 会在 backfilled 为真时短路，计数器留给下一条预测误消费（第 13 轮 MINOR-1）
            proof_written = self.db.info.pop('proof_writes', 0)
            # 补拉到的净值与凭据都是数据源事实，要提交；但只在真写了东西时提交 ——
            # 空 commit 会让 identity map 全过期（`SessionLocal` 未设
            # `expire_on_commit=False`），把预热的净值实例作废成 N+1（第 10 轮 M-3）。
            if backfilled or proof_written or self.db.new or self.db.dirty:
                try:
                    self.db.commit()
                except Exception as commit_error:
                    logger.warning(f"[Verify] 基金 {fund_code} 补拉数据提交失败: {commit_error}")
                    self.db.rollback()
        except Exception as backfill_error:
            logger.warning(
                f"[Verify] 基金 {fund_code} 历史净值补拉异常，按现有数据检查: {backfill_error}"
            )

        data_check = self._check_fund_data_availability(
            fund_code=fund_code,
            nav_start_date=nav_start_date,
            window_end=window_end,
            today=today,
            target_date=target_date,
            # force 只跳过工作日等待期，不跳过最大净值年龄检查
            skip_wait=bool(force),
        )

        if not data_check['available']:
            # 「已问过、数据源答这段没有」是**结构性**结论（reason 已在
            # `_check_fund_data_availability` 里分过档：抖动/没问过都不会走到这一个）。
            # 给它一把重问锁，否则 Cron 与页面上每一次"验证全部"都为同一批永远问不出
            # 来的预测重问一遍（任务 #8：到期队列里的噪音）。锁到哪天由凭据 TTL 决定，
            # 到点自动回队 ⇒ 不是终态、更不写 is_correct。
            held_until = None
            closed_as = None
            if data_check.get('reason') == 'no_source_history':
                from src.services.prediction_lifecycle import (
                    apply_unverifiable_hold, close_as_stale_target_note,
                    should_close_as_stale_target,
                )
                previous_hold = prediction.next_verify_date
                # 库里这只代码最后一条净值在哪天：这是"它停更了"与"我们没同步"的分界，
                # 少了它就只能靠日历猜（第 23 轮那种把镜像坏了说成产品坏了的错）。
                newest = self.db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code).order_by(
                    FundHistory.nav_date.desc()).first()
                latest_nav = self._as_date(newest[0]) if newest else None
                if should_close_as_stale_target(
                        verdict_reason=data_check.get('reason'),
                        previous_hold=previous_hold,
                        local_latest_nav=latest_nav,
                        window_start=nav_start_date,
                        today=today):
                    # 同一个窗口第二次被源端答"没有"，且这只产品从窗口开始之前就没再发过净值
                    # ⇒ 判不了是永久事实，不是"再等等"。收进回收站（带原因、可恢复、不写结论）。
                    from src.services.prediction_service import PredictionService
                    note = close_as_stale_target_note(prediction, latest_nav, nav_start_date)
                    if PredictionService(self.db).close_as_unverifiable(prediction.id, note):
                        closed_as = note
                else:
                    held_until = apply_unverifiable_hold(prediction, as_of=today)
                    try:
                        self.db.commit()
                    except Exception as hold_error:
                        logger.warning('[Verify] 预测 %s 重问锁写入失败: %s',
                                       prediction_id, hold_error)
                        self.db.rollback()
                        held_until = None
            message = data_check['message']
            if held_until:
                message += f'（已压到 {held_until.isoformat()} 再问，期间不重复占用验证）'
            if closed_as:
                message += '（' + closed_as + '）'
            return {
                "success": False,
                "message": message,
                "held_until": held_until.isoformat() if held_until else None,
                "closed_as_unverifiable": bool(closed_as),
                "data": {
                    "fund_code": fund_code,
                    "fund_name": fund_name,
                    "data_status": data_check
                }
            }

        # 数据又够用了（历史被回补、窗口里有新行）⇒ 撤掉可能还挂着的那把重问锁，
        # 否则这条预测会一直带着一个已经失效的"结构性不可验"标签。
        from src.services.prediction_lifecycle import release_unverifiable_hold
        if release_unverifiable_hold(prediction, as_of=today):
            logger.info('[Verify] 预测 %s 净值已可判，解除重问锁', prediction_id)

        logger.info(f"[Verify] 预测 {prediction_id} 开始验证, 今日: {today.isoformat()}")

        if prediction.last_verify_date and prediction.last_verify_date == today:
            logger.info(f"[Verify] 今日已验证, 跳过 prediction_id={prediction_id}")
            return {
                "success": True,
                "message": "今日已验证",
                "skipped": True
            }

        if prediction.start_nav and prediction.start_nav_date:
            if prediction.start_nav_date < window_end:
                nav_start_date = prediction.start_nav_date
                start_nav = prediction.start_nav
                is_cumulative = True
            else:
                nav_start_date = prediction.prediction_date
                start_nav = None
                is_cumulative = False
        else:
            nav_start_date = prediction.prediction_date
            start_nav = None
            is_cumulative = False

        if not start_nav:
            start_nav = self.get_nav_by_date(
                fund_code, nav_start_date, strict_as_of=True
            )
            if not start_nav:
                start_nav = prediction.start_nav

        end_nav_real_date = self._real_nav_date(fund_code, window_end)
        start_nav_real_date = self._real_nav_date(fund_code, nav_start_date)
        end_nav = self.get_nav_by_date(
            fund_code, window_end, strict_as_of=True
        )
        
        if not start_nav or not end_nav:
            return {"success": False, "message": "无法获取净值数据"}
        
        actual_change = (end_nav - start_nav) / start_nav * 100
        
        nav_history = self.get_nav_history(fund_code, nav_start_date, window_end)
        
        process_metrics = self.calculate_process_metrics(
            nav_history, start_nav, prediction.prediction_type,
            flat_threshold=config['flat_threshold']
        )
        
        blogger = self.db.query(Blogger).filter(Blogger.id == prediction.blogger_id).first()
        blogger_context = None
        if blogger:
            blogger_context = {
                'name': blogger.name,
                'grade': blogger.grade,
                'accuracy_rate': blogger.accuracy_rate or 0,
                'total_predictions': blogger.total_predictions or 0,
                'correct_predictions': blogger.correct_predictions or 0
            }
        
        comprehensive_result = self.comprehensive_verify(
            prediction_type=prediction.prediction_type or "up",
            final_change=actual_change,
            process_metrics=process_metrics,
            flat_threshold=config['flat_threshold']
        )
        
        is_correct = comprehensive_result["is_correct"]
        verify_type = comprehensive_result["verify_type"]
        score = comprehensive_result["score"]
        analysis = comprehensive_result["analysis"]
        
        # 扩大LLM调用范围：分数20-80，或者涨跌幅在边界附近，或者预测方向与实际方向相反
        should_call_llm = False
        if not config['is_ultra_short']:
            # 条件1：分数在20-80之间
            if 20 <= score <= 80:
                should_call_llm = True
            # 条件2：涨跌幅在震荡阈值附近（扩大范围）
            elif abs(actual_change) < config['flat_threshold'] * 2:
                should_call_llm = True
            # 条件3：预测方向与实际方向相反（需要LLM辅助判断博主是否正确）
            elif (prediction.prediction_type == 'up' and actual_change < 0) or \
                 (prediction.prediction_type == 'down' and actual_change > 0):
                should_call_llm = True
        
        trend_description = None
        if should_call_llm and nav_history:
            try:
                local_analyzer = get_local_trend_analyzer()
                
                total_days = len(nav_history)
                if total_days <= 30:
                    max_periods = 6
                elif total_days <= 90:
                    max_periods = 8
                else:
                    max_periods = 10
                
                trend_result = local_analyzer.analyze_trend(nav_history, max_periods=max_periods)
                if trend_result and trend_result.get("periods"):
                    periods = trend_result.get("periods", [])
                    trend_summary = trend_result.get("trend_summary", "")
                    if periods:
                        period_desc = []
                        for p in periods[:max_periods]:
                            period_desc.append(
                                f"{p.get('start_date')}~{p.get('end_date')}:{p.get('trend_desc')}{p.get('change_percent', 0):+.1f}%"
                            )
                        trend_description = f"{trend_summary[:30]}\n" + "\n".join(period_desc)
            except Exception as e:
                logger.warning(f"计算趋势描述失败: {e}")
        
        if should_call_llm:
            llm_result = self.llm_analyzer.verify_prediction(
                prediction_content=prediction.prediction_content or "",
                actual_change=actual_change,
                prediction_type=prediction.prediction_type or "up",
                confidence=prediction.confidence or 50,
                verify_count=prediction.verify_count or 0,
                flat_threshold=config['flat_threshold'],
                blogger_context=blogger_context,
                is_ultra_short=config['is_ultra_short'],
                direction_only=config['is_ultra_short'],
                process_metrics=process_metrics,
                trend_description=trend_description
            )
            
            if llm_result and isinstance(llm_result, dict):
                llm_score = llm_result.get("score", score)
                llm_score = max(0, min(100, int(llm_score)))
                if llm_score > score:
                    # 使用明确阈值判定 is_correct，不完全依赖 LLM 的 is_correct 字段
                    is_correct = llm_score >= 60
                    verify_type = "llm_verify"
                    score = llm_score
                    analysis = llm_result.get("analysis", analysis)
        
        before_state = snapshot_prediction(prediction)
        prediction.current_nav = end_nav
        # 与 `end_nav_date` 同一个口径：写**实际用到**的那一天。上一版这里写请求的
        # `window_end`，于是同一端点在两个字段里日期不一致（1106 那段注释正是为
        # `end_nav_date` 说的），而两个字段都会进快照与前端"当前净值"（第 15 轮 m-1）。
        prediction.current_nav_date = end_nav_real_date or window_end
        prediction.actual_change = actual_change
        prediction.is_correct = is_correct
        # 台账与状态同事务：有结论则 status 不得再留 pending（防二次扫待验证）
        prediction.status = "success" if is_correct else "failed"
        if prediction.verified_at is None:
            prediction.verified_at = datetime.now()
        prediction.verify_count = (prediction.verify_count or 0) + 1
        prediction.last_verify_date = today

        if not prediction.start_nav:
            prediction.start_nav = start_nav
            # 与 `end_nav_date` 同口径：存**实际用到**的净值日，不存请求的窗口起点
            # （起点落在周末/停更日时两者不是一天，混着用会让两端日期没法对比 —— 第 10 轮 M-4）
            prediction.start_nav_date = start_nav_real_date or nav_start_date

        # 确保 score 始终在 [0, 100] 范围内
        score = max(0, min(100, score))
        prediction.verify_score = score

        if not prediction.verify_history:
            prediction.verify_history = []
        prediction.verify_history.append({
            "date": today.isoformat(),
            "verify_start_date": nav_start_date.isoformat(),
            "verify_end_date": window_end.isoformat(),
            "start_nav": start_nav,
            "end_nav": end_nav,
            "change": actual_change,
            "is_cumulative": is_cumulative,
            "is_correct": is_correct,
            "verify_type": verify_type,
            "score": score,
            "analysis": analysis,
            "process_metrics": process_metrics
        })
        attributes.flag_modified(prediction, 'verify_history')

        is_newly_completed = False
        if target_date and today >= target_date:
            prediction.is_expired = True
            prediction.end_nav = end_nav
            # 写**实际**取到的净值日，不写请求的目标日：目标日落在休市日时后者是个谎
            # （1709 存成 07-11 周六，用的其实是 07-10 那条），会让"已按目标日验证"的
            # 假象进报表，也让退化终点无法被事后审计出来。
            prediction.end_nav_date = end_nav_real_date or window_end
            # status 已在上方与 is_correct 同步；此处只补到期收口字段
            if before_state.get("status") == "pending" or before_state.get("is_correct") is None:
                is_newly_completed = True
            prediction.status = "success" if is_correct else "failed"

        if is_newly_completed:
            self._update_blogger_accuracy(
                prediction.blogger_id,
                score_change=score,
                is_new_verify=True,
                is_correct=is_correct,
                commit=False,
            )

        add_prediction_change_log(
            self.db,
            prediction,
            action="verified",
            source="automatic",
            before_state=before_state,
        )
        self.db.commit()
        
        return {
            "success": True,
            "message": f"验证完成：{analysis}",
            "data": {
                "prediction_id": prediction.id,
                "is_correct": is_correct,
                "verify_type": verify_type,
                "score": score,
                "actual_change": actual_change,
                "start_nav": start_nav,
                "end_nav": end_nav,
                "process_metrics": process_metrics,
                "verify_start_date": nav_start_date.isoformat(),
                "verify_end_date": window_end.isoformat(),
                "is_cumulative": is_cumulative,
                "verify_count": prediction.verify_count,
                "analysis": analysis,
                "is_expired": prediction.is_expired,
                "fund_name": fund_name,
                "fund_code": fund_code
            }
        }
    
    def verify_all_pending(
        self,
        as_of: Optional[date] = None,
        progress_callback=None,
        max_age_days: Optional[int] = None,
    ) -> Dict:
        """验证所有待验证的预测（到期未验证即可验，无时间上限，带缓存预热）

        Args:
            as_of: 可选的"当前日期"覆盖（默认走 current_as_of 北京时间），主要供测试固定日期。
            progress_callback: 可选回调，每验证完一条调用
                progress_callback(processed, success_count, failed_count, prediction_id, ok)
            max_age_days: 兼容参数（历史签名），不再用于限制目标日下限。

        Returns:
            data.results 逐条结果（成功含验证结论，失败含具体未验证原因）；
            data.skipped 为已到期但不参与验证的预测（观望）及原因。
        """
        from src.services.prediction_lifecycle import (
            current_as_of,
            due_skip_reason,
            filter_due_for_verify,
            filter_unverifiable,
        )

        today = as_of or current_as_of()

        # 统一入口：due_unverified（is_correct is null 且未超过 NAV 年龄窗口）
        all_pending = filter_due_for_verify(self.db, as_of=today, max_age_days=max_age_days)

        # 解释"已到期但不验证"：仅观望预测（与队列同口径的宽查 + 推导原因）
        broad_due = filter_due_for_verify(
            self.db, as_of=today, exclude_flat=False, max_age_days=max_age_days,
        )
        overdue = filter_unverifiable(self.db, as_of=today, max_age_days=max_age_days)
        pending_ids = {p.id for p in all_pending}
        skipped = []
        for prediction in broad_due + overdue:
            if prediction.id in pending_ids:
                continue
            reason = due_skip_reason(prediction, as_of=today)
            if reason:
                skipped.append({"prediction_id": prediction.id, "reason": reason})

        logger.info(f"[Verify] 找到 {len(all_pending)} 个已到期待验证预测，{len(skipped)} 个到期不验证")

        # 预热：收集所有涉及的 fund_code，批量查询 FundHistory 并填充缓存
        self._warm_cache(all_pending, today)

        results = []
        success_count = 0
        failed_count = 0

        for prediction in all_pending:
            logger.info(f"[Verify] 正在验证预测 {prediction.id}: fund_code={prediction.fund_code}, sector={prediction.sector}, target_date={prediction.target_date}")

            force = bool(prediction.target_date and (today - prediction.target_date).days > 30)
            result = self.verify_prediction(prediction.id, force=force)
            ok = bool(result.get("success"))
            results.append({
                "prediction_id": prediction.id,
                "success": ok,
                "message": result.get("message"),
                # 分组键用 reason，不用整条文案：新措辞每条都嵌两个日期，按 message
                # 分组会得到 N 句各不相同的长文案，前端"未成功原因"变成一堵墙
                # （第 12 轮 MINOR-5）
                "reason": ((result.get('data') or {}).get('data_status') or {}).get('reason')
            })

            if ok:
                success_count += 1
            else:
                failed_count += 1
                logger.warning(f"[Verify] 预测 {prediction.id} 验证失败: {result.get('message')}")

            if progress_callback:
                try:
                    progress_callback(
                        len(results), success_count, failed_count, prediction.id, ok
                    )
                except Exception as cb_error:  # 进度上报失败不得中断验证
                    logger.warning(f"[Verify] 进度回调失败: {cb_error}")

        # 清理缓存，释放内存
        self._nav_cache.clear()
        self._cache_order.clear()

        skipped_note = f"，另有 {len(skipped)} 个到期不验证" if skipped else ""
        return {
            "success": True,
            "message": f"验证完成：成功 {success_count} 个，失败 {failed_count} 个{skipped_note}",
            "data": {
                "total": len(all_pending),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
                "skipped": skipped,
            }
        }

    def _warm_cache(self, predictions: List, today: date):
        """
        预热基金净值缓存：批量查询所有涉及基金的 FundHistory

        Args:
            predictions: 待验证预测列表
            today: 当前日期
        """
        if not predictions:
            return

        # 收集所有 fund_code
        fund_codes = set()
        for p in predictions:
            if p.fund_code:
                fund_codes.add(p.fund_code)

        if not fund_codes:
            logger.info("[Verify] 无 fund_code 可预热，跳过缓存")
            return

        # 计算需要查询的日期范围（取最大范围以覆盖所有预测）
        min_date = today - timedelta(days=120)  # 最多回溯 120 天
        max_date = today + timedelta(days=14)   # 最多前瞻 14 天

        logger.info(f"[Verify] 预热缓存：{len(fund_codes)} 个基金，日期范围 {min_date} ~ {max_date}")

        # 批量查询所有基金的 FundHistory
        all_records = self.db.query(FundHistory).filter(
            FundHistory.fund_code.in_(fund_codes),
            FundHistory.nav_date >= min_date,
            FundHistory.nav_date <= max_date
        ).order_by(FundHistory.fund_code, FundHistory.nav_date.asc()).all()

        # 缓存里存**轻量元组**而不是 ORM 实例：`SessionLocal` 没设
        # `expire_on_commit=False`，而批量路径每条预测都会 commit 一次
        # （`prediction_verify_task.update_progress`），实例一旦被 expire，
        # 之后每次读 `.nav_date` 都发一条 SELECT ⇒ 预热从"省查询"翻转成 N+1 放大器
        # （实测 commit 前 0 条、commit 后 720 条，第 13 轮 MAJOR-2）。
        history_cache: Dict[str, List] = {}
        for r in all_records:
            nav_date = self._as_date(r.nav_date)
            if nav_date is None:
                continue
            history_cache.setdefault(r.fund_code, []).append((nav_date, r.nav))

        self._nav_cache['_history'] = history_cache

        # 顺带清掉一桩死代码：以前这里还往 LRU 里塞 `(fund_code, 日期)` 二元组的
        # 单点净值，而 `get_nav_by_date` 查的键是 `(fund_code, 日期, strict_as_of)`
        # 三元组 ⇒ 命中率恒为 0，却把 `_cache_order` 灌到上限、挤掉真实缓存条目。
        logger.info(f"[Verify] 预热完成：{len(all_records)} 条记录，{len(history_cache)} 个基金")
    
    def verify_expired_pending(self) -> Dict:
        """兼容旧补救入口；统一扫描已包含超过 30 天的待验证预测。"""
        return self.verify_all_pending()
    
    def _update_blogger_accuracy(self, blogger_id: int, score_change: int = None, is_new_verify: bool = False, was_correct: bool = None, is_correct: bool = None, commit: bool = True):
        """
        更新博主准确率（使用统一的统计模块）
        
        Args:
            blogger_id: 博主 ID
            score_change: 分数变化量（新增预测时为正，删除/回溯时为负）
            is_new_verify: 是否是新验证完成的预测
            was_correct: 回溯时该预测是否曾被判定为正确（用于正确减少 correct_predictions）
            is_correct: 新验证时该预测是否正确
        """
        if not blogger_id:
            return
        
        from src.utils.blogger_stats import update_blogger_stats_incremental
        
        if score_change is not None:
            if is_new_verify:
                update_blogger_stats_incremental(
                    self.db, blogger_id,
                    score_delta=score_change,
                    correct_delta=1 if is_correct else 0,
                    verified_delta=1,
                    commit=commit,
                )
            else:
                update_blogger_stats_incremental(
                    self.db, blogger_id,
                    score_delta=score_change,
                    correct_delta=-1 if was_correct else 0,
                    verified_delta=-1,
                    commit=commit,
                )
    
    def update_blogger_on_prediction_delete(self, blogger_id: int, verify_score: int, is_correct: bool, commit: bool = True):
        """
        删除预测时更新博主数据

        Args:
            blogger_id: 博主 ID
            verify_score: 被删除预测的分数
            is_correct: 被删除预测是否正确
            commit: 是否在内部提交。批量清理循环应传 False，由调用方统一提交，
                    避免"博主扣分已提交但预测删除被回滚"的不一致。
        """
        if not blogger_id:
            return

        from src.utils.blogger_stats import update_blogger_stats_incremental

        score_change = -(verify_score if verify_score is not None else (100 if is_correct else 0))
        update_blogger_stats_incremental(
            self.db, blogger_id,
            score_delta=score_change,
            correct_delta=-1 if is_correct else 0,
            verified_delta=-1 if verify_score is not None else 0,
            commit=commit
        )
    
    def _get_prediction_score(self, prediction: Prediction) -> int:
        """
        获取预测的分数
        
        优先使用 verify_score，如果没有则根据 is_correct 推断
        """
        if hasattr(prediction, 'verify_score') and prediction.verify_score is not None:
            return prediction.verify_score
        
        if prediction.is_correct:
            return 100
        else:
            return 0
    
    def rollback_invalid_verifications(self, min_data_points: int = 2, dry_run: bool = True,
                                       only_ids=None, allow_full_sweep: bool = False,
                                       run_id: str = None) -> Dict:
        """
        回溯已验证但数据不足的预测
        
        检查所有已验证的预测，如果验证时基金数据不足，则重置验证状态，
        等数据充足后再重新验证。
        
        Args:
            min_data_points: 最少需要的数据点数
            only_ids: 只在这些预测 id 里回溯（谓词在取数之前过滤，既省查询也缩范围）。
                默认 None = 判不过门槛的全撤 —— 那是上千条的量级，且会把"当年用真实
                两条净值判出、如今本地镜像已丢失那段历史"的结论一起抹掉
                （实测 1208/1669 就是这种），所以定向修复必须显式传。
            allow_full_sweep: 真写且 `only_ids=None` 时的必备开关，默认 False。
                把这个毁灭口径从"忘了传参数"变成"必须显式声明"：
                前端"回溯无效验证"按钮因此只能定向撤，整库回溯只能由脚本/CLI 明确发起。
            
        Returns:
            {
                'success': bool,
                'message': str,
                'data': {
                    'total_checked': int,
                    'rolled_back': int,
                    'kept': int,
                    'code_diverged': int,
                    'errors': int,
                    'rollback_details': list
                }
            }
        """
        if not dry_run and only_ids is None and not allow_full_sweep:
            return {
                'success': False,
                'message': '真写且未指定 only_ids 时必须显式传 allow_full_sweep=True：'
                           '不限定 id 会把上千条历史结论一起抹掉，其中多数只是本地镜像缺那段历史',
                'data': {'total_checked': 0, 'would_rollback': 0, 'rolled_back': 0,
                         'kept': 0, 'skipped_by_filter': 0, 'code_diverged': 0,
                         'errors': 0, 'rollback_details': []},
            }
        wanted = set(only_ids) if only_ids is not None else None
        if not dry_run and run_id is None:
            # 真写必须带 run_id：变更日志里没有它，`restore_prediction_batch.py --run-id`
            # 就撤不回来（第 12 轮 MAJOR-4；姊妹入口 sync-sector-mapping 早就补了这条）
            run_id = 'rollback-%s' % datetime.now().strftime('%Y%m%d-%H%M%S')
        today = date.today()

        predictions = self.db.query(Prediction).filter(
            Prediction.status.in_(['success', 'failed']),
            Prediction.verify_count > 0,
            Prediction.is_deleted == False,
            Prediction.prediction_type != 'flat'
        ).all()
        # 定向模式先筛掉不相干的行：既省下上千次取数与判据计算，
        # 也让"保留 N 个"这个数字只统计**真的评估过**的行（第 10 轮 MINOR-7）
        skipped_by_filter = 0
        if wanted is not None:
            selected = [p for p in predictions if p.id in wanted]
            skipped_by_filter = len(predictions) - len(selected)
            predictions = selected
        
        total_checked = len(predictions)
        rolled_back = 0
        would_rollback = 0
        kept = 0
        errors = 0
        code_diverged = 0
        rollback_details = []
        affected_bloggers = set()
        
        for prediction in predictions:
            try:
                fund_code, fund_name = self.match_fund_for_prediction(prediction)
                if not fund_code:
                    kept += 1
                    continue
                row_code_before = prediction.fund_code
                if self.retag_if_drifted(
                        prediction, fund_code, fund_name, dry_run=dry_run,
                        commit=False, run_id=run_id, touched_bloggers=affected_bloggers,
                        source='rollback_drifted_code'):
                    # 标的已经漂到另一只基金上（自带代码被体检否掉）。"这段净值缺不缺"说的
                    # 是**别的那只**，拿它去撤 A 的结论 = 用一个无关的理由撤掉一条记录
                    # （第 19 轮 MAJOR-3）。但上一版的"只数不撤"也不成立：本函数只扫已判行、
                    # 到期队列又只要 `is_correct IS NULL` ⇒ 被跳过的行再也回不到改标那条腿，
                    # 语义只是从"错误撤销"变成"没人管"（第 20 轮 MAJOR-2）。
                    # 现在与 `verify_prediction` 共用一个入口：dry-run 报"会改标"，真跑就
                    # 改标（留痕、带本批 run_id、清旧结论），提交与统计重算由本函数末尾统一做。
                    code_diverged += 1
                    rollback_details.append({
                        'prediction_id': prediction.id,
                        'action': 'would_retag' if dry_run else 'retagged',
                        'row_code': row_code_before, 'resolved_code': fund_code,
                    })
                    continue
                
                period_days = self.parse_period_days(prediction.prediction_period)
                config = self.get_verify_config(period_days)

                target_date = prediction.target_date
                if not target_date:
                    kept += 1
                    continue

                # 验证窗口：使用完整预测周期（prediction_date 到 target_date）
                window_end = target_date
                nav_start_date = prediction.prediction_date

                data_check = self._check_fund_data_availability(
                    fund_code=fund_code,
                    nav_start_date=nav_start_date,
                    window_end=window_end,
                    min_data_points=min_data_points,
                    today=today,
                    target_date=target_date,
                    skip_wait=True,  # rollback 不等待，直接判断是否可验证
                )
                
                if not data_check['available']:
                    would_rollback += 1
                    old_status = prediction.status
                    old_verify_score = prediction.verify_score
                    old_is_correct = prediction.is_correct

                    if not dry_run:
                        before_state = snapshot_prediction(prediction)
                        # 不往 verify_history 里塞"墓碑"：前端"验证历史"表按
                        # `h.is_correct ? '正确' : '错误'`、`h.score || 0` 渲染，
                        # 一条只有说明文字的墓碑会被画成"验证失败 / 0 分 / 错误"
                        # —— 凭空多出一条假历史记录（第 13 轮 MAJOR-1）。
                        # 回溯的审计走 prediction_change_logs（action=verification_rollback
                        # + before_state + run_id），那才是可回滚、可核对的地方。
                        clear_verification_fields(prediction)
                        add_prediction_change_log(
                            self.db,
                            prediction,
                            action="verification_rollback",
                            source="maintenance",
                            before_state=before_state,
                            run_id=run_id,
                        )
                        affected_bloggers.add(prediction.blogger_id)
                        rolled_back += 1
                    rollback_details.append({
                        'prediction_id': prediction.id,
                        'fund_code': fund_code,
                        'fund_name': fund_name,
                        'old_status': old_status,
                        'old_verify_score': old_verify_score,
                        'reason': data_check['message'],
                        'data_status': data_check
                    })
                    
                    logger.info(f"[Rollback] 预测 {prediction.id} 已回溯: {data_check['message']}")
                else:
                    kept += 1
                    
            except Exception as e:
                errors += 1
                self.db.rollback()
                logger.error(f"[Rollback] 检查预测 {prediction.id} 时出错: {e}")
                return {
                    'success': False,
                    'message': f"回溯检查失败，未保存任何修改: {e}",
                    'data': {'total_checked': total_checked, 'errors': errors},
                }

        if not dry_run:
            from src.utils.blogger_stats import recalculate_blogger_stats
            for blogger_id in affected_bloggers:
                if blogger_id:
                    recalculate_blogger_stats(self.db, blogger_id, commit=False)
            self.db.commit()
        
        return {
            'success': True,
            'message': f"{'预览' if dry_run else '回溯'}完成：检查 {total_checked} 个预测，"
                       f"{'将回溯' if dry_run else '已回溯'} {would_rollback if dry_run else rolled_back} 个，"
                       f"保留 {kept} 个，错误 {errors} 个，"
                       f"标的已漂移 {code_diverged} 个"
                       f"（{'按可服务标的改标' if dry_run else '已改标并清掉旧结论'}）"
                       + ('' if dry_run or not run_id
                          else f'（run_id={run_id}，要整批撤销用 '
                               f'python scripts/restore_prediction_batch.py --run-id {run_id}）'),
            'data': {
                'dry_run': dry_run,
                'total_checked': total_checked,
                'would_rollback': would_rollback,
                'rolled_back': rolled_back,
                'kept': kept,
                'skipped_by_filter': skipped_by_filter,
                # 标的已漂到另一只基金的行：本函数不撤它的结论，只数出来（MAJOR-3）
                'code_diverged': code_diverged,
                'run_id': run_id,
                'errors': errors,
                'rollback_details': rollback_details
            }
        }
