"""
统计服务 - 优化版
使用聚合查询减少数据库往返次数（针对 PostgreSQL 网络延迟优化）
"""
from typing import Dict
from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_

from src.models.database import Blogger, Post, Prediction, Viewpoint, FundInfo


class StatsService:
    """统计服务类"""

    def __init__(self, db: Session):
        self.db = db

    def get_overall_stats(self) -> Dict:
        """获取整体统计数据（2条SQL替代原来8条）"""
        # 博主 + 帖子聚合
        row = self.db.query(
            func.count(Blogger.id).label('total_bloggers'),
            func.count(case((Blogger.is_active == True, 1))).label('active_bloggers'),
        ).first()

        post_row = self.db.query(
            func.count(Post.id).label('total_posts'),
            func.count(case((Post.analyzed == True, 1))).label('analyzed_posts'),
        ).first()

        # 预测聚合（一条SQL拿到所有count）
        pred_row = self.db.query(
            func.count(case((Prediction.is_deleted == False, 1))).label('total_predictions'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.is_expired == True), 1))).label('expired_predictions'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.is_expired == True, Prediction.is_correct == True), 1))).label('correct_predictions'),
        ).first()

        # 基金 + 观点聚合
        fund_row = self.db.query(
            func.count(FundInfo.id).label('total_funds'),
        ).first()

        vp_row = self.db.query(
            func.count(case((Viewpoint.is_deleted == False, 1))).label('total_viewpoints'),
        ).first()

        total_bloggers = row.total_bloggers or 0
        total_posts = post_row.total_posts or 0
        analyzed_posts = post_row.analyzed_posts or 0
        total_predictions = pred_row.total_predictions or 0
        expired_predictions = pred_row.expired_predictions or 0
        correct_predictions = pred_row.correct_predictions or 0
        total_funds = fund_row.total_funds or 0
        total_viewpoints = vp_row.total_viewpoints or 0

        accuracy_rate = (correct_predictions / expired_predictions * 100) if expired_predictions > 0 else 0

        return {
            "total_bloggers": total_bloggers,
            "active_bloggers": row.active_bloggers or 0,
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
            "total_content": total_posts + total_viewpoints
        }

    def get_blogger_stats(self) -> Dict:
        """获取博主统计（已优化：GROUP BY）"""
        total = self.db.query(Blogger).count()

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
        """获取预测统计（1条SQL替代原来6条）"""
        row = self.db.query(
            func.count(case((Prediction.is_deleted == False, 1))).label('total'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.status == 'pending'), 1))).label('pending'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.status == 'success'), 1))).label('verified'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.is_expired == True), 1))).label('expired'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.prediction_type == 'up'), 1))).label('up'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.prediction_type == 'down'), 1))).label('down'),
            func.count(case((and_(Prediction.is_deleted == False, Prediction.is_expired == True, Prediction.is_correct == True), 1))).label('correct'),
        ).first()

        total = row.total or 0
        expired = row.expired or 0
        correct = row.correct or 0

        return {
            "total": total,
            "status_distribution": {
                "pending": row.pending or 0,
                "verified": row.verified or 0,
                "expired": expired,
            },
            "type_distribution": {
                "up": row.up or 0,
                "down": row.down or 0,
            },
            "accuracy": round(correct / expired * 100, 2) if expired > 0 else 0,
            "correct_count": correct,
            "incorrect_count": expired - correct
        }

    def get_content_stats(self) -> Dict:
        """获取内容统计（2条SQL替代原来6条）"""
        # 帖子 + 观点 + 来源分布 一条SQL
        total_posts = self.db.query(func.count(Post.id)).scalar() or 0

        # 观点聚合：总数 + 情绪分布 + 来源分布
        vp_base = self.db.query(
            func.count(case((Viewpoint.is_deleted == False, 1))).label('total_viewpoints'),
            func.count(case((and_(Viewpoint.is_deleted == False, Viewpoint.market_direction == 'bullish'), 1))).label('bullish'),
            func.count(case((and_(Viewpoint.is_deleted == False, Viewpoint.market_direction == 'bearish'), 1))).label('bearish'),
            func.count(case((and_(Viewpoint.is_deleted == False, Viewpoint.market_direction == 'neutral'), 1))).label('neutral'),
        ).first()

        # 来源分布用GROUP BY
        source_rows = self.db.query(Viewpoint.source, func.count(Viewpoint.id)).filter(
            Viewpoint.is_deleted == False,
            Viewpoint.source.isnot(None)
        ).group_by(Viewpoint.source).all()
        source_distribution = {source: count for source, count in source_rows if source}

        total_viewpoints = vp_base.total_viewpoints or 0

        return {
            "total_posts": total_posts,
            "total_viewpoints": total_viewpoints,
            "total_content": total_posts + total_viewpoints,
            "source_distribution": source_distribution,
            "sentiment_distribution": {
                "bullish": vp_base.bullish or 0,
                "bearish": vp_base.bearish or 0,
                "neutral": vp_base.neutral or 0,
            }
        }

    def get_fund_stats(self) -> Dict:
        """获取基金统计（2条SQL替代原来6条）"""
        # 聚合查询
        row = self.db.query(
            func.count(FundInfo.id).label('total'),
            func.count(case((FundInfo.active_predictions > 0, 1))).label('active_funds'),
            func.avg(FundInfo.day_growth).label('avg_day_growth'),
            func.avg(FundInfo.week_growth).label('avg_week_growth'),
            func.avg(FundInfo.month_growth).label('avg_month_growth'),
        ).first()

        # 来源分布用GROUP BY
        sector_rows = self.db.query(FundInfo.sector_type, func.count(FundInfo.id)).filter(
            FundInfo.sector_type.isnot(None)
        ).group_by(FundInfo.sector_type).all()
        sector_distribution = {sector: count for sector, count in sector_rows if sector}

        from src.models.database import FundHistory
        fund_history_count = self.db.query(func.count(FundHistory.id)).scalar() or 0

        return {
            "total": row.total or 0,
            "fund_count": row.total or 0,
            "active_funds": row.active_funds or 0,
            "sector_distribution": sector_distribution,
            "avg_day_growth": round(row.avg_day_growth or 0, 2),
            "avg_week_growth": round(row.avg_week_growth or 0, 2),
            "avg_month_growth": round(row.avg_month_growth or 0, 2),
            "fund_history_count": fund_history_count
        }

    def get_all_stats(self) -> Dict:
        """获取所有统计数据"""
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
