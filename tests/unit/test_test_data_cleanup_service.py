"""
测试数据清理服务测试
"""
from datetime import date

from src.models.database import Blogger, Post, Prediction
from src.services.test_data_cleanup_service import TestDataCleanupService


def test_cleanup_deletes_standalone_test_predictions(test_db):
    """清理测试数据应删除内容标记为测试的预测"""
    blogger = Blogger(name="正常博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()

    post = Post(
        blogger_id=blogger.id,
        title="正常帖子",
        content="这是一条正常帖子",
        post_date=date.today(),
    )
    test_db.add(post)
    test_db.flush()

    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="000001",
        fund_name="正常基金",
        prediction_type="bullish",
        prediction_content="测试预测：看涨",
        prediction_date=date.today(),
        target_date=date.today(),
    )
    test_db.add(prediction)
    test_db.commit()

    result = TestDataCleanupService(test_db).cleanup_test_data()

    assert result["deleted"]["predictions"] == 1
    assert test_db.query(Prediction).count() == 0
    assert test_db.query(Post).count() == 1
    assert test_db.query(Blogger).count() == 1
