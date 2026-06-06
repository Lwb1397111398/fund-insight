import inspect

from src.api.main import get_market_sentiment


def test_market_sentiment_uses_database_grouping_for_direction_counts():
    """市场情绪方向计数应使用数据库聚合，避免加载完整观点对象后逐条统计"""
    source = inspect.getsource(get_market_sentiment)

    assert "group_by(Viewpoint.market_direction)" in source
    assert "sum(1 for v in recent_viewpoints" not in source
