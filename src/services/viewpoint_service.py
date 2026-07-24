"""
观点服务
处理观点相关的业务逻辑
"""
from typing import List, Optional, Dict
from datetime import date, timedelta, datetime
from sqlalchemy.orm import Session
from sqlalchemy import func

from .base import BaseService
from src.models.database import Viewpoint


SOURCE_AUTHORITY_MAP = {
    'eastmoney_blog': 0.8,
    'eastmoney_guide': 0.85,
    'sina_finance': 0.75,
    'sina_blog': 0.7,
    'xueqiu': 0.65,
    'manual': 0.6,
    'crawler': 0.5
}


def get_source_authority(source: str) -> float:
    """获取来源可信度"""
    return SOURCE_AUTHORITY_MAP.get(source, 0.5)


class ViewpointService(BaseService[Viewpoint]):
    """观点服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, Viewpoint)
    
    def get_by_blogger(self, blogger_id: int, skip: int = 0, limit: int = 100) -> List[Viewpoint]:
        """
        获取博主的观点列表
        
        Args:
            blogger_id: 博主 ID
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.blogger_id == blogger_id,
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_source(self, source: str, skip: int = 0, limit: int = 100) -> List[Viewpoint]:
        """
        根据来源获取观点
        
        Args:
            source: 来源（manual/crawler）
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.source == source,
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_direction(self, direction: str, skip: int = 0, limit: int = 100) -> List[Viewpoint]:
        """
        根据市场方向获取观点
        
        Args:
            direction: 市场方向（bullish/bearish/neutral）
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.market_direction == direction,
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_active(self, skip: int = 0, limit: int = 100) -> List[Viewpoint]:
        """
        获取有效观点（未过期）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            有效观点列表
        """
        today = date.today()
        return self.db.query(Viewpoint).filter(
            (Viewpoint.valid_until == None) | (Viewpoint.valid_until >= today),
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_expired(self) -> List[Viewpoint]:
        """
        获取过期观点
        
        Returns:
            过期观点列表
        """
        today = date.today()
        return self.db.query(Viewpoint).filter(
            Viewpoint.valid_until < today,
            Viewpoint.is_deleted == False
        ).all()
    
    def get_by_fund(self, fund_code: str, skip: int = 0, limit: int = 100) -> List[Viewpoint]:
        """
        根据基金获取观点
        
        Args:
            fund_code: 基金代码
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.fund_code == fund_code,
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_recent(self, days: int = 7, limit: int = 50) -> List[Viewpoint]:
        """
        获取最近的观点
        
        Args:
            days: 天数
            limit: 返回数量
            
        Returns:
            最近观点列表
        """
        start_date = date.today() - timedelta(days=days)
        return self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= start_date,
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).limit(limit).all()
    
    def get_stats(self) -> Dict:
        """
        获取观点统计
        
        Returns:
            统计数据
        """
        active_filter = Viewpoint.is_deleted == False
        total = self.db.query(func.count(Viewpoint.id)).filter(active_filter).scalar()
        bullish = self.db.query(func.count(Viewpoint.id)).filter(
            active_filter,
            Viewpoint.market_direction == 'bullish'
        ).scalar()
        bearish = self.db.query(func.count(Viewpoint.id)).filter(
            active_filter,
            Viewpoint.market_direction == 'bearish'
        ).scalar()
        neutral = self.db.query(func.count(Viewpoint.id)).filter(
            active_filter,
            Viewpoint.market_direction == 'neutral'
        ).scalar()

        crawler = self.db.query(func.count(Viewpoint.id)).filter(
            active_filter,
            Viewpoint.source == 'crawler'
        ).scalar()
        manual = self.db.query(func.count(Viewpoint.id)).filter(
            active_filter,
            Viewpoint.source == 'manual'
        ).scalar()
        
        return {
            "total": total,
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "crawler": crawler,
            "manual": manual
        }
    
    def search(self, keyword: str, skip: int = 0, limit: int = 20) -> List[Viewpoint]:
        """
        搜索观点
        
        Args:
            keyword: 搜索关键词
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            匹配的观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.content.contains(keyword),
            Viewpoint.is_deleted == False
        ).order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_viewpoints_with_filters(
        self, 
        skip: int = 0, 
        limit: int = 100,
        source: Optional[str] = None,
        market_direction: Optional[str] = None
    ) -> List[Viewpoint]:
        """
        根据条件获取观点列表
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            source: 来源筛选
            market_direction: 市场方向筛选
            
        Returns:
            观点列表
        """
        query = self.db.query(Viewpoint).filter(Viewpoint.is_deleted == False)
        
        if source:
            query = query.filter(Viewpoint.source == source)
        if market_direction:
            query = query.filter(Viewpoint.market_direction == market_direction)
        
        return query.order_by(Viewpoint.viewpoint_date.desc()).offset(skip).limit(limit).all()
    
    def get_viewpoint_by_id(self, viewpoint_id: int) -> Optional[Viewpoint]:
        """
        根据 ID 获取观点
        
        Args:
            viewpoint_id: 观点 ID
            
        Returns:
            观点实例或 None
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.id == viewpoint_id,
            Viewpoint.is_deleted == False,
        ).first()
    
    def delete_viewpoint(self, viewpoint_id: int) -> bool:
        """
        删除观点（软删除）

        Args:
            viewpoint_id: 观点 ID

        Returns:
            是否删除成功
        """
        viewpoint = self.get_viewpoint_by_id(viewpoint_id)
        if not viewpoint:
            return False

        viewpoint.is_deleted = True
        viewpoint.deleted_at = datetime.now()
        self.db.commit()
        return True
    
    def get_viewpoints_for_batch_analyze(
        self, 
        limit: int = 10, 
        source: str = 'all',
        days: int = 7
    ) -> List[Viewpoint]:
        """
        获取需要批量分析的观点
        
        Args:
            limit: 最多返回数量
            source: 来源筛选
            days: 最近天数
            
        Returns:
            需要分析的观点列表
        """
        start_date = date.today() - timedelta(days=days)
        
        query = self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= start_date,
            Viewpoint.is_deleted == False
        )
        
        if source != 'all':
            query = query.filter(Viewpoint.source == source)
        
        viewpoints = query.order_by(Viewpoint.created_at.desc()).limit(limit * 2).all()
        
        # 过滤出需要分析的（没有 reasoning 或不包含"AI深度分析"）
        result = []
        for v in viewpoints:
            if not v.reasoning or "AI深度分析" not in v.reasoning:
                result.append(v)
            if len(result) >= limit:
                break
        
        return result
    
    def update_viewpoint_analysis(
        self,
        viewpoint_id: int,
        market_direction: str,
        confidence: int,
        sectors_bullish: List[str],
        sectors_bearish: List[str],
        reasoning: str,
        time_horizon: str,
        validity_period: str,
        valid_until: date,
        summary: str = None,
        credibility: int = None,
        key_points: List[str] = None,
        action_suggestion: str = None,
        risk_level: str = None,
        sentiment_score: float = None,
        db: Session = None
    ) -> Optional[Viewpoint]:
        """
        更新观点分析结果
        
        Args:
            viewpoint_id: 观点 ID
            market_direction: 市场方向
            confidence: 信心度
            sectors_bullish: 看多板块
            sectors_bearish: 看空板块
            reasoning: 分析理由
            time_horizon: 时间范围
            validity_period: 有效期
            valid_until: 有效截止日期
            summary: 一句话摘要
            credibility: 可信度分数
            key_points: 关键要点
            action_suggestion: 操作建议
            risk_level: 风险等级
            sentiment_score: 情绪分数
            
        Returns:
            更新后的观点实例或 None
        """
        # 使用传入的数据库会话，如果未提供则使用实例的会话
        session = db if db is not None else self.db

        # 需要在传入的会话中重新查询对象
        from src.models.database import Viewpoint as ViewpointModel
        viewpoint = session.query(ViewpointModel).filter(ViewpointModel.id == viewpoint_id).first()
        if not viewpoint:
            return None

        viewpoint.market_direction = market_direction
        viewpoint.confidence = confidence
        viewpoint.sectors_bullish = sectors_bullish
        viewpoint.sectors_bearish = sectors_bearish
        viewpoint.reasoning = reasoning
        viewpoint.time_horizon = time_horizon
        viewpoint.validity_period = validity_period
        viewpoint.valid_until = valid_until
        if summary:
            viewpoint.summary = summary
        if credibility is not None:
            viewpoint.credibility_score = credibility
        if key_points:
            viewpoint.tags = key_points
        if action_suggestion:
            viewpoint.action_suggestion = action_suggestion
        if risk_level:
            viewpoint.risk_level = risk_level
        if sentiment_score is not None:
            viewpoint.score = sentiment_score

        if viewpoint.source and not viewpoint.source_authority:
            viewpoint.source_authority = get_source_authority(viewpoint.source)

        if viewpoint.credibility_score:
            viewpoint.weight = viewpoint.calculate_weight()

        # 注意：不在这里提交，由调用方负责提交
        return viewpoint
    
    def get_pending_summary_dates(self) -> List[Dict]:
        """
        获取待汇总的日期列表（今天以前的日期）
        
        Returns:
            待汇总日期列表，每个元素包含日期和观点数量
        """
        from datetime import date as date_type
        
        today = date_type.today()
        
        result = self.db.query(
            Viewpoint.viewpoint_date,
            func.count(Viewpoint.id).label('count')
        ).filter(
            Viewpoint.viewpoint_date < today,
            Viewpoint.is_deleted == False,
            Viewpoint.is_summary == False
        ).group_by(
            Viewpoint.viewpoint_date
        ).order_by(
            Viewpoint.viewpoint_date.desc()
        ).all()
        
        return [
            {
                "date": row.viewpoint_date.isoformat(),
                "count": row.count
            }
            for row in result
        ]
    
    def get_viewpoints_by_date(self, target_date) -> List[Viewpoint]:
        """
        获取指定日期的所有非汇总观点
        
        Args:
            target_date: 目标日期
            
        Returns:
            观点列表
        """
        return self.db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date == target_date,
            Viewpoint.is_deleted == False,
            Viewpoint.is_summary == False
        ).all()
    
    def create_summary_viewpoint(
        self,
        viewpoint_date,
        content: str,
        market_direction: str,
        confidence: int,
        topics: List[Dict],
        sectors_bullish: List[str],
        sectors_bearish: List[str],
        reasoning: str,
        original_count: int,
        original_ids: List[int]
    ) -> Viewpoint:
        """
        创建汇总观点
        
        Args:
            viewpoint_date: 观点日期
            content: 详细汇总内容
            market_direction: 市场方向
            confidence: 信心度
            topics: 主题列表
            sectors_bullish: 看多板块
            sectors_bearish: 看空板块
            reasoning: 分析理由
            original_count: 原观点数量
            original_ids: 原观点ID列表
            
        Returns:
            创建的汇总观点
        """
        summary_viewpoint = Viewpoint(
            viewpoint_date=viewpoint_date,
            source="daily_summary",
            author="系统汇总",
            content=content,
            summary=None,
            market_direction=market_direction,
            confidence=confidence,
            topics=topics,
            sectors_bullish=sectors_bullish,
            sectors_bearish=sectors_bearish,
            reasoning=reasoning,
            is_summary=True,
            original_count=original_count,
            original_ids=original_ids,
            credibility_score=75,
            weight=1.0,
            time_horizon="medium",
            validity_period="7天"
        )
        
        self.db.add(summary_viewpoint)
        self.db.commit()
        self.db.refresh(summary_viewpoint)
        
        return summary_viewpoint
    
    def delete_viewpoints_by_ids(self, viewpoint_ids: List[int]) -> int:
        """
        批量删除观点（软删除）

        Args:
            viewpoint_ids: 观点ID列表

        Returns:
            删除的数量
        """
        if not viewpoint_ids:
            return 0

        deleted_count = self.db.query(Viewpoint).filter(
            Viewpoint.id.in_(viewpoint_ids),
            Viewpoint.is_deleted == False
        ).update({
            Viewpoint.is_deleted: True,
            Viewpoint.deleted_at: datetime.now()
        }, synchronize_session=False)

        self.db.commit()
        return deleted_count
    
    def get_summary_stats(self) -> Dict:
        """
        获取汇总统计信息
        
        Returns:
            统计信息
        """
        from datetime import date as date_type
        
        today = date_type.today()
        
        pending_dates = self.get_pending_summary_dates()
        total_pending = sum(d['count'] for d in pending_dates)
        
        total_summaries = self.db.query(func.count(Viewpoint.id)).filter(
            Viewpoint.is_summary == True,
            Viewpoint.is_deleted == False
        ).scalar()
        
        return {
            "pending_dates": pending_dates,
            "total_pending_viewpoints": total_pending,
            "total_summaries": total_summaries
        }
