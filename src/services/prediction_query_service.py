from datetime import date
from typing import Any, Dict, Optional

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload

from src.models.database import Blogger, Post, Prediction


class PredictionQueryService:
    """Server-side search, filtering and pagination for prediction management."""

    def __init__(self, db: Session):
        self.db = db

    def search(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        search: Optional[str] = None,
        blogger_id: Optional[int] = None,
        fund_code: Optional[str] = None,
        sector: Optional[str] = None,
        prediction_type: Optional[str] = None,
        status: Optional[str] = None,
        result: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        archive: str = "active",
        is_expired: Optional[bool] = None,
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))

        query = self._base_query(archive=archive)
        query = self._apply_filters(
            query,
            search=search,
            blogger_id=blogger_id,
            fund_code=fund_code,
            sector=sector,
            prediction_type=prediction_type,
            status=status,
            result=result,
            start_date=start_date,
            end_date=end_date,
            is_expired=is_expired,
        )

        total = query.order_by(None).with_entities(func.count(Prediction.id)).scalar() or 0
        rows = (
            query.order_by(Prediction.prediction_date.desc(), Prediction.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "data": [self._serialize(row) for row in rows],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": page * page_size < total,
                "facets": self._facets(),
            },
        }

    def get_detail(self, prediction_id: int) -> Optional[Dict[str, Any]]:
        prediction = (
            self.db.query(Prediction)
            .options(joinedload(Prediction.blogger), joinedload(Prediction.post))
            .filter(Prediction.id == prediction_id)
            .first()
        )
        if prediction is None:
            return None
        detail = self._serialize(prediction)
        detail.update({
            "verify_history": prediction.verify_history or [],
            "start_nav": prediction.start_nav,
            "start_nav_date": prediction.start_nav_date.isoformat() if prediction.start_nav_date else None,
            "end_nav": prediction.end_nav,
            "end_nav_date": prediction.end_nav_date.isoformat() if prediction.end_nav_date else None,
            "current_nav": prediction.current_nav,
            "current_nav_date": prediction.current_nav_date.isoformat() if prediction.current_nav_date else None,
            "last_verify_date": prediction.last_verify_date.isoformat() if prediction.last_verify_date else None,
            "deleted_at": prediction.deleted_at.isoformat() if prediction.deleted_at else None,
            "delete_reason": prediction.delete_reason,
            "restore_before": prediction.restore_before.isoformat() if prediction.restore_before else None,
        })
        return detail

    def _base_query(self, *, archive: str):
        query = self.db.query(Prediction).options(
            joinedload(Prediction.blogger),
            joinedload(Prediction.post),
        )
        if archive == "archived":
            return query.filter(Prediction.is_deleted.is_(True))
        if archive == "all":
            return query
        return query.filter(Prediction.is_deleted.is_(False))

    def _apply_filters(self, query, **filters):
        search = (filters.get("search") or "").strip()
        if search:
            pattern = f"%{search}%"
            query = query.join(Blogger, Prediction.blogger_id == Blogger.id).outerjoin(
                Post, Prediction.post_id == Post.id
            ).filter(or_(
                Blogger.name.ilike(pattern),
                Prediction.fund_code.ilike(pattern),
                Prediction.fund_name.ilike(pattern),
                Prediction.sector.ilike(pattern),
                Prediction.prediction_content.ilike(pattern),
                Post.title.ilike(pattern),
            ))

        if filters.get("blogger_id") is not None:
            query = query.filter(Prediction.blogger_id == filters["blogger_id"])
        if filters.get("fund_code"):
            query = query.filter(Prediction.fund_code == filters["fund_code"])
        if filters.get("sector"):
            query = query.filter(Prediction.sector == filters["sector"])
        if filters.get("prediction_type"):
            query = query.filter(Prediction.prediction_type == filters["prediction_type"])
        if filters.get("status"):
            status = filters["status"]
            if status == "verified":
                query = query.filter(Prediction.status.in_(("success", "failed", "verified")))
            else:
                query = query.filter(Prediction.status == status)
        if filters.get("result"):
            result = filters["result"]
            if result == "correct":
                query = query.filter(Prediction.is_correct.is_(True))
            elif result == "wrong":
                query = query.filter(Prediction.is_correct.is_(False))
            elif result in ("pending", "unverified"):
                query = query.filter(Prediction.status == "pending")
        if filters.get("start_date"):
            query = query.filter(Prediction.prediction_date >= filters["start_date"])
        if filters.get("end_date"):
            query = query.filter(Prediction.prediction_date <= filters["end_date"])
        if filters.get("is_expired") is not None:
            query = query.filter(Prediction.is_expired == filters["is_expired"])
        return query

    def _facets(self) -> Dict[str, int]:
        active = Prediction.is_deleted.is_(False)
        row = self.db.query(
            func.count(case((active, 1))).label("all"),
            func.count(case((and_(active, Prediction.status == "pending"), 1))).label("pending"),
            func.count(case((and_(active, Prediction.status.in_(("success", "failed", "verified"))), 1))).label("verified"),
            func.count(case((and_(active, Prediction.is_correct.is_(True)), 1))).label("correct"),
            func.count(case((and_(active, Prediction.is_correct.is_(False)), 1))).label("wrong"),
            func.count(case((and_(active, Prediction.prediction_type == "flat"), 1))).label("flat"),
            func.count(case((Prediction.is_deleted.is_(True), 1))).label("archived"),
        ).one()
        return {
            "all": row.all or 0,
            "pending": row.pending or 0,
            "verified": row.verified or 0,
            "correct": row.correct or 0,
            "wrong": row.wrong or 0,
            "flat": row.flat or 0,
            "archived": row.archived or 0,
        }

    @staticmethod
    def _serialize(prediction: Prediction) -> Dict[str, Any]:
        blogger = prediction.blogger
        post = prediction.post
        lifecycle_status = (
            "archived"
            if prediction.is_deleted
            else "pending"
            if prediction.status == "pending"
            else "verified"
        )
        verification_result = (
            "correct"
            if prediction.is_correct is True
            else "wrong"
            if prediction.is_correct is False
            else None
        )
        return {
            "id": prediction.id,
            "blogger_id": prediction.blogger_id,
            "blogger_name": blogger.name if blogger else "未知",
            "post_id": prediction.post_id,
            "post_title": post.title if post else None,
            "post_date": post.post_date.isoformat() if post and post.post_date else None,
            "post_source_url": post.source_url if post else None,
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
            "next_verify_date": prediction.next_verify_date.isoformat() if prediction.next_verify_date else None,
            "status": prediction.status,
            "lifecycle_status": lifecycle_status,
            "verification_result": verification_result,
            "is_correct": prediction.is_correct,
            "actual_change": prediction.actual_change,
            "verify_score": prediction.verify_score,
            "verify_count": prediction.verify_count,
            "is_expired": prediction.is_expired,
            "is_deleted": prediction.is_deleted,
            "created_at": prediction.created_at.isoformat() if prediction.created_at else None,
        }
