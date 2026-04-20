"""
预测服务
处理预测相关的业务逻辑
"""
from typing import List, Optional, Dict, Any
from datetime import date, timedelta, datetime
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_
import re

from .base import BaseService
from src.models.database import Prediction, Post, Blogger, FundInfo
from src.analyzer.llm_analyzer import get_analyzer


class PredictionService(BaseService[Prediction]):
    """预测服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, Prediction)
        self._blogger_cache = {}  # 博主信息缓存
    
    def _get_blogger_name(self, blogger_id: int) -> str:
        """
        获取博主名称（带缓存）
        
        Args:
            blogger_id: 博主ID
        
        Returns:
            博主名称
        """
        if blogger_id not in self._blogger_cache:
            blogger = self.db.query(Blogger).filter(Blogger.id == blogger_id).first()
            self._blogger_cache[blogger_id] = blogger.name if blogger else "未知"
        
        return self._blogger_cache[blogger_id]
    
    def get_by_blogger(self, blogger_id: int, skip: int = 0, limit: int = 100) -> List[Prediction]:
        """
        获取博主的预测列表
        
        Args:
            blogger_id: 博主 ID
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            预测列表
        """
        return self.db.query(Prediction).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.is_deleted == False
        ).order_by(Prediction.prediction_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_fund(self, fund_code: str, skip: int = 0, limit: int = 100) -> List[Prediction]:
        """
        获取基金的预测列表
        
        Args:
            fund_code: 基金代码
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            预测列表
        """
        return self.db.query(Prediction).filter(
            Prediction.fund_code == fund_code,
            Prediction.is_deleted == False
        ).order_by(Prediction.prediction_date.desc()).offset(skip).limit(limit).all()
    
    def get_active(self, skip: int = 0, limit: int = 100) -> List[Prediction]:
        """
        获取活跃预测（未过期且未验证）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            活跃预测列表
        """
        today = date.today()
        return self.db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.target_date >= today,
            Prediction.is_deleted == False
        ).order_by(Prediction.target_date.asc()).offset(skip).limit(limit).all()
    
    def get_pending_verification(self, days: int = 7) -> List[Prediction]:
        """
        获取待验证的预测
        
        Args:
            days: 目标日期在几天内
            
        Returns:
            待验证预测列表
        """
        today = date.today()
        end_date = today + timedelta(days=days)
        
        return self.db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.target_date >= today,
            Prediction.target_date <= end_date,
            Prediction.is_deleted == False
        ).all()
    
    def get_expired(self) -> List[Prediction]:
        """
        获取已过期的预测
        
        Returns:
            过期预测列表
        """
        today = date.today()
        return self.db.query(Prediction).filter(
            Prediction.status == 'pending',
            Prediction.target_date < today,
            Prediction.is_deleted == False
        ).all()
    
    def verify(self, prediction_id: int, actual_change: float, is_correct: bool, 
               ai_judgment: str = None) -> Optional[Prediction]:
        """
        验证预测
        
        Args:
            prediction_id: 预测 ID
            actual_change: 实际涨跌幅
            is_correct: 是否正确
            ai_judgment: AI 判断说明
            
        Returns:
            更新后的预测实例
        """
        return self.update(prediction_id, {
            "status": "verified",
            "actual_change": actual_change,
            "is_correct": is_correct,
            "ai_judgment": ai_judgment,
            "verified_at": datetime.now()
        })
    
    def get_stats(self, blogger_id: int = None) -> Dict:
        """
        获取预测统计
        
        Args:
            blogger_id: 博主 ID（可选，不传则统计全部）
            
        Returns:
            统计数据
        """
        query = self.db.query(Prediction).filter(Prediction.is_deleted == False)
        if blogger_id:
            query = query.filter(Prediction.blogger_id == blogger_id)
        
        total = query.count()
        verified = query.filter(Prediction.status == 'verified').count()
        correct = query.filter(Prediction.is_correct == True).count()
        pending = query.filter(Prediction.status == 'pending').count()
        
        accuracy = round(correct / verified, 4) if verified > 0 else 0
        
        return {
            "total": total,
            "verified": verified,
            "correct": correct,
            "pending": pending,
            "accuracy": accuracy
        }
    
    def get_by_sector(self, sector: str, skip: int = 0, limit: int = 100) -> List[Prediction]:
        """
        根据板块获取预测
        
        Args:
            sector: 板块名称
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            预测列表
        """
        return self.db.query(Prediction).filter(
            Prediction.sector == sector,
            Prediction.is_deleted == False
        ).order_by(Prediction.prediction_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_type(self, prediction_type: str, skip: int = 0, limit: int = 100) -> List[Prediction]:
        """
        根据预测类型获取预测
        
        Args:
            prediction_type: 预测类型（up/down）
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            预测列表
        """
        return self.db.query(Prediction).filter(
            Prediction.prediction_type == prediction_type,
            Prediction.is_deleted == False
        ).order_by(Prediction.prediction_date.desc()).offset(skip).limit(limit).all()
    
    def count_by_blogger(self, blogger_id: int) -> int:
        """
        统计博主的预测数量
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            预测数量
        """
        return self.db.query(func.count(Prediction.id)).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.is_deleted == False
        ).scalar()
    
    # ==================== 为路由重构新增的方法 ====================
    
    def get_predictions_with_filters(
        self,
        skip: int = 0,
        limit: int = 100,
        blogger_id: Optional[int] = None,
        status: Optional[str] = None,
        is_expired: Optional[bool] = None
    ) -> List[Dict]:
        """
        获取预测列表（包含博主和帖子信息）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            blogger_id: 博主ID筛选
            status: 状态筛选
            is_expired: 是否过期筛选
            
        Returns:
            预测列表（包含关联信息）
        """
        query = self.db.query(Prediction).options(
            joinedload(Prediction.blogger),
            joinedload(Prediction.post)
        ).filter(Prediction.is_deleted == False)
        
        if blogger_id:
            query = query.filter(Prediction.blogger_id == blogger_id)
        if status:
            query = query.filter(Prediction.status == status)
        if is_expired is not None:
            query = query.filter(Prediction.is_expired == is_expired)
        
        predictions = query.order_by(Prediction.prediction_date.desc()).offset(skip).limit(limit).all()
        
        result = []
        for p in predictions:
            blogger = p.blogger
            post = p.post
            
            result.append({
                "id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": blogger.name if blogger else "未知",
                "post_id": p.post_id,
                "post_title": post.title if post else None,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "sector_type": p.sector_type,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "prediction_period": p.prediction_period,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "status": p.status,
                "is_correct": p.is_correct,
                "actual_change": p.actual_change,
                "is_expired": p.is_expired,
                "verify_count": p.verify_count,
                "created_at": p.created_at.isoformat() if p.created_at else None
            })
        
        return result
    
    def get_prediction_detail(self, prediction_id: int) -> Optional[Dict]:
        """
        获取预测详情
        
        Args:
            prediction_id: 预测ID
            
        Returns:
            预测详情字典或None
        """
        prediction = self.get(prediction_id)
        if not prediction:
            return None
        
        blogger = self.db.query(Blogger).filter(Blogger.id == prediction.blogger_id).first()
        post = self.db.query(Post).filter(Post.id == prediction.post_id).first()
        
        return {
            "id": prediction.id,
            "blogger_id": prediction.blogger_id,
            "blogger_name": blogger.name if blogger else "未知",
            "post_id": prediction.post_id,
            "post_title": post.title if post else None,
            "fund_code": prediction.fund_code,
            "fund_name": prediction.fund_name,
            "sector": prediction.sector,
            "sector_type": prediction.sector_type,
            "prediction_type": prediction.prediction_type,
            "prediction_content": prediction.prediction_content,
            "confidence": prediction.confidence,
            "prediction_date": prediction.prediction_date.isoformat() if prediction.prediction_date else None,
            "prediction_period": prediction.prediction_period,
            "target_date": prediction.target_date.isoformat() if prediction.target_date else None,
            "status": prediction.status,
            "is_correct": prediction.is_correct,
            "actual_change": prediction.actual_change,
            "is_expired": prediction.is_expired,
            "verify_count": prediction.verify_count,
            "verify_history": prediction.verify_history or [],
            "start_nav": prediction.start_nav,
            "end_nav": prediction.end_nav,
            "current_nav": prediction.current_nav,
            "current_nav_date": prediction.current_nav_date.isoformat() if prediction.current_nav_date else None,
            "verify_score": prediction.verify_score,
            "created_at": prediction.created_at.isoformat() if prediction.created_at else None
        }
    
    def delete_prediction(self, prediction_id: int) -> bool:
        """
        删除预测（同时更新博主累计分数）
        
        Args:
            prediction_id: 预测ID
            
        Returns:
            是否删除成功
        """
        prediction = self.get(prediction_id)
        if not prediction:
            return False
        
        if prediction.verify_count and prediction.verify_count > 0:
            from src.services.prediction_verify_service import PredictionVerifyService
            verify_service = PredictionVerifyService(self.db)
            verify_service.update_blogger_on_prediction_delete(
                blogger_id=prediction.blogger_id,
                verify_score=prediction.verify_score,
                is_correct=prediction.is_correct
            )
        
        self.db.delete(prediction)
        self.db.commit()
        return True
    
    def get_verify_progress(self) -> Dict:
        """
        获取验证进度统计
        
        Returns:
            验证进度统计
        """
        total_predictions = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None)
        ).count()
        
        verified_predictions = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.verify_count > 0
        ).count()
        
        expired_predictions = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.is_expired == True
        ).count()
        
        pending_predictions = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.is_expired == False
        ).count()
        
        failed_nav_fetch = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.start_nav == None
        ).count()
        
        correct_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_correct == True
        ).count()
        
        incorrect_predictions = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_correct == False
        ).count()
        
        return {
            "total": total_predictions,
            "verified": verified_predictions,
            "expired": expired_predictions,
            "pending": pending_predictions,
            "failed_nav_fetch": failed_nav_fetch,
            "correct": correct_predictions,
            "incorrect": incorrect_predictions,
            "progress_percent": round(verified_predictions / total_predictions * 100, 1) if total_predictions > 0 else 0,
            "accuracy_percent": round(correct_predictions / expired_predictions * 100, 1) if expired_predictions > 0 else 0
        }
    
    def get_failed_predictions(self) -> List[Dict]:
        """
        获取验证失败的预测列表
        
        Returns:
            失败预测列表
        """
        failed = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.is_expired == True,
            Prediction.is_correct == False
        ).all()
        
        result = []
        for p in failed:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            result.append({
                "id": p.id,
                "blogger_name": blogger.name if blogger else "未知",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "actual_change": p.actual_change,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "target_date": p.target_date.isoformat() if p.target_date else None
            })
        
        return result
    
    def get_expiring_predictions(self, days: int = 7) -> Dict:
        """
        获取即将到期的预测
        
        Args:
            days: 未来天数
            
        Returns:
            即将到期的预测列表
        """
        today = date.today()
        target_date_limit = today + timedelta(days=days)
        
        expiring = self.db.query(Prediction).filter(
            Prediction.fund_code.isnot(None),
            Prediction.is_expired == False,
            Prediction.target_date <= target_date_limit,
            Prediction.target_date >= today
        ).all()
        
        result = []
        for p in expiring:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            days_remaining = (p.target_date - today).days if p.target_date else 0
            
            result.append({
                "id": p.id,
                "blogger_name": blogger.name if blogger else "未知",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "days_remaining": days_remaining,
                "current_change": p.actual_change,
                "verify_count": p.verify_count
            })
        
        return {
            "data": result,
            "message": f"发现 {len(result)} 个预测将在 {days} 天内到期"
        }
    
    def get_anomaly_predictions(self) -> Dict:
        """
        异常预测检测
        
        Returns:
            异常预测列表
        """
        anomalies = []
        
        # 高信心但失败的预测
        high_confidence_failed = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.is_correct == False,
            Prediction.confidence >= 80
        ).all()
        
        for p in high_confidence_failed:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            anomalies.append({
                "id": p.id,
                "type": "high_confidence_failed",
                "severity": "high",
                "blogger_name": blogger.name if blogger else "未知",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "actual_change": p.actual_change,
                "description": f"高信心预测({p.confidence}%)验证失败，实际涨跌{p.actual_change:+.2f}%"
            })
        
        # 方向严重偏离的预测
        large_deviation = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.prediction_type == 'up',
            Prediction.actual_change < -5
        ).all()
        
        large_deviation += self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.prediction_type == 'down',
            Prediction.actual_change > 5
        ).all()
        
        for p in large_deviation:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            anomalies.append({
                "id": p.id,
                "type": "large_deviation",
                "severity": "medium",
                "blogger_name": blogger.name if blogger else "未知",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "prediction_type": p.prediction_type,
                "actual_change": p.actual_change,
                "description": f"预测方向与实际严重偏离，预测{'看涨' if p.prediction_type == 'up' else '看跌'}，实际{p.actual_change:+.2f}%"
            })
        
        # 长期未验证的预测
        long_unverified = self.db.query(Prediction).filter(
            Prediction.is_expired == False,
            Prediction.fund_code.isnot(None),
            Prediction.verify_count == 0
        ).all()
        
        today = date.today()
        for p in long_unverified:
            if p.prediction_date:
                days_since = (today - p.prediction_date).days
                if days_since > 14:
                    blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
                    anomalies.append({
                        "id": p.id,
                        "type": "long_unverified",
                        "severity": "low",
                        "blogger_name": blogger.name if blogger else "未知",
                        "fund_code": p.fund_code,
                        "fund_name": p.fund_name,
                        "prediction_date": p.prediction_date.isoformat(),
                        "days_since": days_since,
                        "description": f"预测已发布{days_since}天但尚未验证"
                    })
        
        return {
            "total_anomalies": len(anomalies),
            "high_severity": len([a for a in anomalies if a['severity'] == 'high']),
            "medium_severity": len([a for a in anomalies if a['severity'] == 'medium']),
            "low_severity": len([a for a in anomalies if a['severity'] == 'low']),
            "anomalies": anomalies
        }
    
    def get_predictions_for_export(
        self,
        blogger_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict]:
        """
        获取用于导出的预测数据
        
        Args:
            blogger_id: 博主ID筛选
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            预测数据列表
        """
        query = self.db.query(Prediction).filter(Prediction.fund_code.isnot(None))
        
        if blogger_id:
            query = query.filter(Prediction.blogger_id == blogger_id)
        
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d').date()
                query = query.filter(Prediction.prediction_date >= start)
            except ValueError:
                print(f"[PredictionService] 日期格式错误: {start_date}")
        
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d').date()
                query = query.filter(Prediction.prediction_date <= end)
            except ValueError:
                print(f"[PredictionService] 日期格式错误: {end_date}")
        
        predictions = query.order_by(Prediction.prediction_date.desc()).all()
        
        result = []
        for p in predictions:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            result.append({
                "id": p.id,
                "blogger_name": blogger.name if blogger else "未知",
                "blogger_grade": blogger.grade if blogger else "C",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "sector_type": p.sector_type,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "confidence": p.confidence,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "prediction_period": p.prediction_period,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "status": p.status,
                "is_correct": p.is_correct,
                "actual_change": p.actual_change,
                "is_expired": p.is_expired,
                "verify_count": p.verify_count,
                "start_nav": p.start_nav,
                "end_nav": p.end_nav,
                "current_nav": p.current_nav
            })
        
        return result
    
    def get_history_lookup(
        self,
        fund_code: Optional[str] = None,
        sector: Optional[str] = None,
        days: int = 30
    ) -> Dict:
        """
        历史回溯查询
        
        Args:
            fund_code: 基金代码
            sector: 板块
            days: 天数
            
        Returns:
            历史预测数据
        """
        query = self.db.query(Prediction).filter(
            Prediction.is_expired == True,
            Prediction.fund_code.isnot(None)
        )
        
        if fund_code:
            query = query.filter(Prediction.fund_code == fund_code)
        elif sector:
            query = query.filter(Prediction.sector == sector)
        
        cutoff_date = date.today() - timedelta(days=days)
        query = query.filter(Prediction.target_date >= cutoff_date)
        
        predictions = query.order_by(Prediction.target_date.desc()).all()
        
        result = []
        correct_count = 0
        total_change = 0
        
        for p in predictions:
            blogger = self.db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            if p.is_correct:
                correct_count += 1
            if p.actual_change:
                total_change += p.actual_change
            
            result.append({
                "id": p.id,
                "blogger_name": blogger.name if blogger else "未知",
                "blogger_grade": blogger.grade if blogger else "C",
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "confidence": p.confidence,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "is_correct": p.is_correct,
                "actual_change": p.actual_change
            })
        
        return {
            "predictions": result,
            "summary": {
                "total": len(predictions),
                "correct": correct_count,
                "accuracy": round(correct_count / len(predictions) * 100, 1) if predictions else 0,
                "avg_change": round(total_change / len(predictions), 2) if predictions else 0
            }
        }
