"""
观点统计与预警服务
包含：多维度统计、观点-基金-预测关联、极端观点预警
"""
import os
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from collections import defaultdict, Counter

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in os.sys.path:
        os.sys.path.insert(0, project_root)

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from src.models.database import Viewpoint, Prediction, FundInfo, SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class SectorViewpointStats:
    sector: str
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    total_count: int = 0
    avg_confidence: float = 0.0
    weighted_sentiment: float = 0.0
    
    @property
    def sentiment_ratio(self) -> float:
        if self.total_count == 0:
            return 0.5
        return self.bullish_count / self.total_count
    
    @property
    def is_extreme(self) -> bool:
        return self.sentiment_ratio >= 0.8 or self.sentiment_ratio <= 0.2


@dataclass
class ViewpointTrend:
    date: str
    bullish_count: int
    bearish_count: int
    neutral_count: int
    total_count: int
    avg_confidence: float


@dataclass
class ExtremeViewpointAlert:
    sector: str
    sentiment_ratio: float
    bullish_count: int
    bearish_count: int
    total_count: int
    alert_type: str
    message: str
    suggested_action: str


class ViewpointStatsService:
    """观点统计服务"""
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
    
    def get_overall_stats(self, days: int = 7) -> Dict:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= cutoff_date
        ).all()
        
        total = len(viewpoints)
        bullish = sum(1 for v in viewpoints if v.market_direction == 'bullish')
        bearish = sum(1 for v in viewpoints if v.market_direction == 'bearish')
        neutral = sum(1 for v in viewpoints if v.market_direction == 'neutral')
        
        source_counts = defaultdict(int)
        for v in viewpoints:
            source_counts[v.source or 'manual'] += 1
        
        avg_confidence = sum(v.confidence or 50 for v in viewpoints) / total if total > 0 else 0
        
        return {
            'period_days': days,
            'total': total,
            'bullish': bullish,
            'bearish': bearish,
            'neutral': neutral,
            'bullish_ratio': bullish / total if total > 0 else 0,
            'bearish_ratio': bearish / total if total > 0 else 0,
            'avg_confidence': round(avg_confidence, 1),
            'by_source': dict(source_counts),
            'crawler_count': sum(source_counts.get(s, 0) for s in ['eastmoney_blog', 'eastmoney_guide', 'sina_finance', 'sina_blog']),
            'manual_count': source_counts.get('manual', 0)
        }
    
    def get_sector_distribution(self, days: int = 7) -> List[SectorViewpointStats]:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= cutoff_date
        ).all()
        
        sector_data = defaultdict(lambda: {
            'bullish': [], 'bearish': [], 'neutral': [],
            'confidences': [], 'weights': []
        })
        
        for v in viewpoints:
            sectors = (v.sectors_bullish or []) + (v.sectors_bearish or [])
            for sector in sectors:
                if v.market_direction == 'bullish':
                    sector_data[sector]['bullish'].append(v)
                elif v.market_direction == 'bearish':
                    sector_data[sector]['bearish'].append(v)
                else:
                    sector_data[sector]['neutral'].append(v)
                
                sector_data[sector]['confidences'].append(v.confidence or 50)
                sector_data[sector]['weights'].append(v.weight or 1.0)
        
        results = []
        for sector, data in sector_data.items():
            bullish_count = len(data['bullish'])
            bearish_count = len(data['bearish'])
            neutral_count = len(data['neutral'])
            total = bullish_count + bearish_count + neutral_count
            
            avg_conf = sum(data['confidences']) / len(data['confidences']) if data['confidences'] else 50
            
            weighted_bullish = sum(
                (v.weight or 1.0) * (v.confidence or 50) / 100
                for v in data['bullish']
            )
            weighted_bearish = sum(
                (v.weight or 1.0) * (v.confidence or 50) / 100
                for v in data['bearish']
            )
            total_weight = weighted_bullish + weighted_bearish
            
            results.append(SectorViewpointStats(
                sector=sector,
                bullish_count=bullish_count,
                bearish_count=bearish_count,
                neutral_count=neutral_count,
                total_count=total,
                avg_confidence=round(avg_conf, 1),
                weighted_sentiment=weighted_bullish / total_weight if total_weight > 0 else 0.5
            ))
        
        return sorted(results, key=lambda x: x.total_count, reverse=True)
    
    def get_sentiment_trend(self, days: int = 7) -> List[ViewpointTrend]:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= cutoff_date
        ).order_by(Viewpoint.viewpoint_date).all()
        
        daily_data = defaultdict(lambda: {
            'bullish': 0, 'bearish': 0, 'neutral': 0,
            'confidences': []
        })
        
        for v in viewpoints:
            date_str = v.viewpoint_date.isoformat() if v.viewpoint_date else 'unknown'
            if v.market_direction == 'bullish':
                daily_data[date_str]['bullish'] += 1
            elif v.market_direction == 'bearish':
                daily_data[date_str]['bearish'] += 1
            else:
                daily_data[date_str]['neutral'] += 1
            daily_data[date_str]['confidences'].append(v.confidence or 50)
        
        results = []
        for date_str in sorted(daily_data.keys()):
            data = daily_data[date_str]
            total = data['bullish'] + data['bearish'] + data['neutral']
            avg_conf = sum(data['confidences']) / len(data['confidences']) if data['confidences'] else 50
            
            results.append(ViewpointTrend(
                date=date_str,
                bullish_count=data['bullish'],
                bearish_count=data['bearish'],
                neutral_count=data['neutral'],
                total_count=total,
                avg_confidence=round(avg_conf, 1)
            ))
        
        return results
    
    def get_source_comparison(self, days: int = 7) -> Dict:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= cutoff_date
        ).all()
        
        source_data = defaultdict(lambda: {
            'total': 0, 'bullish': 0, 'bearish': 0,
            'confidences': [], 'credibility_scores': []
        })
        
        for v in viewpoints:
            source = v.source or 'manual'
            source_data[source]['total'] += 1
            if v.market_direction == 'bullish':
                source_data[source]['bullish'] += 1
            elif v.market_direction == 'bearish':
                source_data[source]['bearish'] += 1
            source_data[source]['confidences'].append(v.confidence or 50)
            if v.credibility_score:
                source_data[source]['credibility_scores'].append(v.credibility_score)
        
        results = {}
        for source, data in source_data.items():
            results[source] = {
                'total': data['total'],
                'bullish': data['bullish'],
                'bearish': data['bearish'],
                'bullish_ratio': data['bullish'] / data['total'] if data['total'] > 0 else 0,
                'avg_confidence': round(sum(data['confidences']) / len(data['confidences']), 1) if data['confidences'] else 50,
                'avg_credibility': round(sum(data['credibility_scores']) / len(data['credibility_scores']), 1) if data['credibility_scores'] else 50
            }
        
        return results


class ViewpointAlertService:
    """观点预警服务"""
    
    EXTREME_THRESHOLD = 0.8
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self.stats_service = ViewpointStatsService(db)
    
    def check_extreme_viewpoints(self, days: int = 7) -> List[ExtremeViewpointAlert]:
        sector_stats = self.stats_service.get_sector_distribution(days)
        
        alerts = []
        for stat in sector_stats:
            if stat.total_count < 3:
                continue
            
            if stat.sentiment_ratio >= self.EXTREME_THRESHOLD:
                alerts.append(ExtremeViewpointAlert(
                    sector=stat.sector,
                    sentiment_ratio=stat.sentiment_ratio,
                    bullish_count=stat.bullish_count,
                    bearish_count=stat.bearish_count,
                    total_count=stat.total_count,
                    alert_type='extreme_bullish',
                    message=f"⚠️ {stat.sector}板块观点高度一致看多（{stat.sentiment_ratio*100:.0f}%），需警惕反向行情",
                    suggested_action="建议谨慎追高，关注获利了结信号"
                ))
            elif stat.sentiment_ratio <= (1 - self.EXTREME_THRESHOLD):
                alerts.append(ExtremeViewpointAlert(
                    sector=stat.sector,
                    sentiment_ratio=stat.sentiment_ratio,
                    bullish_count=stat.bullish_count,
                    bearish_count=stat.bearish_count,
                    total_count=stat.total_count,
                    alert_type='extreme_bearish',
                    message=f"⚠️ {stat.sector}板块观点高度一致看空（{(1-stat.sentiment_ratio)*100:.0f}%），可能存在反弹机会",
                    suggested_action="建议关注超跌反弹机会，分批建仓"
                ))
        
        return alerts
    
    def check_viewpoint_expiry(self) -> List[Dict]:
        today = date.today()
        
        expired_viewpoints = self.db.query(Viewpoint).filter(
            and_(
                Viewpoint.valid_until < today,
                Viewpoint.is_expired == False
            )
        ).all()
        
        results = []
        for v in expired_viewpoints:
            v.is_expired = True
            results.append({
                'viewpoint_id': v.id,
                'author': v.author,
                'sector': (v.sectors_bullish or []) + (v.sectors_bearish or []),
                'expired_date': v.valid_until.isoformat() if v.valid_until else None,
                'action': 'marked_expired'
            })
        
        if results:
            self.db.commit()
            logger.info(f"[Alert] 标记 {len(results)} 个观点为已过期")
        
        return results
    
    def check_reassessment_needed(self) -> List[Dict]:
        today = date.today()
        recent_cutoff = today - timedelta(days=3)
        
        recent_viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= recent_cutoff
        ).all()
        
        sector_viewpoints = defaultdict(list)
        for v in recent_viewpoints:
            for sector in (v.sectors_bullish or []) + (v.sectors_bearish or []):
                sector_viewpoints[sector].append(v)
        
        results = []
        for sector, viewpoints in sector_viewpoints.items():
            if len(viewpoints) >= 5:
                directions = [v.market_direction for v in viewpoints]
                if 'bullish' in directions and 'bearish' in directions:
                    for v in viewpoints:
                        v.needs_reassessment = True
                        v.reassessment_reason = f"{sector}板块近期观点分歧较大，需重新评估"
                        results.append({
                            'viewpoint_id': v.id,
                            'sector': sector,
                            'reason': v.reassessment_reason
                        })
        
        if results:
            self.db.commit()
            logger.info(f"[Alert] 标记 {len(results)} 个观点需要重新评估")
        
        return results


class ViewpointWeightService:
    """观点权重服务"""
    
    SOURCE_AUTHORITY = {
        'eastmoney_blog': 0.8,
        'eastmoney_guide': 0.7,
        'sina_finance': 0.6,
        'sina_blog': 0.5,
        'manual': 1.0
    }
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
    
    def calculate_viewpoint_weight(self, viewpoint: Viewpoint) -> float:
        base_weight = self.SOURCE_AUTHORITY.get(viewpoint.source, 0.5)
        
        if viewpoint.credibility_score:
            if viewpoint.credibility_score >= 80:
                base_weight += 0.1
            elif viewpoint.credibility_score >= 60:
                base_weight += 0.05
        
        if viewpoint.is_vip:
            base_weight += 0.1
        
        if viewpoint.read_count and viewpoint.read_count > 1000:
            base_weight += min(0.05, viewpoint.read_count / 20000)
        
        if viewpoint.score and viewpoint.score >= 8:
            base_weight += 0.05
        
        return min(1.0, base_weight)
    
    def update_all_weights(self, days: int = 30) -> int:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= cutoff_date
        ).all()
        
        updated_count = 0
        for v in viewpoints:
            new_weight = self.calculate_viewpoint_weight(v)
            if v.weight != new_weight:
                v.weight = new_weight
                updated_count += 1
        
        if updated_count > 0:
            self.db.commit()
            logger.info(f"[Weight] 更新了 {updated_count} 个观点的权重")
        
        return updated_count
    
    def get_weighted_sentiment(self, sector: str, days: int = 7) -> Dict:
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = self.db.query(Viewpoint).filter(
            and_(
                Viewpoint.viewpoint_date >= cutoff_date,
                or_(
                    Viewpoint.sectors_bullish.contains([sector]),
                    Viewpoint.sectors_bearish.contains([sector])
                )
            )
        ).all()
        
        weighted_bullish = 0.0
        weighted_bearish = 0.0
        total_weight = 0.0
        
        for v in viewpoints:
            weight = v.weight or 1.0
            confidence = (v.confidence or 50) / 100
            weighted_score = weight * confidence
            
            if v.market_direction == 'bullish':
                weighted_bullish += weighted_score
            elif v.market_direction == 'bearish':
                weighted_bearish += weighted_score
            
            total_weight += weight
        
        return {
            'sector': sector,
            'weighted_bullish': weighted_bullish,
            'weighted_bearish': weighted_bearish,
            'total_weight': total_weight,
            'weighted_sentiment': weighted_bullish / (weighted_bullish + weighted_bearish) if (weighted_bullish + weighted_bearish) > 0 else 0.5,
            'viewpoint_count': len(viewpoints)
        }


class ViewpointLinkService:
    """观点关联服务"""
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
    
    def link_viewpoint_to_fund(self, viewpoint: Viewpoint) -> List[str]:
        linked_funds = []
        
        sectors = (viewpoint.sectors_bullish or []) + (viewpoint.sectors_bearish or [])
        
        for sector in sectors:
            funds = self.db.query(FundInfo).filter(
                FundInfo.sector_type == sector
            ).limit(3).all()
            
            for fund in funds:
                linked_funds.append(fund.fund_code)
        
        return list(set(linked_funds))
    
    def link_viewpoint_to_predictions(self, viewpoint: Viewpoint) -> List[int]:
        if not viewpoint.blogger_id:
            return []
        
        predictions = self.db.query(Prediction).filter(
            and_(
                Prediction.blogger_id == viewpoint.blogger_id,
                Prediction.status == 'pending'
            )
        ).limit(5).all()
        
        return [p.id for p in predictions]
    
    def get_related_viewpoints(self, fund_code: str, days: int = 7) -> List[Dict]:
        cutoff_date = date.today() - timedelta(days=days)
        
        fund = self.db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
        if not fund or not fund.sector_type:
            return []
        
        viewpoints = self.db.query(Viewpoint).filter(
            and_(
                Viewpoint.viewpoint_date >= cutoff_date,
                or_(
                    Viewpoint.sectors_bullish.contains([fund.sector_type]),
                    Viewpoint.sectors_bearish.contains([fund.sector_type])
                )
            )
        ).order_by(Viewpoint.weight.desc()).limit(10).all()
        
        return [v.to_dict() for v in viewpoints]


def get_stats_service(db: Session = None) -> ViewpointStatsService:
    return ViewpointStatsService(db)


def get_alert_service(db: Session = None) -> ViewpointAlertService:
    return ViewpointAlertService(db)


def get_weight_service(db: Session = None) -> ViewpointWeightService:
    return ViewpointWeightService(db)


def get_link_service(db: Session = None) -> ViewpointLinkService:
    return ViewpointLinkService(db)
