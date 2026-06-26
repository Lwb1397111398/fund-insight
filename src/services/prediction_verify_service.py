"""
预测验证服务
支持所有预测周期的验证，包括超短期预测（1-3天）
支持过程验证和峰值验证
"""
from datetime import date, timedelta, datetime
from typing import Dict, Optional, List, Tuple
from sqlalchemy.orm import Session, attributes
from sqlalchemy import and_
import logging

from src.models.database import Prediction, FundInfo, FundHistory, Blogger
from src.analyzer.llm_analyzer import get_analyzer
from src.fund.fund_api import FundAPI
from src.utils.prediction_utils import PERIOD_MAP, ULTRA_SHORT_PERIODS, parse_period_to_days
from src.analyzer.local_trend_analyzer import get_local_trend_analyzer
from src.core.config import config

logger = logging.getLogger(__name__)


class PredictionVerifyService:
    """预测验证服务"""

    # 缓存最大条目数，防止内存溢出
    MAX_CACHE_SIZE = 10000

    def __init__(self, db: Session):
        self.db = db
        self.llm_analyzer = get_analyzer()
        self.fund_api = FundAPI()
        # 实例级净值缓存，用于批量验证时避免 N+1 查询
        # 结构: {(fund_code, date_str): nav, '_history': {fund_code: [FundHistory,...]}}
        # 注意：使用 LRU 机制限制大小，防止内存溢出
        self._nav_cache: Dict = {}
        self._cache_order: list = []  # 记录缓存插入顺序，用于 LRU 淘汰
    
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

    def get_nav_by_date(self, fund_code: str, target_date: date):
        """获取指定日期的基金净值（优先读缓存）"""
        cache_key = (fund_code, target_date.isoformat())
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
        # 尝试从缓存获取
        history_cache = self._nav_cache.get('_history', {})
        if fund_code in history_cache:
            cached_records = history_cache[fund_code]
            result = [
                {"date": r.nav_date, "nav": r.nav}
                for r in cached_records
                if start_date <= r.nav_date <= end_date
            ]
            if result:
                return sorted(result, key=lambda x: x["date"])

        records = self.db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date >= start_date,
            FundHistory.nav_date <= end_date
        ).order_by(FundHistory.nav_date.asc()).all()

        if records:
            return [{"date": r.nav_date, "nav": r.nav} for r in records]

        return []
    
    def _check_fund_data_availability(
        self,
        fund_code: str,
        nav_start_date: date,
        window_end: date,
        min_data_points: int = 2
    ) -> Dict:
        """
        检查基金数据是否充足以进行验证（优先读缓存）

        Args:
            fund_code: 基金代码
            nav_start_date: 净值起始日期
            window_end: 验证窗口结束日期
            min_data_points: 最少需要的数据点数

        Returns:
            {
                'available': bool,  # 数据是否充足
                'message': str,     # 提示信息
                'data_points': int, # 实际数据点数
                'latest_date': date # 最新数据日期
            }
        """
        records = None
        # 尝试从缓存获取
        history_cache = self._nav_cache.get('_history', {})
        if fund_code in history_cache:
            cached_records = history_cache[fund_code]
            records = [
                r for r in cached_records
                if nav_start_date <= r.nav_date <= window_end
            ]
            records.sort(key=lambda r: r.nav_date)

        if records is None:
            records = self.db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date >= nav_start_date,
                FundHistory.nav_date <= window_end
            ).order_by(FundHistory.nav_date.asc()).all()

        data_points = len(records)
        latest_date = records[-1].nav_date if records else None

        if data_points < min_data_points:
            # 尝试从缓存获取最新记录
            latest_record = None
            if fund_code in history_cache:
                cached = history_cache[fund_code]
                if cached:
                    latest_record = max(cached, key=lambda r: r.nav_date)
            else:
                latest_record = self.db.query(FundHistory).filter(
                    FundHistory.fund_code == fund_code
                ).order_by(FundHistory.nav_date.desc()).first()

            if latest_record:
                latest_date = latest_record.nav_date
                days_behind = (window_end - latest_date).days
                return {
                    'available': False,
                    'message': f"基金数据不足，最新数据为 {latest_date}，落后 {days_behind} 天，请更新基金数据后再验证",
                    'data_points': data_points,
                    'latest_date': latest_date,
                    'days_behind': days_behind
                }
            else:
                return {
                    'available': False,
                    'message': f"基金 {fund_code} 无历史数据，请先更新基金数据",
                    'data_points': 0,
                    'latest_date': None
                }

        return {
            'available': True,
            'message': f"数据充足，共 {data_points} 个数据点",
            'data_points': data_points,
            'latest_date': latest_date
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
        
        if prediction_type == 'up':
            peak_hit = max_change > 0
            peak_hit_days = sum(1 for c in changes if c["change"] > 0)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
        elif prediction_type == 'down':
            peak_hit = min_change < 0
            peak_hit_days = sum(1 for c in changes if c["change"] < 0)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
        else:
            peak_hit = abs(max_change) < flat_threshold and abs(min_change) < flat_threshold
            peak_hit_days = sum(1 for c in changes if abs(c["change"]) < flat_threshold)
            peak_hit_ratio = peak_hit_days / len(changes) if changes else 0
        
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
            "peak_date": peak_date.isoformat() if isinstance(peak_date, date) else peak_date,
            "peak_nav": peak_nav,
            "trough_date": trough_date.isoformat() if isinstance(trough_date, date) else trough_date,
            "trough_nav": trough_nav,
            "peak_hit": peak_hit,
            "peak_hit_days": peak_hit_days,
            "peak_hit_ratio": round(peak_hit_ratio, 2),
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
        
        if peak_correct:
            score = int(60 + peak_hit_ratio * 30)
            return {
                "is_correct": True,
                "verify_type": "process",
                "score": min(score, 90),
                "analysis": f"过程中{int(peak_hit_ratio*100)}%时间在阈值内，判定部分正确"
            }
        
        if peak_hit_ratio >= 0.3:
            score = int(40 + peak_hit_ratio * 30)
            return {
                "is_correct": False,
                "verify_type": "partial",
                "score": min(score, 60),
                "analysis": f"过程中{int(peak_hit_ratio*100)}%时间在阈值内，判定部分正确"
            }
        
        return {
            "is_correct": False,
            "verify_type": "failed",
            "score": 0,
            "analysis": f"预测方向错误，最终涨跌{final_change:+.2f}%"
        }
    
    def match_fund_for_prediction(self, prediction: Prediction) -> Tuple:
        """为预测匹配基金"""
        if prediction.fund_code:
            return prediction.fund_code, prediction.fund_name
        
        if prediction.fund_name:
            fund = self.db.query(FundInfo).filter(
                FundInfo.fund_name == prediction.fund_name
            ).first()
            if fund:
                return fund.fund_code, fund.fund_name
        
        sector = prediction.sector or prediction.sector_type
        if sector:
            excluded_keywords = ['债券', '债', '货币', '理财', '短债', '纯债', '利率债', '信用债']
            
            all_funds = self.db.query(FundInfo).filter(
                FundInfo.sector_type == sector
            ).all()
            
            for f in all_funds:
                fund_name = f.fund_name or ''
                if not any(kw in fund_name for kw in excluded_keywords):
                    return f.fund_code, f.fund_name
            
            all_funds = self.db.query(FundInfo).all()
            for f in all_funds:
                if f.sector_type and (sector in f.sector_type or f.sector_type in sector):
                    fund_name = f.fund_name or ''
                    if not any(kw in fund_name for kw in excluded_keywords):
                        return f.fund_code, f.fund_name
        
        return None, None
    
    def verify_prediction(self, prediction_id: int, force: bool = False) -> Dict:
        """
        验证单个预测（支持过程验证）

        Args:
            prediction_id: 预测 ID
            force: 强制验证模式，跳过30天补救期检查（用于 verify_expired_pending）

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
            
            has_verified = (prediction.verify_count or 0) > 0

            if not force:
                if has_verified and prediction.status not in ('pending',):
                    grace_period_days = 30
                    if days_to_target < -grace_period_days:
                        return {
                            "success": False,
                            "message": f"验证通道已关闭（目标日期已过{abs(days_to_target)}天，超过{grace_period_days}天补救期）"
                        }
                elif not has_verified:
                    grace_period_days = 30
                    if days_to_target < -grace_period_days:
                        return {
                            "success": False,
                            "message": f"验证通道已关闭（目标日期已过{abs(days_to_target)}天，超过{grace_period_days}天补救期）"
                        }
        
        # 验证窗口：已过期的预测扩展到 today，允许用最新交易日数据验证
        # 解决 target_date 是非交易日（周末/节假日）导致数据不足的问题
        window_end = today if today > target_date else target_date

        # 检查验证时间窗口：只有目标日期已过期才允许验证
        if today < target_date:
            return {
                "success": False,
                "message": f"预测周期尚未结束，请等待至 {target_date.isoformat()} 后再验证"
            }

        # 所有预测都使用预测日期作为净值起始点，确保覆盖完整周期
        nav_start_date = prediction.prediction_date
        
        data_check = self._check_fund_data_availability(
            fund_code=fund_code,
            nav_start_date=nav_start_date,
            window_end=window_end,
            min_data_points=2
        )
        
        if not data_check['available']:
            return {
                "success": False,
                "message": data_check['message'],
                "data": {
                    "fund_code": fund_code,
                    "fund_name": fund_name,
                    "data_status": data_check
                }
            }
        
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
            start_nav = self.get_nav_by_date(fund_code, nav_start_date)
            if not start_nav:
                start_nav = prediction.start_nav
        
        end_nav = self.get_nav_by_date(fund_code, window_end)
        
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
        
        prediction.current_nav = end_nav
        prediction.current_nav_date = window_end
        prediction.actual_change = actual_change
        prediction.is_correct = is_correct
        prediction.verify_count = (prediction.verify_count or 0) + 1
        prediction.last_verify_date = today
        
        if not prediction.start_nav:
            prediction.start_nav = start_nav
            prediction.start_nav_date = nav_start_date

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
            prediction.end_nav_date = window_end
            if prediction.status == 'pending':
                is_newly_completed = True
            prediction.status = "success" if is_correct else "failed"
        
        self.db.commit()
        
        if is_newly_completed:
            self._update_blogger_accuracy(prediction.blogger_id, score_change=score, is_new_verify=True, is_correct=is_correct)
        
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
    
    def verify_all_pending(self) -> Dict:
        """验证所有待验证的预测（只验证已到期的，带缓存预热）"""
        today = date.today()

        # 只查询已到期的预测（target_date <= today）
        all_pending = self.db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.is_deleted == False,
            Prediction.prediction_type != 'flat',
            Prediction.target_date <= today
        ).all()

        logger.info(f"[Verify] 找到 {len(all_pending)} 个已到期待验证预测")

        # 预热：收集所有涉及的 fund_code，批量查询 FundHistory 并填充缓存
        self._warm_cache(all_pending, today)

        results = []
        success_count = 0
        failed_count = 0

        for prediction in all_pending:
            logger.info(f"[Verify] 正在验证预测 {prediction.id}: fund_code={prediction.fund_code}, sector={prediction.sector}, target_date={prediction.target_date}")

            result = self.verify_prediction(prediction.id)
            results.append({
                "prediction_id": prediction.id,
                "success": result.get("success"),
                "message": result.get("message")
            })

            if result.get("success"):
                success_count += 1
            else:
                failed_count += 1
                logger.warning(f"[Verify] 预测 {prediction.id} 验证失败: {result.get('message')}")

        # 清理缓存，释放内存
        self._nav_cache.clear()
        self._cache_order.clear()

        return {
            "success": True,
            "message": f"验证完成：成功 {success_count} 个，失败 {failed_count} 个",
            "data": {
                "total": len(all_pending),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results
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

        # 按基金代码分组存入缓存（使用 LRU 淘汰）
        history_cache: Dict[str, List] = {}
        for r in all_records:
            key = (r.fund_code, r.nav_date.isoformat())
            self._add_to_cache(key, r.nav)
            if r.fund_code not in history_cache:
                history_cache[r.fund_code] = []
            history_cache[r.fund_code].append(r)

        self._nav_cache['_history'] = history_cache

        logger.info(f"[Verify] 预热完成：{len(all_records)} 条记录，{len(history_cache)} 个基金")
    
    def verify_expired_pending(self) -> Dict:
        """验证所有超过30天补救期的待验证预测（补救验证，与 verify_all_pending 互补）

        verify_all_pending 验证 target_date <= today 的预测，
        verify_expired_pending 验证 target_date < grace_cutoff (30天前) 的预测。
        """
        today = date.today()
        grace_cutoff = today - timedelta(days=30)

        # 查询超过30天补救期的预测（这些预测不会被 verify_all_pending 处理）
        expired_pending = self.db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.is_deleted == False,
            Prediction.prediction_type != 'flat',
            Prediction.target_date < grace_cutoff
        ).all()

        logger.info(f"[Verify-Expired] 找到 {len(expired_pending)} 个超过30天补救期的待验证预测")

        if not expired_pending:
            return {
                "success": True,
                "message": "没有需要补救验证的预测",
                "data": {
                    "total": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "results": []
                }
            }

        # 预热缓存
        self._warm_cache(expired_pending, today)

        results = []
        success_count = 0
        failed_count = 0

        for prediction in expired_pending:

            fund_code, fund_name = self.match_fund_for_prediction(prediction)
            if not fund_code:
                logger.warning(f"[Verify-Expired] 预测 {prediction.id} 无法匹配基金，跳过")
                failed_count += 1
                results.append({
                    "prediction_id": prediction.id,
                    "success": False,
                    "message": f"无法匹配基金：{prediction.sector}"
                })
                continue

            # 直接调用 verify_prediction，由它内部处理数据可用性检查
            result = self.verify_prediction(prediction.id, force=True)
            results.append({
                "prediction_id": prediction.id,
                "success": result.get("success"),
                "message": result.get("message")
            })

            if result.get("success"):
                success_count += 1
            else:
                failed_count += 1
                logger.warning(f"[Verify-Expired] 预测 {prediction.id} 验证失败: {result.get('message')}")

        # 清理缓存
        self._nav_cache.clear()
        self._cache_order.clear()

        return {
            "success": True,
            "message": f"补救验证完成：成功 {success_count} 个，失败 {failed_count} 个",
            "data": {
                "total": len(expired_pending),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results
            }
        }
    
    def _update_blogger_accuracy(self, blogger_id: int, score_change: int = None, is_new_verify: bool = False, was_correct: bool = None, is_correct: bool = None):
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
                    verified_delta=1
                )
            else:
                update_blogger_stats_incremental(
                    self.db, blogger_id,
                    score_delta=score_change,
                    correct_delta=-1 if was_correct else 0,
                    verified_delta=-1
                )
    
    def update_blogger_on_prediction_delete(self, blogger_id: int, verify_score: int, is_correct: bool):
        """
        删除预测时更新博主数据
        
        Args:
            blogger_id: 博主 ID
            verify_score: 被删除预测的分数
            is_correct: 被删除预测是否正确
        """
        if not blogger_id:
            return
        
        from src.utils.blogger_stats import update_blogger_stats_incremental
        
        score_change = -(verify_score if verify_score is not None else (100 if is_correct else 0))
        update_blogger_stats_incremental(
            self.db, blogger_id,
            score_delta=score_change,
            correct_delta=-1 if is_correct else 0,
            verified_delta=-1 if verify_score is not None else 0
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
    
    def rollback_invalid_verifications(self, min_data_points: int = 2) -> Dict:
        """
        回溯已验证但数据不足的预测
        
        检查所有已验证的预测，如果验证时基金数据不足，则重置验证状态，
        等数据充足后再重新验证。
        
        Args:
            min_data_points: 最少需要的数据点数
            
        Returns:
            {
                'success': bool,
                'message': str,
                'data': {
                    'total_checked': int,
                    'rolled_back': int,
                    'kept': int,
                    'errors': int,
                    'rollback_details': list
                }
            }
        """
        today = date.today()
        
        predictions = self.db.query(Prediction).filter(
            Prediction.status.in_(['success', 'failed']),
            Prediction.verify_count > 0,
            Prediction.is_deleted == False,
            Prediction.prediction_type != 'flat'
        ).all()
        
        total_checked = len(predictions)
        rolled_back = 0
        kept = 0
        errors = 0
        rollback_details = []
        
        for prediction in predictions:
            try:
                fund_code, fund_name = self.match_fund_for_prediction(prediction)
                if not fund_code:
                    kept += 1
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
                    min_data_points=min_data_points
                )
                
                if not data_check['available']:
                    old_status = prediction.status
                    old_verify_score = prediction.verify_score
                    old_is_correct = prediction.is_correct
                    
                    prediction.status = 'pending'
                    prediction.is_expired = False
                    prediction.verify_history = []
                    prediction.verify_count = 0
                    prediction.verify_score = None
                    prediction.actual_change = None
                    prediction.is_correct = None
                    prediction.current_nav = None
                    prediction.current_nav_date = None
                    prediction.end_nav = None
                    prediction.end_nav_date = None
                    prediction.start_nav = None
                    prediction.start_nav_date = None
                    
                    if prediction.blogger_id and old_verify_score is not None:
                        self._update_blogger_accuracy(
                            prediction.blogger_id,
                            score_change=-old_verify_score,
                            is_new_verify=False,
                            was_correct=old_is_correct
                        )
                    
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
                logger.error(f"[Rollback] 检查预测 {prediction.id} 时出错: {e}")
        
        self.db.commit()
        
        return {
            'success': True,
            'message': f"回溯完成：检查 {total_checked} 个预测，回溯 {rolled_back} 个，保留 {kept} 个，错误 {errors} 个",
            'data': {
                'total_checked': total_checked,
                'rolled_back': rolled_back,
                'kept': kept,
                'errors': errors,
                'rollback_details': rollback_details
            }
        }
    
    def get_verification_status(self, prediction_id: int) -> Dict:
        """
        获取预测的验证状态（用于前端显示）
        
        Args:
            prediction_id: 预测 ID
            
        Returns:
            {
                'can_verify': bool,       # 是否可以验证
                'reason': str,            # 原因说明
                'data_status': dict,      # 数据状态
                'prediction_status': str  # 预测当前状态
            }
        """
        prediction = self.db.query(Prediction).filter(
            Prediction.id == prediction_id
        ).first()
        
        if not prediction:
            return {
                'can_verify': False,
                'reason': '预测不存在',
                'data_status': None,
                'prediction_status': None
            }
        
        fund_code, fund_name = self.match_fund_for_prediction(prediction)
        if not fund_code:
            return {
                'can_verify': False,
                'reason': f'无法匹配基金：{prediction.sector}',
                'data_status': None,
                'prediction_status': prediction.status
            }
        
        period_days = self.parse_period_days(prediction.prediction_period)
        config = self.get_verify_config(period_days)
        
        today = date.today()
        target_date = prediction.target_date
        
        if target_date:
            days_to_target = (target_date - today).days
            
            if days_to_target > config['window_days_before']:
                return {
                    'can_verify': False,
                    'reason': f"验证通道尚未开放，请于目标日期前{config['window_days_before']}天验证",
                    'data_status': None,
                    'prediction_status': prediction.status
                }
            
            if days_to_target < -config['window_days_after']:
                return {
                    'can_verify': False,
                    'reason': "验证通道已关闭",
                    'data_status': None,
                    'prediction_status': prediction.status
                }

        # 验证窗口：使用完整预测周期（prediction_date 到 target_date）
        window_end = target_date
        nav_start_date = prediction.prediction_date

        data_check = self._check_fund_data_availability(
            fund_code=fund_code,
            nav_start_date=nav_start_date,
            window_end=window_end,
            min_data_points=2
        )

        return {
            'can_verify': data_check['available'],
            'reason': data_check['message'],
            'data_status': data_check,
            'prediction_status': prediction.status,
            'fund_code': fund_code,
            'fund_name': fund_name
        }
