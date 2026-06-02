"""
统计服务
处理数据统计相关的业务逻辑
"""
from typing import Dict
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import Blogger, Post, Prediction, Viewpoint, FundInfo


class StatsService:
    """统计服务类"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_overall_stats(self) -> Dict:
        """
        获取整体统计数据
        
        Returns:
            统计数据字典
        """
        # 博主统计
        total_bloggers = self.db.query(Blogger).count()
        active_bloggers = self.db.query(Blogger).filter(
            Blogger.is_active == True
        ).count()
        
        # 帖子统计
        total_posts = self.db.query(Post).count()
        analyzed_posts = self.db.query(Post).filter(
            Post.analyzed == True
        ).count()
        
        # 预测统计
        total_predictions = self.db.query(Prediction).filter(
            Prediction.is_deleted == False
        ).count()
        expired_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_deleted == False
        ).count()
        correct_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_correct == True,
            Prediction.is_deleted == False
        ).count()
        
        accuracy_rate = (
            correct_predictions / expired_predictions * 100
        ) if expired_predictions > 0 else 0
        
        # 基金和观点统计
        total_funds = self.db.query(FundInfo).count()
        total_viewpoints = self.db.query(Viewpoint).filter(
            Viewpoint.is_deleted == False
        ).count()
        total_content = total_posts + total_viewpoints
        
        return {
            "total_bloggers": total_bloggers,
            "active_bloggers": active_bloggers,
            "total_posts": total_posts,
            "analyzed_posts": analyzed_posts,
            "analysis_rate": round(analyzed_posts / total_posts * 100, 1) if total_posts > 0 else 0,
            "total_predictions": total_predictions,
            "pending_predictions": total_predictions - expired_predictions,
            "expired_predictions": expired_predictions,
            "correct_predictions": correct_predictions,
            "incorrect_predictions": expired_predictions - correct_predictions,
            "avg_accuracy": round(accuracy_rate, 2),
            "total_viewpoints": total_viewpoints,
            "total_funds": total_funds,
            "total_content": total_content
        }
    
    def get_blogger_stats(self) -> Dict:
        """
        获取博主统计

        Returns:
            博主统计数据
        """
        total = self.db.query(Blogger).count()

        # 使用 GROUP BY 批量获取等级分布，避免 N+1 查询
        grade_rows = self.db.query(Blogger.grade, func.count(Blogger.id)).filter(
            Blogger.is_active == True
        ).group_by(Blogger.grade).all()
        grade_distribution = {grade: count for grade, count in grade_rows if grade}

        avg_accuracy = self.db.query(func.avg(Blogger.accuracy_rate)).scalar() or 0

        top_bloggers = self.db.query(Blogger).order_by(
            Blogger.accuracy_rate.desc()
        ).limit(5).all()
        
        return {
            "total": total,
            "grade_distribution": grade_distribution,
            "avg_accuracy": round(avg_accuracy, 2),
            "top_bloggers": [
                {
                    "id": b.id,
                    "name": b.name,
                    "accuracy_rate": b.accuracy_rate,
                    "grade": b.grade,
                    "total_predictions": b.total_predictions
                }
                for b in top_bloggers
            ]
        }
    
    def get_prediction_stats(self) -> Dict:
        """
        获取预测统计
        
        Returns:
            预测统计数据
        """
        total = self.db.query(Prediction).filter(
            Prediction.is_deleted == False
        ).count()
        
        status_distribution = {
            "pending": self.db.query(Prediction).filter(
                Prediction.status == 'pending',
                Prediction.is_deleted == False
            ).count(),
            "verified": self.db.query(Prediction).filter(
                Prediction.status == 'success',
                Prediction.is_deleted == False
            ).count(),
            "expired": self.db.query(Prediction).filter(
                Prediction.is_expired == True,
                Prediction.is_deleted == False
            ).count()
        }
        
        type_distribution = {
            "up": self.db.query(Prediction).filter(
                Prediction.prediction_type == 'up',
                Prediction.is_deleted == False
            ).count(),
            "down": self.db.query(Prediction).filter(
                Prediction.prediction_type == 'down',
                Prediction.is_deleted == False
            ).count()
        }
        
        correct = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_correct == True,
            Prediction.is_deleted == False
        ).count()
        
        expired = status_distribution["expired"]
        
        return {
            "total": total,
            "status_distribution": status_distribution,
            "type_distribution": type_distribution,
            "accuracy": round(correct / expired * 100, 2) if expired > 0 else 0,
            "correct_count": correct,
            "incorrect_count": expired - correct
        }
    
    def get_content_stats(self) -> Dict:
        """
        获取内容统计

        Returns:
            内容统计数据
        """
        total_posts = self.db.query(Post).count()
        total_viewpoints = self.db.query(Viewpoint).filter(Viewpoint.is_deleted == False).count()

        # 使用 GROUP BY 批量获取来源分布，避免 N+1 查询
        source_rows = self.db.query(Viewpoint.source, func.count(Viewpoint.id)).filter(
            Viewpoint.is_deleted == False,
            Viewpoint.source.isnot(None)
        ).group_by(Viewpoint.source).all()
        source_distribution = {source: count for source, count in source_rows if source}
        
        # 市场情绪分布
        sentiment_distribution = {
            "bullish": self.db.query(Viewpoint).filter(
                Viewpoint.market_direction == 'bullish',
                Viewpoint.is_deleted == False
            ).count(),
            "bearish": self.db.query(Viewpoint).filter(
                Viewpoint.market_direction == 'bearish',
                Viewpoint.is_deleted == False
            ).count(),
            "neutral": self.db.query(Viewpoint).filter(
                Viewpoint.market_direction == 'neutral',
                Viewpoint.is_deleted == False
            ).count()
        }
        
        return {
            "total_posts": total_posts,
            "total_viewpoints": total_viewpoints,
            "total_content": total_posts + total_viewpoints,
            "source_distribution": source_distribution,
            "sentiment_distribution": sentiment_distribution
        }
    
    def get_fund_stats(self) -> Dict:
        """
        获取基金统计

        Returns:
            基金统计数据
        """
        total = self.db.query(FundInfo).count()

        # 使用 GROUP BY 批量获取板块分布，避免 N+1 查询
        sector_rows = self.db.query(FundInfo.sector_type, func.count(FundInfo.id)).filter(
            FundInfo.sector_type.isnot(None)
        ).group_by(FundInfo.sector_type).all()
        sector_distribution = {sector: count for sector, count in sector_rows if sector}
        
        # 有活跃预测的基金
        active_funds = self.db.query(FundInfo).filter(
            FundInfo.active_predictions > 0
        ).count()
        
        # 平均涨跌幅
        avg_day_growth = self.db.query(func.avg(FundInfo.day_growth)).scalar() or 0
        avg_week_growth = self.db.query(func.avg(FundInfo.week_growth)).scalar() or 0
        avg_month_growth = self.db.query(func.avg(FundInfo.month_growth)).scalar() or 0
        
        # 基金历史数据统计
        from src.models.database import FundHistory
        fund_history_count = self.db.query(FundHistory).count()
        
        return {
            "total": total,
            "fund_count": total,
            "active_funds": active_funds,
            "sector_distribution": sector_distribution,
            "avg_day_growth": round(avg_day_growth, 2),
            "avg_week_growth": round(avg_week_growth, 2),
            "avg_month_growth": round(avg_month_growth, 2),
            "fund_history_count": fund_history_count
        }
    
    def get_all_stats(self) -> Dict:
        """
        获取所有统计数据
        
        Returns:
            完整统计数据
        """
        return {
            "success": True,
            "data": {
                "overall": self.get_overall_stats(),
                "bloggers": self.get_blogger_stats(),
                "predictions": self.get_prediction_stats(),
                "content": self.get_content_stats(),
                "funds": self.get_fund_stats()
            }
        }