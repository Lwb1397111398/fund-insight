"""
博主统计工具模块
提供统一的博主准确率和评级计算逻辑

累计算法说明：
- total_verify_score: 累计验证分数（即使预测被删除也保留）
- total_predictions: 已验证预测数（用于计算准确率分母）
- 准确率 = total_verify_score / (total_predictions * 100) * 100

这样设计的好处：即使预测被物理删除，博主的准确率仍然可以正确计算
"""
from typing import Dict, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, case

from src.models.database import Blogger, Prediction


RATING_THRESHOLDS = [
    ('S', 80, 20),
    ('A', 70, 15),
    ('B', 60, 10),
    ('C', 50, 5),
    ('D', 0, 0),
]


def calculate_blogger_rating(accuracy: float, total_predictions: int) -> str:
    """
    根据准确率和预测数量计算博主评级
    
    Args:
        accuracy: 准确率 (0-100)
        total_predictions: 总预测数量
        
    Returns:
        评级 (S/A/B/C/D)
    """
    if total_predictions < 3:
        return 'C'
    
    for rating, acc_threshold, count_threshold in RATING_THRESHOLDS:
        if accuracy >= acc_threshold and total_predictions >= count_threshold:
            return rating
    return 'D'


def recalculate_blogger_stats(db: Session, blogger_id: int) -> Dict:
    """
    重新计算博主的统计数据（从预测记录重新统计）
    
    使用累计分数制：
    - total_verify_score: 累计验证分数
    - total_predictions: 已验证预测数
    - 准确率 = total_verify_score / (total_predictions * 100) * 100
    
    Args:
        db: 数据库会话
        blogger_id: 博主ID
        
    Returns:
        包含更新后统计数据的字典
    """
    blogger = db.query(Blogger).filter(Blogger.id == blogger_id).first()
    if not blogger:
        return None
    
    stats = db.query(
        func.count(Prediction.id).label('verified'),
        func.sum(case((Prediction.is_correct == True, 1), else_=0)).label('correct'),
        func.sum(Prediction.verify_score).label('total_score')
    ).filter(
        Prediction.blogger_id == blogger_id,
        Prediction.is_deleted == False,
        Prediction.verify_count > 0,
        Prediction.prediction_type != 'flat'
    ).first()
    
    verified_count = stats.verified or 0
    correct_predictions = stats.correct or 0
    total_verify_score = float(stats.total_score or 0)
    
    if verified_count > 0:
        accuracy = round(total_verify_score / (verified_count * 100) * 100, 2)
        accuracy = min(100.0, max(0.0, accuracy))
    else:
        accuracy = 0.0
    
    grade = calculate_blogger_rating(accuracy, verified_count)
    
    blogger.total_predictions = verified_count
    blogger.correct_predictions = correct_predictions
    blogger.total_verify_score = total_verify_score
    blogger.accuracy_rate = accuracy
    blogger.grade = grade
    
    db.commit()
    db.refresh(blogger)
    
    return {
        'total_predictions': verified_count,
        'correct_predictions': correct_predictions,
        'total_verify_score': total_verify_score,
        'accuracy_rate': accuracy,
        'grade': grade
    }


def update_blogger_stats_incremental(
    db: Session, 
    blogger_id: int,
    score_delta: float = 0,
    correct_delta: int = 0,
    verified_delta: int = 0
) -> Dict:
    """
    增量更新博主统计数据
    
    使用累计分数制：
    - total_predictions: 已验证预测数
    - 准确率 = total_verify_score / (total_predictions * 100) * 100
    
    Args:
        db: 数据库会话
        blogger_id: 博主ID
        score_delta: 分数变化量
        correct_delta: 正确预测数变化量
        verified_delta: 已验证预测数变化量（新增验证时为+1，合并/删除时为-1）
        
    Returns:
        更新后的统计数据
    """
    blogger = db.query(Blogger).filter(Blogger.id == blogger_id).first()
    if not blogger:
        return None
    
    blogger.total_predictions = max(0, (blogger.total_predictions or 0) + verified_delta)
    blogger.correct_predictions = max(0, (blogger.correct_predictions or 0) + correct_delta)
    blogger.total_verify_score = max(0, (blogger.total_verify_score or 0) + score_delta)
    
    if blogger.total_predictions > 0:
        blogger.accuracy_rate = round(
            blogger.total_verify_score / (blogger.total_predictions * 100) * 100, 2
        )
        blogger.accuracy_rate = min(100.0, max(0.0, blogger.accuracy_rate))
    else:
        blogger.accuracy_rate = 0.0
    
    blogger.grade = calculate_blogger_rating(
        blogger.accuracy_rate, 
        blogger.total_predictions
    )
    
    db.commit()
    db.refresh(blogger)
    
    return {
        'total_predictions': blogger.total_predictions,
        'correct_predictions': blogger.correct_predictions,
        'total_verify_score': blogger.total_verify_score,
        'accuracy_rate': blogger.accuracy_rate,
        'grade': blogger.grade
    }
