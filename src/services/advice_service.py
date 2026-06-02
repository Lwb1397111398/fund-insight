"""
投资建议服务
处理投资建议相关的业务逻辑
"""
from typing import List, Optional, Dict, Tuple
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, case
import hashlib
import json

from .base import BaseService
from src.models.database import (
    InvestmentAdvice, Blogger, Prediction, Viewpoint, 
    AdviceReasoning, UserProfile, MarketData, FundInfo
)


class AdviceService(BaseService[InvestmentAdvice]):
    """投资建议服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, InvestmentAdvice)
    
    def _calculate_data_hash(self) -> str:
        """
        计算当前数据的哈希值（优化版：使用统计摘要而非全量数据）
        
        使用以下信息计算哈希：
        1. 博主统计：总数、平均准确率、最高准确率
        2. 预测统计：总数、已完成数、最后更新时间
        3. 观点统计：近7天数量、最后更新时间
        
        Returns:
            数据哈希值
        """
        blogger_stats = self.db.query(
            func.count(Blogger.id).label('total'),
            func.avg(Blogger.accuracy_rate).label('avg_accuracy'),
            func.max(Blogger.accuracy_rate).label('max_accuracy'),
            func.max(Blogger.updated_at).label('last_update')
        ).first()
        
        prediction_stats = self.db.query(
            func.count(Prediction.id).label('total'),
            func.sum(case((Prediction.is_expired == True, 1), else_=0)).label('expired'),
            func.max(Prediction.created_at).label('last_update')
        ).filter(Prediction.is_deleted == False).first()
        
        viewpoint_stats = self.db.query(
            func.count(Viewpoint.id).label('total'),
            func.max(Viewpoint.created_at).label('last_update')
        ).filter(Viewpoint.viewpoint_date >= date.today() - timedelta(days=7)).first()
        
        data_str = "|".join([
            f"bloggers:{blogger_stats.total}:{blogger_stats.avg_accuracy or 0:.2f}:{blogger_stats.max_accuracy or 0:.2f}:{blogger_stats.last_update or ''}",
            f"predictions:{prediction_stats.total}:{prediction_stats.expired or 0}:{prediction_stats.last_update or ''}",
            f"viewpoints:{viewpoint_stats.total}:{viewpoint_stats.last_update or ''}"
        ])
        
        return hashlib.md5(data_str.encode()).hexdigest()
    
    def get_latest_advice(self) -> Optional[Dict]:
        """
        获取最新投资建议
        
        Returns:
            投资建议字典或None
        """
        advice = self.db.query(InvestmentAdvice).order_by(
            InvestmentAdvice.advice_date.desc()
        ).first()
        
        if not advice:
            return None
        
        return self._advice_to_dict(advice)
    
    def _advice_to_dict(self, advice: InvestmentAdvice) -> Dict:
        """将投资建议对象转换为字典"""
        return {
            "id": advice.id,
            "advice_date": advice.advice_date.isoformat() if advice.advice_date else None,
            "advice_type": advice.advice_type,
            "advice_content": advice.advice_content,
            "reasoning": advice.reasoning,
            "risk_warning": advice.risk_warning,
            "suggested_sectors": advice.suggested_sectors or [],
            "avoid_sectors": advice.avoid_sectors or [],
            "short_term_advice": advice.short_term_advice or {},
            "mid_term_advice": advice.mid_term_advice or {},
            "avoid_reasoning": advice.avoid_reasoning or "",
            "referenced_bloggers": advice.referenced_bloggers or [],
            "referenced_predictions": advice.referenced_predictions or [],
            "market_sentiment": advice.market_sentiment,
            "confidence": advice.confidence,
            "data_hash": advice.data_hash,
            "created_at": advice.created_at.isoformat() if advice.created_at else None
        }
    
    def check_data_changed(self) -> Tuple[bool, str, Optional[Dict]]:
        """
        检查数据是否发生变化
        
        Returns:
            (是否变化, 当前哈希, 最新建议)
        """
        current_hash = self._calculate_data_hash()
        latest_advice = self.get_latest_advice()
        
        if latest_advice:
            stored_hash = latest_advice.get('data_hash')
            if stored_hash == current_hash:
                return False, current_hash, latest_advice
        
        return True, current_hash, latest_advice
    
    def get_data_for_advice(
        self,
        min_accuracy: int = 50,
        top_bloggers: int = 15,
        max_predictions: int = 30,
        recent_viewpoints_days: int = 7,
        top_viewpoints: int = 50
    ) -> Dict:
        """
        获取生成投资建议所需的数据
        
        Args:
            min_accuracy: 最低准确率要求
            top_bloggers: 顶级博主数量
            max_predictions: 最大预测数量
            recent_viewpoints_days: 近期观点天数
            top_viewpoints: 顶级观点数量
            
        Returns:
            包含博主、预测、观点的数据字典
        """
        bloggers = self.db.query(Blogger).filter(
            Blogger.accuracy_rate >= min_accuracy,
            Blogger.total_predictions >= 3
        ).order_by(Blogger.accuracy_rate.desc()).limit(top_bloggers).all()
        
        if not bloggers:
            bloggers = self.db.query(Blogger).filter(
                Blogger.total_predictions >= 1
            ).order_by(Blogger.accuracy_rate.desc()).limit(top_bloggers).all()
        
        if not bloggers:
            bloggers = self.db.query(Blogger).order_by(
                Blogger.accuracy_rate.desc()
            ).limit(top_bloggers).all()
        
        blogger_list = [
            {
                "name": b.name,
                "accuracy_rate": b.accuracy_rate or 0,
                "grade": b.grade or 'C',
                "total_predictions": b.total_predictions or 0,
                "correct_predictions": b.correct_predictions or 0,
                "recent_view": ""
            }
            for b in bloggers
        ]
        
        near_term_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == False,
            Prediction.is_deleted == False,
            Prediction.target_date >= date.today(),
            Prediction.target_date <= date.today() + timedelta(days=7)
        ).order_by(Prediction.target_date.asc()).all()
        
        mid_term_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == False,
            Prediction.is_deleted == False,
            Prediction.target_date > date.today() + timedelta(days=7),
            Prediction.target_date <= date.today() + timedelta(days=30)
        ).order_by(Prediction.target_date.asc()).limit(20).all()
        
        predictions = near_term_predictions + mid_term_predictions

        # 批量查询博主，避免 N+1
        blogger_ids = list(set(p.blogger_id for p in predictions if p.blogger_id))
        bloggers_map = {}
        if blogger_ids:
            bloggers = self.db.query(Blogger).filter(Blogger.id.in_(blogger_ids)).all()
            bloggers_map = {b.id: b for b in bloggers}

        prediction_list = []
        for p in predictions:
            blogger = bloggers_map.get(p.blogger_id)
            days_to_target = (p.target_date - date.today()).days if p.target_date else 0
            prediction_list.append({
                "blogger_name": blogger.name if blogger else "未知",
                "blogger_id": p.blogger_id,
                "blogger_grade": blogger.grade if blogger else "C",
                "blogger_accuracy": blogger.accuracy_rate if blogger else 0,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content or "",
                "confidence": p.confidence,
                "status": p.status,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "days_to_target": days_to_target,
                "term": "near" if days_to_target <= 7 else "mid"
            })
        
        recent_viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= date.today() - timedelta(days=recent_viewpoints_days),
            Viewpoint.is_deleted == False,
            Viewpoint.is_expired == False
        ).order_by(Viewpoint.viewpoint_date.desc()).limit(top_viewpoints).all()
        
        viewpoint_list = [
            {
                "source": v.source,
                "author": v.author,
                "market_direction": v.market_direction,
                "confidence": v.confidence,
                "credibility_score": v.credibility_score or 50,
                "weight": v.weight or 1.0,
                "sectors_bullish": v.sectors_bullish or [],
                "sectors_bearish": v.sectors_bearish or [],
                "summary": v.summary if v.summary else (v.content[:500] if v.content else ""),
                "reasoning": v.reasoning or "",
                "is_summary": v.is_summary or False
            }
            for v in recent_viewpoints
        ]
        
        funds = self.db.query(FundInfo).filter(
            FundInfo.latest_nav.isnot(None)
        ).order_by(FundInfo.day_growth.desc()).limit(10).all()
        
        fund_list = [
            {
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "sector_type": f.sector_type,
                "day_growth": f.day_growth,
                "week_growth": f.week_growth,
                "month_growth": f.month_growth,
                "ma5": f.ma5,
                "ma10": f.ma10,
                "ma20": f.ma20,
                "sharpe_ratio": f.sharpe_ratio,
                "max_drawdown": f.max_drawdown,
                "support_level": f.support_level,
                "resistance_level": f.resistance_level,
                "vs_sector": f.vs_sector,
                "vs_market": f.vs_market
            }
            for f in funds
        ]
        
        return {
            "bloggers": blogger_list,
            "predictions": prediction_list,
            "viewpoints": viewpoint_list,
            "funds": fund_list
        }
    
    def get_market_data(self) -> Dict:
        """
        获取最新市场数据
        
        Returns:
            市场数据字典
        """
        latest = self.db.query(MarketData).filter(
            MarketData.data_type == 'index'
        ).order_by(MarketData.data_time.desc()).first()
        
        if latest:
            return {
                "index_name": latest.index_name,
                "current_value": latest.current_value,
                "change_pct": latest.change_pct,
                "data_time": latest.data_time.isoformat() if latest.data_time else None
            }
        
        return {}
    
    def get_user_profile(self, user_id: str) -> Optional[Dict]:
        """
        获取用户画像
        
        Args:
            user_id: 用户ID
            
        Returns:
            用户画像字典或None
        """
        if not user_id:
            return None
        
        profile = self.db.query(UserProfile).filter(
            UserProfile.user_id == user_id
        ).first()
        
        if profile:
            return {
                "risk_level": profile.risk_level,
                "investment_period": profile.investment_period,
                "experience_level": profile.experience_level,
                "preferred_sectors": profile.preferred_sectors or [],
                "holdings": profile.holdings or []
            }
        
        return None
    
    def create_advice(
        self,
        advice_type: str,
        advice_content: str,
        market_sentiment: str,
        confidence: int,
        referenced_bloggers: List[str],
        data_hash: str,
        advice_date: Optional[date] = None,
        reasoning: str = None,
        risk_warning: str = None,
        suggested_sectors: List[str] = None,
        avoid_sectors: List[str] = None,
        referenced_predictions: List[Dict] = None,
        short_term_advice: Dict = None,
        mid_term_advice: Dict = None,
        avoid_reasoning: str = None
    ) -> Dict:
        """
        创建投资建议
        
        Args:
            advice_type: 建议类型
            advice_content: 建议内容
            market_sentiment: 市场情绪
            confidence: 信心度
            referenced_bloggers: 引用的博主列表
            data_hash: 数据哈希值
            advice_date: 建议日期（可选）
            reasoning: 建议理由
            risk_warning: 风险提示
            suggested_sectors: 建议板块
            avoid_sectors: 规避板块
            referenced_predictions: 引用的预测列表
            short_term_advice: 短期建议（1-3天）
            mid_term_advice: 中期建议（1-2周）
            avoid_reasoning: 回避理由
            
        Returns:
            创建的投资建议
        """
        db_advice = InvestmentAdvice(
            advice_date=advice_date or date.today(),
            advice_type=advice_type,
            advice_content=advice_content,
            reasoning=reasoning,
            risk_warning=risk_warning,
            suggested_sectors=suggested_sectors or [],
            avoid_sectors=avoid_sectors or [],
            short_term_advice=short_term_advice or {},
            mid_term_advice=mid_term_advice or {},
            avoid_reasoning=avoid_reasoning or "",
            referenced_bloggers=referenced_bloggers or [],
            referenced_predictions=referenced_predictions or [],
            market_sentiment=market_sentiment,
            confidence=confidence,
            data_hash=data_hash
        )
        
        self.db.add(db_advice)
        self.db.commit()
        self.db.refresh(db_advice)
        
        result = self._advice_to_dict(db_advice)
        result["is_new"] = True
        return result
    
    def save_reasoning(
        self,
        advice_id: int,
        supporting_data: List[Dict],
        risk_points: List[Dict],
        weight_distribution: Dict,
        decision_chain: List[str],
        market_state: str
    ) -> AdviceReasoning:
        """
        保存决策依据
        
        Args:
            advice_id: 建议ID
            supporting_data: 支撑数据
            risk_points: 风险点
            weight_distribution: 权重分布
            decision_chain: 决策链
            market_state: 市场状态
            
        Returns:
            决策依据记录
        """
        record = AdviceReasoning(
            advice_id=advice_id,
            supporting_data=supporting_data,
            risk_points=risk_points,
            weight_distribution=weight_distribution,
            decision_chain=decision_chain,
            market_state=market_state
        )
        self.db.add(record)
        self.db.commit()
        return record
    
    def get_advice_history(
        self,
        skip: int = 0,
        limit: int = 30
    ) -> List[Dict]:
        """
        获取投资建议历史
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            投资建议历史列表
        """
        advices = self.db.query(InvestmentAdvice).order_by(
            InvestmentAdvice.advice_date.desc(),
            InvestmentAdvice.created_at.desc()
        ).offset(skip).limit(limit).all()
        
        return [self._advice_to_dict(a) for a in advices]
    
    def get_advice_stats(self) -> Dict:
        """
        获取投资建议统计
        
        Returns:
            统计数据
        """
        total = self.db.query(InvestmentAdvice).count()
        
        today_count = self.db.query(InvestmentAdvice).filter(
            InvestmentAdvice.advice_date == date.today()
        ).count()
        
        recent_advices = self.db.query(InvestmentAdvice).order_by(
            InvestmentAdvice.advice_date.desc()
        ).limit(10).all()
        
        type_distribution = {}
        for a in recent_advices:
            advice_type = a.advice_type or 'unknown'
            type_distribution[advice_type] = type_distribution.get(advice_type, 0) + 1
        
        return {
            "total": total,
            "today_count": today_count,
            "type_distribution": type_distribution
        }
