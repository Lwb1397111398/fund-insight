"""
博主服务
处理博主相关的业务逻辑
"""
from typing import List, Optional, Dict, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func

from .base import BaseService
from src.models.database import Blogger, Prediction, Post, Viewpoint
from src.utils.blogger_stats import (
    recalculate_blogger_stats,
    update_blogger_stats_incremental,
    calculate_blogger_rating
)


class BloggerService(BaseService[Blogger]):
    """博主服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, Blogger)
    
    def get_by_name(self, name: str) -> Optional[Blogger]:
        """
        根据名称获取博主
        
        Args:
            name: 博主名称
            
        Returns:
            博主实例或 None
        """
        return self.db.query(Blogger).filter(Blogger.name == name).first()
    
    def get_by_platform(self, platform: str, skip: int = 0, limit: int = 100) -> List[Blogger]:
        """
        根据平台获取博主列表
        
        Args:
            platform: 平台名称
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            博主列表
        """
        return self.db.query(Blogger).filter(
            Blogger.platform == platform
        ).offset(skip).limit(limit).all()
    
    def get_active_bloggers(self, skip: int = 0, limit: int = 100) -> List[Blogger]:
        """
        获取活跃博主列表
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            活跃博主列表
        """
        return self.db.query(Blogger).filter(
            Blogger.is_active == True
        ).order_by(Blogger.accuracy_rate.desc()).offset(skip).limit(limit).all()
    
    def get_top_bloggers(self, limit: int = 10) -> List[Blogger]:
        """
        获取准确率最高的博主
        
        Args:
            limit: 返回数量
            
        Returns:
            博主列表（按准确率降序）
        """
        return self.db.query(Blogger).filter(
            Blogger.total_predictions >= 5
        ).order_by(Blogger.accuracy_rate.desc()).limit(limit).all()
    
    def get_with_stats(self, blogger_id: int) -> Optional[Dict]:
        """
        获取博主及其统计数据
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            包含统计数据的博主信息
        """
        blogger = self.get(blogger_id)
        if not blogger:
            return None
        
        prediction_count = self.db.query(func.count(Prediction.id)).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.is_deleted == False
        ).scalar()
        
        correct_count = self.db.query(func.count(Prediction.id)).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.verify_result == '正确',
            Prediction.is_deleted == False
        ).scalar()
        
        return {
            **blogger.__dict__,
            "prediction_count": prediction_count,
            "correct_count": correct_count,
            "accuracy_rate": blogger.accuracy_rate
        }
    
    def update_accuracy(self, blogger_id: int) -> Optional[Blogger]:
        """
        更新博主准确率（使用统一的统计模块）
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            更新后的博主实例
        """
        stats = recalculate_blogger_stats(self.db, blogger_id)
        if not stats:
            return None
        return self.get(blogger_id)
    
    def update_stats_incremental(
        self, 
        blogger_id: int,
        score_delta: float = 0,
        correct_delta: int = 0,
        verified_delta: int = 0
    ) -> Optional[Blogger]:
        """
        增量更新博主统计数据
        
        Args:
            blogger_id: 博主 ID
            score_delta: 分数变化量
            correct_delta: 正确预测数变化量
            verified_delta: 已验证预测数变化量
            
        Returns:
            更新后的博主实例
        """
        update_blogger_stats_incremental(
            self.db, blogger_id, score_delta, correct_delta, verified_delta
        )
        return self.get(blogger_id)
    
    def deactivate(self, blogger_id: int) -> Optional[Blogger]:
        """
        停用博主
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            更新后的博主实例
        """
        return self.update(blogger_id, {"is_active": False})
    
    def activate(self, blogger_id: int) -> Optional[Blogger]:
        """
        激活博主

        Args:
            blogger_id: 博主 ID

        Returns:
            更新后的博主实例
        """
        return self.update(blogger_id, {"is_active": True})

    def safe_delete(self, blogger_id: int) -> Tuple[bool, str]:
        """
        安全删除博主（检查关联数据）

        Args:
            blogger_id: 博主 ID

        Returns:
            (成功与否, 消息)
        """
        blogger = self.get(blogger_id)
        if not blogger:
            return False, "博主不存在"

        # 检查关联的预测记录
        prediction_count = self.db.query(func.count(Prediction.id)).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.is_deleted == False
        ).scalar() or 0

        # 检查关联的帖子
        post_count = self.db.query(func.count(Post.id)).filter(
            Post.blogger_id == blogger_id
        ).scalar() or 0

        # 检查关联的观点
        viewpoint_count = self.db.query(func.count(Viewpoint.id)).filter(
            Viewpoint.blogger_id == blogger_id,
            Viewpoint.is_deleted == False
        ).scalar() or 0

        # 构建错误信息
        issues = []
        if prediction_count > 0:
            issues.append(f"{prediction_count} 条预测记录")
        if post_count > 0:
            issues.append(f"{post_count} 篇帖子")
        if viewpoint_count > 0:
            issues.append(f"{viewpoint_count} 条观点")

        if issues:
            return False, f"该博主存在 {'、'.join(issues)}，无法删除。请先删除相关数据。"

        # 没有关联数据，可以安全删除
        try:
            self.db.delete(blogger)
            self.db.commit()
            return True, "博主已成功删除"
        except Exception as e:
            self.db.rollback()
            return False, f"删除失败: {str(e)}"
