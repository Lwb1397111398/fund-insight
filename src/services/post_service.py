"""
帖子服务
处理帖子相关的业务逻辑
"""
from typing import Any, Dict, List, Optional
from datetime import date, datetime
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_
import logging

logger = logging.getLogger(__name__)

from .base import BaseService
from src.analyzer.llm_analyzer import get_analyzer
from src.models.database import (
    AnalysisLog,
    Blogger,
    FundInfo,
    Post,
    Prediction,
    PredictionGroup,
    VerificationTask,
    Viewpoint,
)


class PostService(BaseService[Post]):
    """帖子服务类"""
    
    def __init__(self, db: Session):
        super().__init__(db, Post)
    
    def get_by_blogger(self, blogger_id: int, skip: int = 0, limit: int = 100) -> List[Post]:
        """
        获取博主的帖子列表
        
        Args:
            blogger_id: 博主 ID
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            帖子列表
        """
        return self.db.query(Post).filter(
            Post.blogger_id == blogger_id
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def get_by_date_range(self, start_date: date, end_date: date, skip: int = 0, limit: int = 100) -> List[Post]:
        """
        获取日期范围内的帖子
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            帖子列表
        """
        return self.db.query(Post).filter(
            Post.post_date >= start_date,
            Post.post_date <= end_date
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def get_unanalyzed(self, limit: int = 50) -> List[Post]:
        """
        获取未分析的帖子
        
        Args:
            limit: 返回数量
            
        Returns:
            未分析的帖子列表
        """
        return self.db.query(Post).filter(
            Post.analyzed == False
        ).order_by(Post.created_at.desc()).limit(limit).all()
    
    def get_with_predictions(self, post_id: int) -> Optional[Dict]:
        """
        获取帖子及其预测
        
        Args:
            post_id: 帖子 ID
            
        Returns:
            包含预测的帖子信息
        """
        post = self.get(post_id)
        if not post:
            return None
        
        predictions = self.db.query(Prediction).filter(
            Prediction.post_id == post_id
        ).all()
        
        return {
            **{k: v for k, v in post.__dict__.items() if not k.startswith('_')},
            "predictions": [{k: v for k, v in p.__dict__.items() if not k.startswith('_')} for p in predictions]
        }
    
    def mark_analyzed(self, post_id: int, analysis_result: Dict) -> Optional[Post]:
        """
        标记帖子已分析
        
        Args:
            post_id: 帖子 ID
            analysis_result: 分析结果
            
        Returns:
            更新后的帖子实例
        """
        return self.update(post_id, {
            "analyzed": True,
            "analysis_result": analysis_result
        })
    
    def update_title(self, post_id: int, title: str, auto_titled: bool = True) -> Optional[Post]:
        """
        更新帖子标题
        
        Args:
            post_id: 帖子 ID
            title: 新标题
            auto_titled: 是否自动生成
            
        Returns:
            更新后的帖子实例
        """
        return self.update(post_id, {
            "title": title,
            "auto_titled": auto_titled
        })
    
    def search(self, keyword: str, skip: int = 0, limit: int = 20) -> List[Post]:
        """
        搜索帖子
        
        Args:
            keyword: 搜索关键词
            skip: 跳过记录数
            limit: 返回记录数
            
        Returns:
            匹配的帖子列表
        """
        return self.db.query(Post).filter(
            (Post.title.contains(keyword)) | (Post.content.contains(keyword))
        ).order_by(Post.post_date.desc()).offset(skip).limit(limit).all()
    
    def count_by_blogger(self, blogger_id: int) -> int:
        """
        统计博主的帖子数量
        
        Args:
            blogger_id: 博主 ID
            
        Returns:
            帖子数量
        """
        return self.db.query(func.count(Post.id)).filter(
            Post.blogger_id == blogger_id
        ).scalar()

    @staticmethod
    def get_analysis_status(post: Post) -> str:
        if post.analyzed:
            return "succeeded"
        result = post.analysis_result if isinstance(post.analysis_result, dict) else {}
        meta = result.get("_meta") or {}
        status = meta.get("status")
        if status == "running":
            # 僵尸自愈（显示层）：进程中断后 meta 会停在 running，
            # 超时的按待分析显示，否则首页"分析中"徽章永久虚挂。
            # 阈值与 PostAnalysisService._claim 的接管窗口一致；
            # 解析失败时显示层宁可判 stale（_claim 执行层另有保守判断防双写）。
            from src.services.post_analysis_service import PostAnalysisService

            try:
                updated_at = datetime.fromisoformat(meta.get("updated_at", ""))
            except (TypeError, ValueError):
                return "pending"
            if datetime.now() - updated_at > PostAnalysisService.INTERRUPTED_AFTER:
                return "pending"
        return status if status in {"pending", "running", "failed", "skipped", "succeeded"} else "pending"

    def get_posts_page(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        keyword: Optional[str] = None,
        blogger_id: Optional[int] = None,
        analyzed: Optional[bool] = None,
        analysis_status: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        quality: Optional[str] = None,
    ) -> Dict[str, Any]:
        """返回兼容列表数据及服务端筛选、分页元信息。"""
        skip = max(0, int(skip or 0))
        limit = max(1, min(int(limit or 100), 1000))
        query = self.db.query(Post).options(joinedload(Post.blogger))
        if blogger_id is not None:
            query = query.filter(Post.blogger_id == blogger_id)
        if analyzed is not None:
            query = query.filter(Post.analyzed.is_(analyzed))
        if start_date is not None:
            query = query.filter(Post.post_date >= start_date)
        if end_date is not None:
            query = query.filter(Post.post_date <= end_date)
        if keyword and keyword.strip():
            pattern = f"%{keyword.strip()}%"
            query = query.filter(or_(
                Post.title.ilike(pattern),
                Post.content.ilike(pattern),
                Post.source_url.ilike(pattern),
            ))

        candidates = query.order_by(Post.post_date.desc(), Post.id.desc()).all()
        status_counts = {key: 0 for key in ("succeeded", "failed", "pending", "running", "skipped")}
        filtered = []
        for post in candidates:
            status = self.get_analysis_status(post)
            status_counts[status] += 1
            is_low, _ = self._is_low_quality_post(post.title or "", post.content or "")
            if analysis_status and analysis_status != "all" and status != analysis_status:
                continue
            if quality == "low" and not is_low:
                continue
            if quality == "normal" and is_low:
                continue
            filtered.append((post, status, is_low))

        total = len(filtered)
        page_rows = filtered[skip:skip + limit]
        page_post_ids = [post.id for post, _, _ in page_rows]
        prediction_counts = {
            post_id: count
            for post_id, count in self.db.query(
                Prediction.post_id,
                func.count(Prediction.id),
            ).filter(
                Prediction.post_id.in_(page_post_ids),
                Prediction.is_deleted.is_(False),
            ).group_by(Prediction.post_id).all()
        } if page_post_ids else {}

        data = []
        for post, status, is_low in page_rows:
            data.append({
                "id": post.id,
                "blogger_id": post.blogger_id,
                "blogger_name": post.blogger.name if post.blogger else "未知",
                "title": post.title,
                "content": post.content[:200] + "..." if len(post.content) > 200 else post.content,
                "post_date": post.post_date.isoformat() if post.post_date else None,
                "source_url": post.source_url,
                "analyzed": post.analyzed,
                "analysis_status": status,
                "analysis_error": ((post.analysis_result or {}).get("_meta") or {}).get("error")
                if isinstance(post.analysis_result, dict) else None,
                "is_low_quality": is_low,
                "prediction_count": prediction_counts.get(post.id, 0),
                "auto_titled": post.auto_titled,
                "created_at": post.created_at.isoformat() if post.created_at else None,
            })
        return {
            "data": data,
            "meta": {
                "total": total,
                "skip": skip,
                "limit": limit,
                "has_more": skip + limit < total,
                "status_counts": status_counts,
            },
        }

    def update_post_fields(self, post_id: int, values: Dict[str, Any]) -> Optional[Post]:
        post = self.db.query(Post).filter(Post.id == post_id).with_for_update().first()
        if not post:
            return None
        prediction_count = self.db.query(Prediction).filter(Prediction.post_id == post_id).count()
        protected_changes = {
            key for key in ("content", "post_date")
            if key in values and values[key] != getattr(post, key)
        }
        if prediction_count and protected_changes:
            raise ValueError("该帖子已有预测，只能修改标题和来源链接")
        for key in ("title", "content", "post_date", "source_url"):
            if key in values:
                setattr(post, key, values[key])
        if protected_changes:
            post.analyzed = False
            post.analysis_result = None
        self.db.commit()
        self.db.refresh(post)
        return post
    
    # ==================== 为路由重构新增的方法 ====================
    
    def get_posts_with_blogger_info(
        self, 
        skip: int = 0, 
        limit: int = 100,
        blogger_id: Optional[int] = None,
        analyzed: Optional[bool] = None
    ) -> List[Dict]:
        """
        获取帖子列表（包含博主信息）
        
        Args:
            skip: 跳过记录数
            limit: 返回记录数
            blogger_id: 博主ID筛选
            analyzed: 是否已分析筛选
            
        Returns:
            帖子列表（包含博主名称）
        """
        return self.get_posts_page(
            skip=skip,
            limit=limit,
            blogger_id=blogger_id,
            analyzed=analyzed,
        )["data"]
    
    def get_post_detail(self, post_id: int) -> Optional[Dict]:
        """
        获取帖子详情（包含预测列表）
        
        Args:
            post_id: 帖子ID
            
        Returns:
            帖子详情字典或None
        """
        post = self.get(post_id)
        if not post:
            return None
        
        # 获取关联的预测
        predictions = self.db.query(Prediction).filter(
            Prediction.post_id == post_id,
            Prediction.is_deleted == False
        ).all()
        
        prediction_list = []
        for p in predictions:
            prediction_list.append({
                "id": p.id,
                "fund_code": p.fund_code,
                "fund_name": p.fund_name,
                "sector": p.sector,
                "sector_type": p.sector_type,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "prediction_period": p.prediction_period,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "confidence": p.confidence,
                "status": p.status,
                "is_correct": p.is_correct,
                "verify_count": p.verify_count,
                "verify_score": p.verify_score
            })
        
        return {
            "id": post.id,
            "blogger_id": post.blogger_id,
            "blogger_name": post.blogger.name if post.blogger else "未知",
            "title": post.title,
            "content": post.content,
            "post_date": post.post_date.isoformat() if post.post_date else None,
            "source_url": post.source_url,
            "analyzed": post.analyzed,
            "analysis_status": self.get_analysis_status(post),
            "analysis_result": post.analysis_result,
            "auto_titled": post.auto_titled,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "predictions": prediction_list
        }
    
    def create_post_with_analysis(
        self, 
        blogger_id: int,
        content: str,
        post_date: date,
        title: Optional[str] = None,
        source_url: Optional[str] = None,
        async_mode: bool = True
    ) -> Dict:
        """
        创建帖子并自动分析
        
        Args:
            blogger_id: 博主ID
            content: 帖子内容
            post_date: 发布日期
            title: 标题（可选，不传则自动生成）
            source_url: 来源URL（可选）
            async_mode: 是否异步模式（True=快速返回，后台分析）
            
        Returns:
            创建结果，包含帖子信息和创建的预测数量
        """
        blogger = self.db.query(Blogger).filter(Blogger.id == blogger_id).first()
        if not blogger:
            raise ValueError("博主不存在")
        
        auto_titled = False
        if not title:
            try:
                post_date_str = post_date.strftime('%Y-%m-%d') if post_date else datetime.now().strftime('%Y-%m-%d')
                title = f"{post_date_str} {blogger.name}"
                auto_titled = True
            except (AttributeError, ValueError) as e:
                title = content[:30]
        
        db_post = Post(
            blogger_id=blogger_id,
            title=title,
            content=content,
            post_date=post_date,
            source_url=source_url,
            auto_titled=auto_titled,
            analyzed=False
        )
        self.db.add(db_post)
        self.db.commit()
        self.db.refresh(db_post)
        
        if async_mode:
            return {
                "success": True,
                "id": db_post.id,
                "title": db_post.title,
                "auto_titled": auto_titled,
                "analyzed": False,
                "predictions_created": 0,
                "message": "帖子已添加，请手动点击分析"
            }

        from src.services.post_analysis_service import PostAnalysisService

        result = PostAnalysisService(
            db=self.db,
            analyzer_factory=get_analyzer,
        ).analyze_post(db_post.id)
        return {
            "success": result.get("success", False),
            "id": db_post.id,
            "title": db_post.title,
            "auto_titled": auto_titled,
            "analyzed": result.get("status") == "succeeded",
            "predictions_created": result.get("predictions_created", 0),
            "message": result.get("message", "分析失败"),
            "analysis_status": result.get("status"),
        }

    def analyze_post_async(self, post_id: int) -> Dict:
        """兼容入口：委托统一分析服务。"""
        from src.services.post_analysis_service import PostAnalysisService

        return PostAnalysisService(
            db=self.db,
            analyzer_factory=get_analyzer,
        ).analyze_post(post_id)
    
    def _is_low_quality_post(self, title: str, content: str) -> tuple:
        from src.services.post_analysis_service import PostAnalysisService

        return PostAnalysisService.is_low_quality_post(title, content)

    def batch_analyze_posts(self) -> Dict:
        """兼容入口：同步运行统一的持久化任务服务。"""
        from src.services.post_analysis_service import PostAnalysisService

        task, _ = PostAnalysisService.create_job(self.db, limit=100)
        if not task.total_count:
            return {
                "analyzed": 0,
                "failed": 0,
                "deleted": 0,
                "skipped": 0,
                "message": "没有需要分析的帖子",
            }

        PostAnalysisService.run_job(task.id)
        self.db.expire_all()
        task = self.db.get(type(task), task.id)
        summary = dict(task.result_summary or {})
        return {
            "analyzed": summary.get("analyzed", 0),
            "failed": summary.get("failed", 0),
            "deleted": 0,
            "skipped": summary.get("skipped", 0),
            "message": (
                f"批量分析完成: 成功 {summary.get('analyzed', 0)} 个, "
                f"失败 {summary.get('failed', 0)} 个, 跳过 {summary.get('skipped', 0)} 个"
            ),
        }

    def _get_affected_prediction_groups(self, prediction_ids: List[int]) -> List[PredictionGroup]:
        if not prediction_ids:
            return []
        id_set = set(prediction_ids)
        affected = []
        for group in self.db.query(PredictionGroup).all():
            member_ids = set(group.prediction_ids or [])
            if group.representative_id in id_set or member_ids.intersection(id_set):
                affected.append(group)
        return affected

    def get_delete_preview(self, post_id: int) -> Optional[Dict[str, Any]]:
        post = self.db.get(Post, post_id)
        if not post:
            return None
        predictions = self.db.query(Prediction).filter(Prediction.post_id == post_id).all()
        prediction_ids = [prediction.id for prediction in predictions]
        return {
            "post_id": post.id,
            "title": post.title,
            "prediction_count": len(predictions),
            "verified_prediction_count": sum(1 for prediction in predictions if prediction.verify_count),
            "verification_task_count": self.db.query(VerificationTask).filter(
                VerificationTask.prediction_id.in_(prediction_ids)
            ).count() if prediction_ids else 0,
            "prediction_group_count": len(self._get_affected_prediction_groups(prediction_ids)),
            "analysis_log_count": self.db.query(AnalysisLog).filter(AnalysisLog.post_id == post_id).count(),
            "viewpoint_detach_count": self.db.query(Viewpoint).filter(Viewpoint.post_id == post_id).count(),
        }

    def _recalculate_after_post_delete(self, blogger_id: int, fund_codes: List[str]) -> None:
        from src.utils.blogger_stats import recalculate_blogger_stats

        recalculate_blogger_stats(self.db, blogger_id, commit=False)
        for fund_code in set(code for code in fund_codes if code):
            active_count = self.db.query(func.count(Prediction.id)).filter(
                Prediction.fund_code == fund_code,
                Prediction.is_deleted.is_(False),
            ).scalar() or 0
            fund = self.db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if fund:
                fund.active_predictions = active_count
                fund.can_delete = active_count == 0

    def delete_post_permanently(self, post_id: int) -> Optional[Dict[str, int]]:
        """彻底删除帖子及依赖；所有写操作共享一个事务。"""
        try:
            post = self.db.query(Post).filter(Post.id == post_id).with_for_update().first()
            if not post:
                return None
            blogger_id = post.blogger_id
            predictions = self.db.query(Prediction).filter(Prediction.post_id == post_id).all()
            prediction_ids = [prediction.id for prediction in predictions]
            fund_codes = [prediction.fund_code for prediction in predictions if prediction.fund_code]
            groups = self._get_affected_prediction_groups(prediction_ids)

            for group in groups:
                self.db.delete(group)
            if prediction_ids:
                self.db.query(VerificationTask).filter(
                    VerificationTask.prediction_id.in_(prediction_ids)
                ).delete(synchronize_session=False)
            deleted_logs = self.db.query(AnalysisLog).filter(
                AnalysisLog.post_id == post_id
            ).delete(synchronize_session=False)
            detached_viewpoints = self.db.query(Viewpoint).filter(
                Viewpoint.post_id == post_id
            ).update({Viewpoint.post_id: None}, synchronize_session=False)
            deleted_predictions = self.db.query(Prediction).filter(
                Prediction.post_id == post_id
            ).delete(synchronize_session=False)
            self.db.delete(post)
            self.db.flush()
            self._recalculate_after_post_delete(blogger_id, fund_codes)
            self.db.commit()
            return {
                "deleted_predictions": deleted_predictions,
                "deleted_prediction_groups": len(groups),
                "deleted_analysis_logs": deleted_logs,
                "detached_viewpoints": detached_viewpoints,
            }
        except Exception:
            self.db.rollback()
            raise

    def delete_post(self, post_id: int) -> bool:
        """兼容旧服务方法，执行已确认的彻底删除。"""
        return self.delete_post_permanently(post_id) is not None
