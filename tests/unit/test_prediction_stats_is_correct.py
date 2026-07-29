"""统计口径：准确率分母只用 is_correct，不依赖 is_expired 列。"""
from datetime import date, timedelta

from src.models.database import Blogger, Post, Prediction
from src.services.prediction_service import PredictionService
from src.services.stats_service import StatsService


def _seed(db, *, is_correct, is_expired, status="pending", fund_code="ST001"):
    blogger = db.query(Blogger).filter(Blogger.name == "统计口径博主").first()
    if not blogger:
        blogger = Blogger(name="统计口径博主", platform="test")
        db.add(blogger)
        db.flush()
    post = Post(
        blogger_id=blogger.id,
        content="stats",
        post_date=date.today() - timedelta(days=30),
        analyzed=True,
    )
    db.add(post)
    db.flush()
    p = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund_code,
        fund_name="统计基金",
        sector="测试",
        prediction_type="bullish",
        prediction_content="看涨",
        prediction_date=post.post_date,
        target_date=date.today() - timedelta(days=7),
        status=status,
        is_correct=is_correct,
        is_expired=is_expired,
        is_deleted=False,
        verify_count=1 if is_correct is not None else 0,
    )
    db.add(p)
    db.commit()
    return p


def test_get_stats_uses_is_correct_not_is_expired(test_db):
    """is_expired=True 但无结论 → 不得进 verified；is_correct 有值 → 进 verified。"""
    _seed(test_db, is_correct=None, is_expired=True, status="failed")  # 脏：列过期无结论
    _seed(test_db, is_correct=True, is_expired=False, status="pending")  # 脏：有结论列未过期
    _seed(test_db, is_correct=False, is_expired=True, status="failed")

    stats = PredictionService(test_db).get_stats()
    assert stats["verified"] == 2
    assert stats["correct"] == 1
    assert stats["pending"] == 1
    assert stats["accuracy"] == 0.5


def test_get_verify_progress_accuracy_denominator(test_db):
    _seed(test_db, is_correct=True, is_expired=False, fund_code="VP001")
    _seed(test_db, is_correct=False, is_expired=False, fund_code="VP002")
    _seed(test_db, is_correct=None, is_expired=True, fund_code="VP003")  # 不进分母

    progress = PredictionService(test_db).get_verify_progress()
    assert progress["verified"] == 2
    assert progress["expired"] == 2  # 兼容字段
    assert progress["correct"] == 1
    assert progress["incorrect"] == 1
    assert progress["pending"] == 1
    assert progress["accuracy_percent"] == 50.0


def test_stats_service_prediction_stats_aligns(test_db):
    _seed(test_db, is_correct=True, is_expired=False, fund_code="SS001")
    _seed(test_db, is_correct=False, is_expired=False, fund_code="SS002")
    _seed(test_db, is_correct=None, is_expired=True, fund_code="SS003")

    s = StatsService(test_db).get_prediction_stats()
    assert s["status_distribution"]["verified"] == 2
    assert s["status_distribution"]["expired"] == 2
    assert s["status_distribution"]["pending"] == 1
    assert s["correct_count"] == 1
    assert s["incorrect_count"] == 1
    assert s["accuracy"] == 50.0
