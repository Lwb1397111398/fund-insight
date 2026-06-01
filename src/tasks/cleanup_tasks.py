"""
自动清理任务模块
定期清理过期的预测和观点
"""
from datetime import date, timedelta
from sqlalchemy.orm import Session
from src.models.database import Prediction, Viewpoint, Post, FundHistory, InvestmentAdvice, SessionLocal
from src.services.prediction_verify_service import PredictionVerifyService
import logging

logger = logging.getLogger(__name__)


class CleanupManager:
    """清理管理器 - 每次操作创建独立会话，避免连接泄漏"""
    
    def _get_db(self) -> Session:
        return SessionLocal()
    
    def cleanup_expired_predictions(self) -> dict:
        """
        清理过期的预测
        规则：target_date + 7天后自动删除
        """
        db = self._get_db()
        try:
            cutoff_date = date.today() - timedelta(days=7)
            
            expired_predictions = db.query(Prediction).filter(
                Prediction.target_date < cutoff_date,
                Prediction.is_deleted == False
            ).all()
            
            if not expired_predictions:
                return {
                    "success": True,
                    "deleted_predictions": 0,
                    "cleaned_posts": 0
                }
            
            prediction_data = []
            for p in expired_predictions:
                prediction_data.append({
                    'id': p.id,
                    'post_id': p.post_id,
                    'blogger_id': p.blogger_id,
                    'verify_count': p.verify_count,
                    'verify_score': p.verify_score,
                    'is_correct': p.is_correct
                })
            
            affected_posts = set(p['post_id'] for p in prediction_data)
            deleted_count = 0
            
            verify_service = PredictionVerifyService(db)
            
            for data in prediction_data:
                if data['verify_count'] and data['verify_count'] > 0:
                    verify_service.update_blogger_on_prediction_delete(
                        blogger_id=data['blogger_id'],
                        verify_score=data['verify_score'],
                        is_correct=data['is_correct']
                    )
                
                db.query(Prediction).filter(Prediction.id == data['id']).delete()
                deleted_count += 1
                logger.info(f"删除过期预测 ID: {data['id']}, 帖子ID: {data['post_id']}")
            
            db.commit()
            
            cleaned_posts = self._cleanup_empty_posts(db, affected_posts)
            
            return {
                "success": True,
                "deleted_predictions": deleted_count,
                "cleaned_posts": cleaned_posts
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"清理过期预测失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_predictions": 0,
                "cleaned_posts": 0
            }
        finally:
            db.close()
    
    def cleanup_expired_viewpoints(self) -> dict:
        """
        清理过期的观点
        规则：viewpoint_date + 7天后删除
        """
        db = self._get_db()
        try:
            today = date.today()
            cutoff = today - timedelta(days=7)
            
            deleted_count = 0
            
            old_viewpoints = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date < cutoff
            ).all()
            
            for viewpoint in old_viewpoints:
                db.delete(viewpoint)
                deleted_count += 1
                logger.info(f"删除过期观点 ID: {viewpoint.id}, 日期: {viewpoint.viewpoint_date}")
            
            db.commit()
            
            logger.info(f"观点清理完成: 删除 {deleted_count} 个")
            
            return {
                "success": True,
                "deleted_viewpoints": deleted_count
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"清理过期观点失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_viewpoints": 0
            }
        finally:
            db.close()
    
    def manual_cleanup_viewpoints(self, days: int = 10) -> dict:
        """
        手动清理观点
        规则：删除 viewpoint_date 超过指定天数的观点
        默认10天，因为观点只有近7天才会被投资建议采纳
        
        Args:
            days: 保留天数，默认10天
        """
        db = self._get_db()
        try:
            cutoff_date = date.today() - timedelta(days=days)
            
            old_viewpoints = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date < cutoff_date
            ).all()
            
            deleted_count = 0
            
            for viewpoint in old_viewpoints:
                db.delete(viewpoint)
                deleted_count += 1
                logger.info(f"手动删除观点 ID: {viewpoint.id}, 日期: {viewpoint.viewpoint_date}, 来源: {viewpoint.source}")
            
            db.commit()
            
            logger.info(f"手动清理观点完成: 删除 {deleted_count} 个超过 {days} 天的观点")
            
            return {
                "success": True,
                "deleted_viewpoints": deleted_count,
                "cutoff_date": cutoff_date.isoformat(),
                "message": f"已删除 {deleted_count} 个超过 {days} 天的观点"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"手动清理观点失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_viewpoints": 0
            }
        finally:
            db.close()
    
    def _cleanup_empty_posts(self, db: Session, post_ids: set) -> int:
        """
        清理没有预测的帖子
        返回清理的帖子数量
        """
        cleaned_count = 0
        
        for post_id in post_ids:
            remaining_predictions = db.query(Prediction).filter(
                Prediction.post_id == post_id
            ).count()
            
            if remaining_predictions == 0:
                post = db.query(Post).filter(Post.id == post_id).first()
                if post:
                    db.delete(post)
                    cleaned_count += 1
                    logger.info(f"删除空帖子 ID: {post_id}")
        
        if cleaned_count > 0:
            db.commit()
        
        return cleaned_count
    
    def cleanup_old_fund_history(self) -> dict:
        """
        清理过期的基金历史净值数据
        """
        db = self._get_db()
        try:
            today = date.today()
            cutoff_90 = today - timedelta(days=90)
            
            deleted_count = 0
            preserved_for_predictions = 0
            
            from src.models.database import FundInfo
            
            active_long_predictions = db.query(Prediction).filter(
                Prediction.status == 'pending',
                Prediction.target_date > cutoff_90
            ).all()
            
            fund_extended_cutoff = {}
            for pred in active_long_predictions:
                fund_code = pred.fund_code
                if fund_code:
                    min_date = pred.prediction_date if pred.prediction_date else cutoff_90
                    if fund_code not in fund_extended_cutoff:
                        fund_extended_cutoff[fund_code] = min_date
                    else:
                        fund_extended_cutoff[fund_code] = min(
                            fund_extended_cutoff[fund_code], 
                            min_date
                        )
            
            if fund_extended_cutoff:
                logger.info(f"[清理] 发现 {len(fund_extended_cutoff)} 个基金有长期活跃预测，延长数据保留")
            
            funds = db.query(FundInfo).all()
            
            for fund in funds:
                extended_cutoff = fund_extended_cutoff.get(fund.fund_code)
                
                history = db.query(FundHistory).filter(
                    FundHistory.fund_code == fund.fund_code
                ).order_by(FundHistory.nav_date.desc()).all()
                
                for h in history:
                    nav_date = h.nav_date if isinstance(h.nav_date, date) else date.fromisoformat(str(h.nav_date))
                    
                    if nav_date >= cutoff_90:
                        continue
                    
                    if extended_cutoff and nav_date >= extended_cutoff:
                        preserved_for_predictions += 1
                        continue
                    
                    db.delete(h)
                    deleted_count += 1
                    logger.debug(f"删除基金历史(>90天): {fund.fund_code} {nav_date}")
            
            db.commit()
            
            logger.info(f"清理基金历史净值完成: 删除 {deleted_count} 条记录, 为长期预测保留 {preserved_for_predictions} 条记录")
            
            return {
                "success": True,
                "deleted_fund_history": deleted_count,
                "preserved_for_predictions": preserved_for_predictions,
                "funds_with_extended_retention": len(fund_extended_cutoff)
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"清理基金历史净值失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_fund_history": 0
            }
        finally:
            db.close()
    
    def cleanup_empty_posts(self) -> dict:
        """
        清理没有预测的空帖子（无论时间）
        """
        db = self._get_db()
        try:
            posts_with_predictions = db.query(Prediction.post_id).filter(
                Prediction.is_deleted == False
            ).distinct().subquery()
            
            empty_posts = db.query(Post).filter(
                ~Post.id.in_(posts_with_predictions)
            ).all()
            
            deleted_count = 0
            for post in empty_posts:
                db.delete(post)
                deleted_count += 1
                logger.info(f"删除空帖子 ID: {post.id}, 标题: {post.title}")
            
            if deleted_count > 0:
                db.commit()
            
            return {
                "success": True,
                "deleted_empty_posts": deleted_count
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"清理空帖子失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_empty_posts": 0
            }
        finally:
            db.close()
    
    def cleanup_old_advice(self) -> dict:
        """
        清理超过一周的投资建议
        """
        db = self._get_db()
        try:
            cutoff_date = date.today() - timedelta(days=7)
            
            old_advice = db.query(InvestmentAdvice).filter(
                InvestmentAdvice.advice_date < cutoff_date
            ).all()
            
            deleted_count = 0
            for advice in old_advice:
                db.delete(advice)
                deleted_count += 1
                logger.info(f"删除过期投资建议 ID: {advice.id}, 日期: {advice.advice_date}")
            
            if deleted_count > 0:
                db.commit()
            
            return {
                "success": True,
                "deleted_advice": deleted_count
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"清理投资建议失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "deleted_advice": 0
            }
        finally:
            db.close()
    
    def cleanup_oldest_batch(self, batch_days: int = 7, limit: int = 100) -> dict:
        """
        温和清理：只清理最老的一批过期数据，避免一次性清理过多影响博主统计

        与 run_full_cleanup 的区别：
        - run_full_cleanup 清理所有过期数据（target_date < 今天-7天）
        - cleanup_oldest_batch 只清理过期最久的数据（target_date < 今天-7-batch_days天），且限制数量

        Args:
            batch_days: 额外回溯天数，默认7天。即清理 target_date < 今天-14天 的数据
            limit: 每类数据最多清理条数，默认100
        """
        logger.info(f"开始温和清理: batch_days={batch_days}, limit={limit}")

        # 清理最老的过期预测
        prediction_result = self._cleanup_oldest_predictions(batch_days, limit)

        # 清理最老的过期观点
        viewpoint_result = self._cleanup_oldest_viewpoints(batch_days, limit)

        total_deleted = (
            prediction_result.get("deleted_predictions", 0) +
            viewpoint_result.get("deleted_viewpoints", 0)
        )

        result = {
            "success": prediction_result.get("success", False) and viewpoint_result.get("success", False),
            "predictions": prediction_result,
            "viewpoints": viewpoint_result,
            "total_deleted": total_deleted,
            "batch_days": batch_days,
            "limit": limit,
            "timestamp": date.today().isoformat()
        }

        logger.info(f"温和清理完成: 共删除 {total_deleted} 项")
        return result

    def _cleanup_oldest_predictions(self, batch_days: int, limit: int) -> dict:
        """清理最老的一批过期预测"""
        db = self._get_db()
        try:
            # 清理条件：target_date < 今天 - 7(保留) - batch_days(回溯)
            cutoff_date = date.today() - timedelta(days=7 + batch_days)

            expired_predictions = db.query(Prediction).filter(
                Prediction.target_date < cutoff_date,
                Prediction.is_deleted == False
            ).order_by(Prediction.target_date.asc()).limit(limit).all()

            if not expired_predictions:
                return {"success": True, "deleted_predictions": 0, "cleaned_posts": 0}

            prediction_data = []
            for p in expired_predictions:
                prediction_data.append({
                    'id': p.id,
                    'post_id': p.post_id,
                    'blogger_id': p.blogger_id,
                    'verify_count': p.verify_count,
                    'verify_score': p.verify_score,
                    'is_correct': p.is_correct
                })

            affected_posts = set(p['post_id'] for p in prediction_data)
            deleted_count = 0

            verify_service = PredictionVerifyService(db)

            for data in prediction_data:
                if data['verify_count'] and data['verify_count'] > 0:
                    verify_service.update_blogger_on_prediction_delete(
                        blogger_id=data['blogger_id'],
                        verify_score=data['verify_score'],
                        is_correct=data['is_correct']
                    )

                db.query(Prediction).filter(Prediction.id == data['id']).delete()
                deleted_count += 1

            db.commit()

            cleaned_posts = self._cleanup_empty_posts(db, affected_posts)

            logger.info(f"温和清理预测完成: 删除 {deleted_count} 个, 清理 {cleaned_posts} 个空帖子")
            return {
                "success": True,
                "deleted_predictions": deleted_count,
                "cleaned_posts": cleaned_posts
            }
        except Exception as e:
            db.rollback()
            logger.error(f"温和清理预测失败: {e}")
            return {"success": False, "error": str(e), "deleted_predictions": 0, "cleaned_posts": 0}
        finally:
            db.close()

    def _cleanup_oldest_viewpoints(self, batch_days: int, limit: int) -> dict:
        """清理最老的一批过期观点"""
        db = self._get_db()
        try:
            cutoff_date = date.today() - timedelta(days=7 + batch_days)

            old_viewpoints = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date < cutoff_date
            ).order_by(Viewpoint.viewpoint_date.asc()).limit(limit).all()

            deleted_count = 0
            for viewpoint in old_viewpoints:
                db.delete(viewpoint)
                deleted_count += 1

            if deleted_count > 0:
                db.commit()

            logger.info(f"温和清理观点完成: 删除 {deleted_count} 个")
            return {"success": True, "deleted_viewpoints": deleted_count}
        except Exception as e:
            db.rollback()
            logger.error(f"温和清理观点失败: {e}")
            return {"success": False, "error": str(e), "deleted_viewpoints": 0}
        finally:
            db.close()

    def run_full_cleanup(self) -> dict:
        """运行完整清理"""
        logger.info("开始自动清理任务...")
        
        prediction_result = self.cleanup_expired_predictions()
        viewpoint_result = self.cleanup_expired_viewpoints()
        fund_history_result = self.cleanup_old_fund_history()
        empty_posts_result = self.cleanup_empty_posts()
        advice_result = self.cleanup_old_advice()
        
        total_deleted = (
            prediction_result.get("deleted_predictions", 0) +
            viewpoint_result.get("deleted_viewpoints", 0) +
            fund_history_result.get("deleted_fund_history", 0) +
            empty_posts_result.get("deleted_empty_posts", 0) +
            advice_result.get("deleted_advice", 0)
        )
        
        result = {
            "success": all([
                prediction_result.get("success", False),
                viewpoint_result.get("success", False),
                fund_history_result.get("success", False),
                empty_posts_result.get("success", False),
                advice_result.get("success", False)
            ]),
            "predictions": {
                "deleted": prediction_result.get("deleted_predictions", 0)
            },
            "viewpoints": {
                "deleted": viewpoint_result.get("deleted_viewpoints", 0)
            },
            "fund_history": {
                "deleted": fund_history_result.get("deleted_fund_history", 0)
            },
            "empty_posts": {
                "deleted": empty_posts_result.get("deleted_empty_posts", 0)
            },
            "advice": {
                "deleted": advice_result.get("deleted_advice", 0)
            },
            "total_deleted": total_deleted,
            "timestamp": date.today().isoformat()
        }
        
        logger.info(f"清理任务完成: {result}")
        return result


_cleanup_manager = None


def get_cleanup_manager() -> CleanupManager:
    """获取清理管理器实例"""
    global _cleanup_manager
    if _cleanup_manager is None:
        _cleanup_manager = CleanupManager()
    return _cleanup_manager


def run_cleanup_task():
    """运行清理任务（供定时任务调用）"""
    manager = get_cleanup_manager()
    return manager.run_full_cleanup()


if __name__ == '__main__':
    result = run_cleanup_task()
    print(f"清理结果: {result}")
