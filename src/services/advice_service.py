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
    AdviceReasoning, UserProfile, FundInfo
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
        ).filter(
            Viewpoint.viewpoint_date >= date.today() - timedelta(days=7),
            Viewpoint.is_deleted == False,
            (Viewpoint.valid_until == None) | (Viewpoint.valid_until >= date.today()),
            (Viewpoint.is_summary == True) | (
                (Viewpoint.reasoning.isnot(None)) & (Viewpoint.summary.isnot(None))
            )
        ).first()
        
        data_str = "|".join([
            f"bloggers:{blogger_stats.total}:{float(blogger_stats.avg_accuracy or 0):.2f}:{float(blogger_stats.max_accuracy or 0):.2f}:{blogger_stats.last_update or ''}",
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
        获取生成投资建议所需的数据。

        P0：内部经 AdviceEvidenceBuilder 规范化；
        方向信号唯一入口 filter_actionable_current。
        返回键保持 bloggers/predictions/viewpoints/funds 兼容三阶段与 API。
        额外附带 evidence_pack 审计字段（不影响旧调用方）。
        """
        from src.services.advice_evidence import AdviceEvidenceBuilder

        from src.services.prediction_lifecycle import current_as_of
        pack = AdviceEvidenceBuilder(
            self.db,
            min_accuracy=float(min_accuracy),
            top_bloggers=top_bloggers,
            max_predictions=max_predictions,
            recent_viewpoints_days=recent_viewpoints_days,
            top_viewpoints=top_viewpoints,
        ).build(as_of=current_as_of())

        # 三阶段兼容：blogger 列表保留 name/accuracy/grade 等旧字段
        blogger_list = []
        for b in pack.bloggers:
            blogger_list.append({
                "name": b.get("name"),
                "accuracy_rate": b.get("accuracy_rate") or 0,
                "grade": b.get("grade") or "C",
                "total_predictions": b.get("total_predictions") or 0,
                "correct_predictions": b.get("correct_predictions") or 0,
                "recent_view": "",
                "blogger_id": b.get("blogger_id"),
                "reliability_score": b.get("reliability_score"),
            })

        return {
            "bloggers": blogger_list,
            "predictions": pack.predictions,
            "viewpoints": pack.viewpoints,
            "funds": pack.funds,
            "as_of_date": pack.as_of_date.isoformat(),
            "evidence_hash": pack.evidence_hash,
            "exclusions": pack.exclusions,
            "conflicts": pack.conflicts,
            "meta": pack.meta,
        }

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
