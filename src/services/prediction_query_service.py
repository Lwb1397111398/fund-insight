from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, func, nullslast, or_
from sqlalchemy.orm import Session, joinedload

from src.models.database import Blogger, Post, Prediction
from src.services.prediction_lifecycle import classify, current_as_of, max_end_nav_age_days


class PredictionQueryService:
    """Server-side search, filtering and pagination for prediction management."""

    # 默认 due_first：到期待验证在最上面，其次即将到期，最后是已验证/无目标日
    SORT_OPTIONS = ("due_first", "target_asc", "target_desc", "latest")
    DEFAULT_SORT = "due_first"
    LIFECYCLE_FILTERS = ("due", "active", "unverifiable")

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
        lifecycle: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        sort = sort if sort in self.SORT_OPTIONS else self.DEFAULT_SORT

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
            lifecycle=lifecycle,
        )

        total = query.order_by(None).with_entities(func.count(Prediction.id)).scalar() or 0
        rows = (
            query.order_by(*self._order_by(sort))
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
                "sort": sort,
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

    def _order_by(self, sort: str):
        """默认 due_first：到期待验证 → 未到期（近到远）→ 无目标日 → 已验证（新到旧）。"""
        if sort == "target_asc":
            return (nullslast(Prediction.target_date.asc()), Prediction.id.desc())
        if sort == "target_desc":
            return (nullslast(Prediction.target_date.desc()), Prediction.id.desc())
        if sort == "latest":
            return (Prediction.prediction_date.desc(), Prediction.id.desc())

        today = current_as_of()
        window_floor = today - timedelta(days=max_end_nav_age_days())
        unverified = Prediction.is_correct.is_(None)
        has_target = Prediction.target_date.isnot(None)
        rank = case(
            # 0 = 到期且仍可验证（用户最需要先看到的）
            (
                and_(
                    unverified,
                    has_target,
                    Prediction.target_date <= today,
                    Prediction.target_date >= window_floor,
                ),
                0,
            ),
            # 1 = 未到期，按目标日由近到远
            (and_(unverified, has_target, Prediction.target_date > today), 1),
            # 2 = 缺目标日，无法排期
            (and_(unverified, Prediction.target_date.is_(None)), 2),
            # 3 = 已错过验证窗口
            (unverified, 3),
            # 4 = 已有结论
            else_=4,
        )
        # 未验证按目标日升序（先到期的先处理）；已验证按目标日降序（最近的在上）
        pending_key = case((unverified, Prediction.target_date), else_=None)
        verified_key = case((Prediction.is_correct.isnot(None), Prediction.target_date), else_=None)
        return (
            rank.asc(),
            nullslast(pending_key.asc()),
            nullslast(verified_key.desc()),
            Prediction.id.desc(),
        )

    def _lifecycle_conditions(self, lifecycle: str) -> List[Any]:
        """把 lifecycle 语义翻译成 SQL 条件（与 prediction_lifecycle 同口径）。"""
        today = current_as_of()
        window_floor = today - timedelta(days=max_end_nav_age_days())
        if lifecycle == "due":
            return [
                Prediction.is_correct.is_(None),
                Prediction.target_date.isnot(None),
                Prediction.target_date <= today,
                Prediction.target_date >= window_floor,
            ]
        if lifecycle == "active":
            return [
                Prediction.is_correct.is_(None),
                Prediction.target_date.isnot(None),
                Prediction.target_date > today,
            ]
        if lifecycle == "unverifiable":
            return [
                Prediction.is_correct.is_(None),
                Prediction.target_date.isnot(None),
                Prediction.target_date < window_floor,
            ]
        return []

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
        lifecycle = filters.get("lifecycle")
        if lifecycle in self.LIFECYCLE_FILTERS:
            query = query.filter(*self._lifecycle_conditions(lifecycle))
        return query

    def _facets(self) -> Dict[str, int]:
        today = current_as_of()
        window_floor = today - timedelta(days=max_end_nav_age_days())
        active = Prediction.is_deleted.is_(False)
        unverified = and_(active, Prediction.is_correct.is_(None), Prediction.target_date.isnot(None))
        row = self.db.query(
            func.count(case((active, 1))).label("all"),
            func.count(case((and_(active, Prediction.status == "pending"), 1))).label("pending"),
            func.count(case((and_(active, Prediction.status.in_(("success", "failed", "verified"))), 1))).label("verified"),
            func.count(case((and_(active, Prediction.is_correct.is_(True)), 1))).label("correct"),
            func.count(case((and_(active, Prediction.is_correct.is_(False)), 1))).label("wrong"),
            func.count(case((and_(active, Prediction.prediction_type == "flat"), 1))).label("flat"),
            func.count(case((Prediction.is_deleted.is_(True), 1))).label("archived"),
            func.count(case((
                and_(
                    unverified,
                    Prediction.target_date <= today,
                    Prediction.target_date >= window_floor,
                ), 1))).label("due"),
            func.count(case((and_(unverified, Prediction.target_date > today), 1))).label("upcoming"),
            func.count(case((and_(unverified, Prediction.target_date < window_floor), 1))).label("unverifiable"),
        ).one()
        return {
            "all": row.all or 0,
            "pending": row.pending or 0,
            "verified": row.verified or 0,
            "correct": row.correct or 0,
            "wrong": row.wrong or 0,
            "flat": row.flat or 0,
            "archived": row.archived or 0,
            "due": row.due or 0,
            "upcoming": row.upcoming or 0,
            "unverifiable": row.unverifiable or 0,
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
        lifecycle = classify(prediction)
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
            "lifecycle": lifecycle,
            "days_to_target": (
                (prediction.target_date - current_as_of()).days
                if prediction.target_date
                else None
            ),
            "verification_result": verification_result,
            "is_correct": prediction.is_correct,
            "actual_change": prediction.actual_change,
            "verify_score": prediction.verify_score,
            "verify_count": prediction.verify_count,
            "is_expired": prediction.is_expired,
            "is_deleted": prediction.is_deleted,
            "created_at": prediction.created_at.isoformat() if prediction.created_at else None,
        }
