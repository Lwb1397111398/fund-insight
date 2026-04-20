"""
动态分析触发器

功能：
1. 价格冲击触发（单日涨跌≥3%）
2. 市场事件触发（政策利好/利空）
3. 预测到期触发
4. 异常波动触发
"""
import json
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable
from sqlalchemy.orm import Session

from src.models.database import (
    FundInfo, FundHistory, Prediction, MarketEvent, SessionLocal
)
from src.fund.technical_analyzer import technical_calculator, relative_performance_analyzer


class DynamicAnalysisTrigger:
    """
    动态分析触发器
    
    触发条件：
    1. 价格冲击：单日涨跌≥3%
    2. 市场事件：政策利好/利空
    3. 预测到期：预测即将到期
    4. 异常波动：连续涨跌/震荡
    """
    
    PRICE_SHOCK_THRESHOLD = 3.0
    CONTINUOUS_DAYS = 3
    PREDICTION_EXPIRE_DAYS = 2
    
    def __init__(self):
        self.callbacks = {
            'price_shock': [],
            'market_event': [],
            'prediction_expire': [],
            'anomaly': []
        }
    
    def register_callback(self, trigger_type: str, callback: Callable):
        """注册回调函数"""
        if trigger_type in self.callbacks:
            self.callbacks[trigger_type].append(callback)
    
    def check_all_triggers(self, db: Session) -> Dict:
        """
        检查所有触发条件
        
        Returns:
            {
                'price_shock': [价格冲击基金列表],
                'market_event': [市场事件列表],
                'prediction_expire': [即将到期预测列表],
                'anomaly': [异常波动基金列表],
                'triggered_count': 触发总数
            }
        """
        result = {
            'price_shock': [],
            'market_event': [],
            'prediction_expire': [],
            'anomaly': [],
            'triggered_count': 0
        }
        
        result['price_shock'] = self._check_price_shock(db)
        
        result['market_event'] = self._check_market_events(db)
        
        result['prediction_expire'] = self._check_prediction_expire(db)
        
        result['anomaly'] = self._check_anomaly(db)
        
        result['triggered_count'] = (
            len(result['price_shock']) + 
            len(result['market_event']) + 
            len(result['prediction_expire']) + 
            len(result['anomaly'])
        )
        
        return result
    
    def _check_price_shock(self, db: Session) -> List[Dict]:
        """检查价格冲击"""
        triggered = []
        
        funds = db.query(FundInfo).filter(
            FundInfo.latest_nav.isnot(None),
            FundInfo.day_growth.isnot(None)
        ).all()
        
        for fund in funds:
            if fund.day_growth and abs(fund.day_growth) >= self.PRICE_SHOCK_THRESHOLD:
                trigger_info = {
                    'fund_code': fund.fund_code,
                    'fund_name': fund.fund_name,
                    'day_growth': fund.day_growth,
                    'trigger_type': 'price_shock',
                    'trigger_reason': f'单日涨跌{fund.day_growth:+.2f}%超过阈值{self.PRICE_SHOCK_THRESHOLD}%',
                    'trigger_time': datetime.now().isoformat()
                }
                triggered.append(trigger_info)
                
                self._execute_callbacks('price_shock', trigger_info, db)
        
        return triggered
    
    def _check_market_events(self, db: Session) -> List[Dict]:
        """检查市场事件"""
        triggered = []
        
        recent_events = db.query(MarketEvent).filter(
            MarketEvent.event_date >= datetime.now() - timedelta(days=1),
            MarketEvent.trigger_analysis == False
        ).all()
        
        for event in recent_events:
            trigger_info = {
                'event_id': event.id,
                'event_type': event.event_type,
                'event_level': event.event_level,
                'title': event.title,
                'affected_sectors': event.affected_sectors,
                'affected_funds': event.affected_funds,
                'trigger_type': 'market_event',
                'trigger_reason': f'检测到{event.event_level}级{event.event_type}事件',
                'trigger_time': datetime.now().isoformat()
            }
            triggered.append(trigger_info)
            
            event.trigger_analysis = True
            event.analysis_triggered_at = datetime.now()
            
            self._execute_callbacks('market_event', trigger_info, db)
        
        db.commit()
        
        return triggered
    
    def _check_prediction_expire(self, db: Session) -> List[Dict]:
        """检查即将到期的预测"""
        triggered = []
        
        today = date.today()
        expire_threshold = today + timedelta(days=self.PREDICTION_EXPIRE_DAYS)
        
        predictions = db.query(Prediction).filter(
            Prediction.is_expired == False,
            Prediction.target_date <= expire_threshold,
            Prediction.target_date >= today
        ).all()
        
        for pred in predictions:
            days_remaining = (pred.target_date - today).days
            
            trigger_info = {
                'prediction_id': pred.id,
                'fund_code': pred.fund_code,
                'fund_name': pred.fund_name,
                'sector': pred.sector,
                'prediction_type': pred.prediction_type,
                'target_date': pred.target_date.isoformat(),
                'days_remaining': days_remaining,
                'trigger_type': 'prediction_expire',
                'trigger_reason': f'预测将在{days_remaining}天后到期',
                'trigger_time': datetime.now().isoformat()
            }
            triggered.append(trigger_info)
            
            self._execute_callbacks('prediction_expire', trigger_info, db)
        
        return triggered
    
    def _check_anomaly(self, db: Session) -> List[Dict]:
        """检查异常波动"""
        triggered = []
        
        funds = db.query(FundInfo).filter(
            FundInfo.latest_nav.isnot(None)
        ).all()
        
        for fund in funds:
            history = db.query(FundHistory).filter(
                FundHistory.fund_code == fund.fund_code
            ).order_by(FundHistory.nav_date.desc()).limit(self.CONTINUOUS_DAYS + 1).all()
            
            if len(history) < self.CONTINUOUS_DAYS:
                continue
            
            growths = [h.day_growth for h in history if h.day_growth is not None]
            
            if len(growths) >= self.CONTINUOUS_DAYS:
                all_positive = all(g > 0 for g in growths[:self.CONTINUOUS_DAYS])
                all_negative = all(g < 0 for g in growths[:self.CONTINUOUS_DAYS])
                
                if all_positive or all_negative:
                    total_change = sum(growths[:self.CONTINUOUS_DAYS])
                    direction = '连续上涨' if all_positive else '连续下跌'
                    
                    trigger_info = {
                        'fund_code': fund.fund_code,
                        'fund_name': fund.fund_name,
                        'continuous_days': self.CONTINUOUS_DAYS,
                        'total_change': total_change,
                        'direction': direction,
                        'trigger_type': 'anomaly',
                        'trigger_reason': f'{direction}{self.CONTINUOUS_DAYS}天，累计{total_change:+.2f}%',
                        'trigger_time': datetime.now().isoformat()
                    }
                    triggered.append(trigger_info)
                    
                    self._execute_callbacks('anomaly', trigger_info, db)
        
        return triggered
    
    def _execute_callbacks(self, trigger_type: str, trigger_info: Dict, db: Session):
        """执行回调函数"""
        for callback in self.callbacks.get(trigger_type, []):
            try:
                callback(trigger_info, db)
            except Exception as e:
                logging.error(f"[DynamicTrigger] 回调执行失败: {e}")
    
    def manual_trigger_analysis(self, fund_codes: List[str], reason: str,
                                db: Session) -> Dict:
        """
        手动触发分析
        
        Args:
            fund_codes: 基金代码列表
            reason: 触发原因
            db: 数据库会话
        """
        result = {
            'triggered': [],
            'failed': [],
            'total': len(fund_codes)
        }
        
        for fund_code in fund_codes:
            trigger_info = {
                'fund_code': fund_code,
                'trigger_type': 'manual',
                'trigger_reason': reason,
                'trigger_time': datetime.now().isoformat()
            }
            
            try:
                self._execute_callbacks('manual', trigger_info, db)
                result['triggered'].append(fund_code)
            except Exception as e:
                result['failed'].append({
                    'fund_code': fund_code,
                    'error': str(e)
                })
        
        return result


class BatchFetchOptimizer:
    """
    批量抓取优化器
    
    功能：
    1. 批量请求合并
    2. 并发控制
    3. 请求去重
    4. 结果缓存
    """
    
    BATCH_SIZE = 10
    MAX_CONCURRENT = 5
    REQUEST_TIMEOUT = 30
    
    def __init__(self):
        self.request_queue = []
        self.result_cache = {}
    
    def batch_get_nav(self, fund_codes: List[str], use_cache: bool = True) -> Dict[str, Dict]:
        """
        批量获取净值
        
        Args:
            fund_codes: 基金代码列表
            use_cache: 是否使用缓存
        
        Returns:
            {fund_code: nav_result}
        """
        results = {}
        need_fetch = []
        
        if use_cache:
            for code in fund_codes:
                if code in self.result_cache:
                    cached = self.result_cache[code]
                    if self._is_cache_valid(cached):
                        results[code] = cached['data']
                        continue
                need_fetch.append(code)
        else:
            need_fetch = fund_codes
        
        if need_fetch:
            from src.fund.multi_source_api import multi_source_api
            
            for i in range(0, len(need_fetch), self.BATCH_SIZE):
                batch = need_fetch[i:i + self.BATCH_SIZE]
                
                for fund_code in batch:
                    try:
                        nav_result = multi_source_api.get_nav_with_validation(fund_code)
                        results[fund_code] = nav_result
                        
                        self.result_cache[fund_code] = {
                            'data': nav_result,
                            'cached_at': datetime.now()
                        }
                    except Exception as e:
                        results[fund_code] = {
                            'nav': None,
                            'quality': 'error',
                            'quality_note': str(e)
                        }
        
        return results
    
    def _is_cache_valid(self, cached: Dict, max_age_minutes: int = 15) -> bool:
        """检查缓存是否有效"""
        if not cached or 'cached_at' not in cached:
            return False
        
        age = (datetime.now() - cached['cached_at']).total_seconds() / 60
        return age < max_age_minutes
    
    def clear_cache(self, fund_code: str = None):
        """清除缓存"""
        if fund_code:
            self.result_cache.pop(fund_code, None)
        else:
            self.result_cache.clear()


class DataCacheManager:
    """
    数据缓存管理器
    
    缓存规则：
    - 实时净值：15分钟
    - 历史净值：24小时
    - 基金信息：7天
    - 技术指标：6小时
    """
    
    CACHE_RULES = {
        'realtime_nav': 15 * 60,
        'history_nav': 24 * 60 * 60,
        'fund_info': 7 * 24 * 60 * 60,
        'technical_indicators': 6 * 60 * 60,
        'relative_performance': 6 * 60 * 60
    }
    
    def __init__(self):
        self.cache = {}
    
    def get(self, key: str, cache_type: str) -> Optional[any]:
        """获取缓存"""
        if key not in self.cache:
            return None
        
        cached = self.cache[key]
        
        if cache_type not in self.CACHE_RULES:
            return cached['data']
        
        age = (datetime.now() - cached['cached_at']).total_seconds()
        if age > self.CACHE_RULES[cache_type]:
            del self.cache[key]
            return None
        
        return cached['data']
    
    def set(self, key: str, data: any, cache_type: str = None):
        """设置缓存"""
        self.cache[key] = {
            'data': data,
            'cached_at': datetime.now(),
            'cache_type': cache_type
        }
    
    def invalidate(self, pattern: str = None):
        """使缓存失效"""
        if pattern:
            keys_to_delete = [k for k in self.cache.keys() if pattern in k]
            for k in keys_to_delete:
                del self.cache[k]
        else:
            self.cache.clear()
    
    def get_cache_stats(self) -> Dict:
        """获取缓存统计"""
        stats = {
            'total_items': len(self.cache),
            'by_type': {},
            'oldest': None,
            'newest': None
        }
        
        oldest_time = None
        newest_time = None
        
        for key, cached in self.cache.items():
            cache_type = cached.get('cache_type', 'unknown')
            if cache_type not in stats['by_type']:
                stats['by_type'][cache_type] = 0
            stats['by_type'][cache_type] += 1
            
            cached_time = cached['cached_at']
            if oldest_time is None or cached_time < oldest_time:
                oldest_time = cached_time
            if newest_time is None or cached_time > newest_time:
                newest_time = cached_time
        
        if oldest_time:
            stats['oldest'] = oldest_time.isoformat()
        if newest_time:
            stats['newest'] = newest_time.isoformat()
        
        return stats


dynamic_trigger = DynamicAnalysisTrigger()
batch_fetch_optimizer = BatchFetchOptimizer()
cache_manager = DataCacheManager()


if __name__ == '__main__':
    db = SessionLocal()
    
    triggers = dynamic_trigger.check_all_triggers(db)
    print("触发检查:", json.dumps(triggers, ensure_ascii=False, indent=2, default=str))
    
    db.close()
