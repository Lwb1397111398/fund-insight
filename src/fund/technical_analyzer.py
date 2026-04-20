"""
基金技术分析模块

功能：
1. 技术指标计算（支撑位/压力位/均线/回撤）
2. 相对表现分析（vs板块/vs大盘）
3. 动态分析触发
4. 趋势分析历史记录
"""
import json
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.models.database import (
    FundInfo, FundHistory, SessionLocal
)


class TechnicalIndicatorCalculator:
    """
    技术指标计算器
    
    计算指标：
    - 支撑位/压力位
    - 移动均线（MA5/MA10/MA20）
    - 最大回撤
    - 波动率
    - 相对强弱
    """
    
    def calculate_all_indicators(self, history: List[Dict]) -> Dict:
        """
        计算所有技术指标
        
        Args:
            history: 历史净值列表 [{'date': date, 'nav': float, 'growth': float}, ...]
        
        Returns:
            {
                'support_level': 支撑位,
                'resistance_level': 压力位,
                'ma5': 5日均线,
                'ma10': 10日均线,
                'ma20': 20日均线,
                'max_drawdown': 最大回撤,
                'volatility': 波动率,
                'rsi': 相对强弱指标,
                'trend': 趋势方向
            }
        """
        if not history or len(history) < 5:
            return {
                'support_level': None,
                'resistance_level': None,
                'ma5': None,
                'ma10': None,
                'ma20': None,
                'max_drawdown': None,
                'volatility': None,
                'rsi': None,
                'trend': None
            }
        
        navs = [h['nav'] for h in history if h.get('nav')]
        growths = [h.get('growth', 0) for h in history if h.get('growth') is not None]
        
        result = {}
        
        result['support_level'] = self._calc_support_level(navs)
        result['resistance_level'] = self._calc_resistance_level(navs)
        
        result['ma5'] = self._calc_ma(navs, 5)
        result['ma10'] = self._calc_ma(navs, 10)
        result['ma20'] = self._calc_ma(navs, 20)
        
        result['max_drawdown'] = self._calc_max_drawdown(navs)
        
        result['volatility'] = self._calc_volatility(growths)
        
        result['rsi'] = self._calc_rsi(growths, 14)
        
        result['trend'] = self._determine_trend(navs, result)
        
        return result
    
    def _calc_support_level(self, navs: List[float], window: int = 20) -> float:
        """计算支撑位（近期最低点）"""
        if len(navs) < window:
            return min(navs) if navs else None
        return min(navs[:window])
    
    def _calc_resistance_level(self, navs: List[float], window: int = 20) -> float:
        """计算压力位（近期最高点）"""
        if len(navs) < window:
            return max(navs) if navs else None
        return max(navs[:window])
    
    def _calc_ma(self, navs: List[float], period: int) -> float:
        """计算移动均线"""
        if len(navs) < period:
            return None
        return round(sum(navs[:period]) / period, 4)
    
    def _calc_max_drawdown(self, navs: List[float]) -> float:
        """
        计算最大回撤
        
        最大回撤 = (历史最高点 - 之后最低点) / 历史最高点
        """
        if not navs or len(navs) < 2:
            return None
        
        max_drawdown = 0
        peak = navs[-1]
        
        for nav in reversed(navs):
            if nav > peak:
                peak = nav
            
            drawdown = (peak - nav) / peak if peak > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        return round(max_drawdown * 100, 2)
    
    def _calc_volatility(self, growths: List[float], annualize: bool = True) -> float:
        """
        计算波动率（标准差）
        
        Args:
            growths: 日涨跌幅列表
            annualize: 是否年化
        """
        if not growths or len(growths) < 5:
            return None
        
        mean = sum(growths) / len(growths)
        variance = sum((g - mean) ** 2 for g in growths) / len(growths)
        std = variance ** 0.5
        
        if annualize:
            std = std * (252 ** 0.5)
        
        return round(std, 2)
    
    def _calc_rsi(self, growths: List[float], period: int = 14) -> float:
        """
        计算RSI相对强弱指标
        
        RSI = 100 - 100 / (1 + RS)
        RS = 平均上涨幅度 / 平均下跌幅度
        """
        if not growths or len(growths) < period:
            return None
        
        recent_growths = growths[:period]
        
        gains = [g for g in recent_growths if g > 0]
        losses = [-g for g in recent_growths if g < 0]
        
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 2)
    
    def _determine_trend(self, navs: List[float], indicators: Dict) -> str:
        """
        判断趋势方向
        
        综合考虑：
        - 均线排列
        - 当前位置
        - RSI指标
        """
        if not navs or len(navs) < 5:
            return 'unknown'
        
        current_nav = navs[0]
        ma5 = indicators.get('ma5')
        ma10 = indicators.get('ma10')
        ma20 = indicators.get('ma20')
        rsi = indicators.get('rsi')
        
        score = 0
        
        if ma5 and ma10:
            if ma5 > ma10:
                score += 1
            else:
                score -= 1
        
        if ma10 and ma20:
            if ma10 > ma20:
                score += 1
            else:
                score -= 1
        
        if ma5 and current_nav:
            if current_nav > ma5:
                score += 1
            else:
                score -= 1
        
        if rsi:
            if rsi > 70:
                score -= 1
            elif rsi < 30:
                score += 1
        
        if score >= 2:
            return 'up'
        elif score <= -2:
            return 'down'
        else:
            return 'flat'


class RelativePerformanceAnalyzer:
    """
    相对表现分析器
    
    分析：
    - 基金 vs 板块指数
    - 基金 vs 大盘指数
    - 相关性分析
    """
    
    MARKET_INDEX = '110020'
    
    SECTOR_INDEX_MAP = {
        '白酒': '161725',
        '新能源': '516790',
        '半导体': '512480',
        '医药': '512010',
        '人工智能': '015719',
        '光伏': '013013',
        '军工': '512660',
        '消费': '159928',
        '有色金属': '160221',
        '银行': '512800',
        '券商': '512880',
        '沪深300': '110020'
    }
    
    def analyze_relative_performance(self, fund_code: str, sector: str,
                                     start_date: date, end_date: date,
                                     db: Session) -> Dict:
        """
        分析相对表现
        
        Returns:
            {
                'fund_change': 基金涨跌幅,
                'sector_change': 板块涨跌幅,
                'market_change': 大盘涨跌幅,
                'vs_sector': 跑赢板块,
                'vs_market': 跑赢大盘,
                'performance_type': 表现类型,
                'correlation_sector': 与板块相关性
            }
        """
        result = {
            'fund_change': None,
            'sector_change': None,
            'market_change': None,
            'vs_sector': None,
            'vs_market': None,
            'performance_type': None,
            'correlation_sector': None
        }
        
        fund_history = self._get_nav_history(fund_code, start_date, end_date, db)
        if fund_history:
            result['fund_change'] = self._calc_period_change(fund_history)
        
        sector_index = self.SECTOR_INDEX_MAP.get(sector)
        if sector_index:
            sector_history = self._get_nav_history(sector_index, start_date, end_date, db)
            if sector_history:
                result['sector_change'] = self._calc_period_change(sector_history)
                result['correlation_sector'] = self._calc_correlation(fund_history, sector_history)
        
        market_history = self._get_nav_history(self.MARKET_INDEX, start_date, end_date, db)
        if market_history:
            result['market_change'] = self._calc_period_change(market_history)
        
        if result['fund_change'] is not None:
            if result['sector_change'] is not None:
                result['vs_sector'] = round(result['fund_change'] - result['sector_change'], 2)
            if result['market_change'] is not None:
                result['vs_market'] = round(result['fund_change'] - result['market_change'], 2)
        
        result['performance_type'] = self._determine_performance_type(result)
        
        return result
    
    def _get_nav_history(self, fund_code: str, start_date: date, end_date: date,
                        db: Session) -> List[Dict]:
        """获取净值历史"""
        records = db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code,
            FundHistory.nav_date >= start_date,
            FundHistory.nav_date <= end_date
        ).order_by(FundHistory.nav_date.asc()).all()
        
        return [{'date': r.nav_date, 'nav': r.nav} for r in records if r.nav]
    
    def _calc_period_change(self, history: List[Dict]) -> float:
        """计算区间涨跌幅"""
        if not history or len(history) < 2:
            return None
        
        start_nav = history[0]['nav']
        end_nav = history[-1]['nav']
        
        if start_nav <= 0:
            return None
        
        return round((end_nav - start_nav) / start_nav * 100, 2)
    
    def _calc_correlation(self, history1: List[Dict], history2: List[Dict]) -> float:
        """计算相关性"""
        if not history1 or not history2:
            return None
        
        changes1 = self._calc_daily_changes(history1)
        changes2 = self._calc_daily_changes(history2)
        
        if not changes1 or not changes2 or len(changes1) != len(changes2):
            return None
        
        n = len(changes1)
        mean1 = sum(changes1) / n
        mean2 = sum(changes2) / n
        
        numerator = sum((changes1[i] - mean1) * (changes2[i] - mean2) for i in range(n))
        denominator = (
            sum((c - mean1) ** 2 for c in changes1) ** 0.5 *
            sum((c - mean2) ** 2 for c in changes2) ** 0.5
        )
        
        if denominator == 0:
            return None
        
        return round(numerator / denominator, 2)
    
    def _calc_daily_changes(self, history: List[Dict]) -> List[float]:
        """计算日涨跌幅"""
        changes = []
        for i in range(1, len(history)):
            if history[i-1]['nav'] > 0:
                change = (history[i]['nav'] - history[i-1]['nav']) / history[i-1]['nav'] * 100
                changes.append(change)
        return changes
    
    def _determine_performance_type(self, result: Dict) -> str:
        """
        判断表现类型
        
        - follow: 跟随板块
        - independent: 独立走势
        - outperform: 跑赢
        - underperform: 跑输
        """
        vs_sector = result.get('vs_sector')
        vs_market = result.get('vs_market')
        correlation = result.get('correlation_sector')
        
        if vs_sector is None and vs_market is None:
            return 'unknown'
        
        if correlation is not None and correlation > 0.8:
            if vs_sector and abs(vs_sector) < 2:
                return 'follow'
        
        if vs_sector is not None:
            if vs_sector > 5:
                return 'outperform'
            elif vs_sector < -5:
                return 'underperform'
        
        if vs_market is not None:
            if vs_market > 3 and (vs_sector is None or vs_sector > 0):
                return 'outperform'
            elif vs_market < -3 and (vs_sector is None or vs_sector < 0):
                return 'underperform'
        
        return 'independent'


class TrendHistoryManager:
    """
    趋势历史管理器
    
    功能：
    - 保存趋势分析历史（已禁用）
    - 生成趋势变化曲线（已禁用）
    - 分析预测准确度（已禁用）
    
    注意：FundTrendAnalysisHistory 表已被移除，此管理器功能已禁用
    """
    
    def save_trend_analysis(self, fund_code: str, analysis_result: Dict,
                           data_days: int, db: Session = None) -> Dict:
        """保存趋势分析结果（已禁用，直接返回分析结果）"""
        return {
            'fund_code': fund_code,
            'analysis_date': date.today().isoformat(),
            'trend_direction': analysis_result.get('trend'),
            'trend_strength': analysis_result.get('strength', 50),
            'confidence': analysis_result.get('confidence', 50),
            'data_days': data_days
        }
    
    def get_trend_evolution(self, fund_code: str, days: int = 30,
                           db: Session = None) -> Dict:
        """
        获取趋势演变曲线（已禁用）
        
        Returns:
            {
                'trend_changes': [],
                'summary': {'message': '趋势历史功能已禁用'}
            }
        """
        return {
            'trend_changes': [],
            'summary': {'message': '趋势历史功能已禁用'}
        }


technical_calculator = TechnicalIndicatorCalculator()
relative_performance_analyzer = RelativePerformanceAnalyzer()
trend_history_manager = TrendHistoryManager()


if __name__ == '__main__':
    calculator = TechnicalIndicatorCalculator()
    
    test_history = [
        {'date': date(2024, 1, 15), 'nav': 1.5, 'growth': 1.0},
        {'date': date(2024, 1, 14), 'nav': 1.48, 'growth': -0.5},
        {'date': date(2024, 1, 13), 'nav': 1.49, 'growth': 0.3},
        {'date': date(2024, 1, 12), 'nav': 1.485, 'growth': -0.2},
        {'date': date(2024, 1, 11), 'nav': 1.488, 'growth': 0.1},
    ]
    
    indicators = calculator.calculate_all_indicators(test_history)
    print("技术指标:", json.dumps(indicators, ensure_ascii=False, indent=2, default=str))
