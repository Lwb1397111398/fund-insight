"""
本地基金趋势分析引擎
不使用LLM，纯算法分析基金走势
根据预测周期动态调整分析粒度
"""
import json
from datetime import date, timedelta
from typing import List, Dict, Optional


class LocalTrendAnalyzer:
    """本地趋势分析器（不使用LLM）"""
    
    DAY_TREND_THRESHOLD = 0.5
    TOTAL_TREND_THRESHOLD = 2.0
    MIN_PERIOD_DAYS = 1
    MAX_PERIODS = 5
    
    def analyze_trend(self, history: List[Dict], max_periods: int = None) -> Dict:
        """
        分析基金趋势
        
        Args:
            history: 历史净值数据列表，格式 [{"date": "2024-01-01", "nav": 1.0, "day_growth": 0.01}, ...]
            max_periods: 最大阶段数量，默认5个
            
        Returns:
            {
                "trend_summary": "整体趋势总结",
                "periods": [...],
                "up_days": 3,
                "down_days": 5,
                "flat_days": 2,
                "total_change": -3.2
            }
        """
        if not history or len(history) < 3:
            return {
                "trend_summary": "数据不足",
                "periods": [],
                "daily_trends": [],
                "up_days": 0,
                "down_days": 0,
                "flat_days": 0,
                "total_change": 0
            }
        
        if max_periods is None:
            max_periods = self.MAX_PERIODS
        
        sorted_history = sorted(history, key=lambda x: x.get('date', ''))
        
        first_nav = sorted_history[0].get('nav', 0)
        last_nav = sorted_history[-1].get('nav', 0)
        total_change = (last_nav - first_nav) / first_nav * 100 if first_nav else 0
        
        total_days = len(sorted_history)
        
        min_period_days = max(1, total_days // (max_periods * 2))
        
        daily_trends = self._analyze_daily_trends(sorted_history)
        
        up_days = sum(1 for d in daily_trends if d['trend'] == 'up')
        down_days = sum(1 for d in daily_trends if d['trend'] == 'down')
        flat_days = sum(1 for d in daily_trends if d['trend'] == 'flat')
        
        periods = self._identify_periods(daily_trends, min_period_days, max_periods)
        
        trend_summary = self._generate_summary(total_change, periods, up_days, down_days, flat_days, total_days)
        
        return {
            "trend_summary": trend_summary,
            "periods": periods,
            "daily_trends": daily_trends,
            "up_days": up_days,
            "down_days": down_days,
            "flat_days": flat_days,
            "total_change": round(total_change, 2),
            "total_days": total_days
        }
    
    def _analyze_daily_trends(self, history: List[Dict]) -> List[Dict]:
        """分析每日趋势"""
        daily_trends = []
        
        for i, h in enumerate(history):
            day_growth = h.get('day_growth', 0)
            if day_growth is None:
                day_growth = 0
            
            # 判断每日趋势
            if day_growth > self.DAY_TREND_THRESHOLD:
                trend = 'up'
            elif day_growth < -self.DAY_TREND_THRESHOLD:
                trend = 'down'
            else:
                trend = 'flat'
            
            daily_trends.append({
                'date': h.get('date'),
                'change': round(day_growth, 2) if day_growth else 0,
                'trend': trend
            })
        
        return daily_trends
    
    def _identify_periods(self, daily_trends: List[Dict], min_period_days: int = 1, max_periods: int = 5) -> List[Dict]:
        """
        识别连续同趋势的阶段
        
        Args:
            daily_trends: 每日趋势列表
            min_period_days: 最小阶段天数
            max_periods: 最大阶段数量
            
        Returns:
            阶段列表
        """
        if not daily_trends:
            return []
        
        raw_periods = []
        current_trend = daily_trends[0]['trend']
        period_start = 0
        period_changes = [daily_trends[0]['change']]
        
        for i in range(1, len(daily_trends)):
            dt = daily_trends[i]
            
            if dt['trend'] == current_trend:
                period_changes.append(dt['change'])
            else:
                if len(period_changes) >= min_period_days:
                    total_change = sum(period_changes)
                    raw_periods.append({
                        'start_date': daily_trends[period_start]['date'],
                        'end_date': daily_trends[i-1]['date'],
                        'trend': current_trend,
                        'change_percent': round(total_change, 2),
                        'days': len(period_changes),
                        'trend_desc': self._get_trend_desc(current_trend, total_change)
                    })
                
                current_trend = dt['trend']
                period_start = i
                period_changes = [dt['change']]
        
        if len(period_changes) >= min_period_days:
            total_change = sum(period_changes)
            raw_periods.append({
                'start_date': daily_trends[period_start]['date'],
                'end_date': daily_trends[-1]['date'],
                'trend': current_trend,
                'change_percent': round(total_change, 2),
                'days': len(period_changes),
                'trend_desc': self._get_trend_desc(current_trend, total_change)
            })
        
        if len(raw_periods) <= max_periods:
            return raw_periods
        
        return self._merge_periods(raw_periods, max_periods)
    
    def _merge_periods(self, periods: List[Dict], max_periods: int) -> List[Dict]:
        """
        合并相邻阶段，减少阶段数量
        
        策略：合并相邻的相似阶段（如震荡上涨+震荡下跌 → 震荡）
        """
        while len(periods) > max_periods:
            min_change_idx = 0
            min_change = abs(periods[0]['change_percent'])
            
            for i, p in enumerate(periods):
                if abs(p['change_percent']) < min_change:
                    min_change = abs(p['change_percent'])
                    min_change_idx = i
            
            if min_change_idx == 0 and len(periods) > 1:
                next_p = periods[1]
                periods[0]['end_date'] = next_p['end_date']
                periods[0]['change_percent'] = round(periods[0]['change_percent'] + next_p['change_percent'], 2)
                periods[0]['days'] += next_p['days']
                periods[0]['trend_desc'] = self._get_trend_desc(periods[0]['trend'], periods[0]['change_percent'])
                periods.pop(1)
            elif min_change_idx == len(periods) - 1:
                prev_p = periods[-2]
                prev_p['end_date'] = periods[-1]['end_date']
                prev_p['change_percent'] = round(prev_p['change_percent'] + periods[-1]['change_percent'], 2)
                prev_p['days'] += periods[-1]['days']
                prev_p['trend_desc'] = self._get_trend_desc(prev_p['trend'], prev_p['change_percent'])
                periods.pop(-1)
            else:
                prev_p = periods[min_change_idx - 1]
                next_p = periods[min_change_idx + 1]
                
                prev_p['end_date'] = next_p['end_date']
                prev_p['change_percent'] = round(prev_p['change_percent'] + periods[min_change_idx]['change_percent'] + next_p['change_percent'], 2)
                prev_p['days'] += periods[min_change_idx]['days'] + next_p['days']
                prev_p['trend_desc'] = self._get_trend_desc(prev_p['trend'], prev_p['change_percent'])
                
                periods.pop(min_change_idx)
                periods.pop(min_change_idx)
        
        return periods
    
    def _get_trend_desc(self, trend: str, total_change: float) -> str:
        """获取趋势描述"""
        if trend == 'up':
            if total_change > 3:
                return '强势上涨'
            elif total_change > 1:
                return '持续上涨'
            else:
                return '震荡上涨'
        elif trend == 'down':
            if total_change < -3:
                return '强势下跌'
            elif total_change < -1:
                return '持续下跌'
            else:
                return '震荡下跌'
        else:
            return '横盘震荡'
    
    def _generate_summary(self, total_change: float, periods: List[Dict], 
                          up_days: int, down_days: int, flat_days: int, total_days: int = None) -> str:
        """生成趋势总结"""
        if not periods:
            return "数据不足，无法分析"
        
        if total_days is None:
            total_days = up_days + down_days + flat_days
        
        # 整体趋势判断
        if total_change > self.TOTAL_TREND_THRESHOLD:
            overall = f"近{total_days}天上涨{total_change:.1f}%"
        elif total_change < -self.TOTAL_TREND_THRESHOLD:
            overall = f"近{total_days}天下跌{abs(total_change):.1f}%"
        else:
            overall = f"近{total_days}天震荡，涨跌幅{total_change:.1f}%"
        
        # 趋势特征
        if up_days > down_days + flat_days:
            feature = "，以涨为主"
        elif down_days > up_days + flat_days:
            feature = "，以跌为主"
        elif flat_days > up_days + down_days:
            feature = "，横盘为主"
        else:
            feature = "，涨跌互现"
        
        return overall + feature
    
    def should_reanalyze(self, fund_info, history: List[Dict]) -> tuple:
        """判断是否需要重新分析"""
        return True, "每次都分析"


# 单例
local_trend_analyzer = LocalTrendAnalyzer()


def get_local_trend_analyzer():
    return local_trend_analyzer
