from datetime import date, datetime

from src.models.database import InvestmentAdvice, Viewpoint
from src.services.advice_service import AdviceService


def test_check_data_changed_detects_recent_viewpoint_soft_delete(test_db):
    viewpoint = Viewpoint(
        content="看好人工智能板块",
        author="测试作者",
        source="manual",
        market_direction="bullish",
        confidence=70,
        sectors_bullish=["人工智能"],
        viewpoint_date=date.today(),
        created_at=datetime(2026, 7, 10, 9, 0, 0),
        is_deleted=False,
        is_expired=False,
    )
    test_db.add(viewpoint)
    test_db.commit()

    service = AdviceService(test_db)
    current_hash = service._calculate_data_hash()
    test_db.add(InvestmentAdvice(
        advice_date=date.today(),
        advice_type="hold",
        advice_content="保持观察",
        data_hash=current_hash,
    ))
    test_db.commit()

    viewpoint.is_deleted = True
    viewpoint.deleted_at = datetime(2026, 7, 10, 10, 0, 0)
    test_db.commit()

    has_changed, new_hash, latest_advice = service.check_data_changed()

    assert has_changed is True
    assert new_hash != current_hash
    assert latest_advice is not None
