"""
Pytest 配置文件
"""
import sys
import os
import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture
def db_session():
    """数据库会话 fixture，每次测试前清理测试数据"""
    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        # 回滚所有未提交的操作，避免测试数据污染
        db.rollback()
        db.close()


@pytest.fixture
def sample_blogger_data():
    """示例博主数据（使用 UUID 避免名称冲突）"""
    import uuid
    return {
        "name": f"测试博主_{uuid.uuid4().hex[:8]}",
        "platform": "xiaohongshu",
        "description": "这是一个测试博主"
    }


@pytest.fixture
def sample_post_data():
    """示例帖子数据"""
    from datetime import date
    return {
        "blogger_id": 1,
        "title": "测试帖子",
        "content": "这是一篇测试帖子的内容，用于单元测试。",
        "post_date": date.today()
    }


@pytest.fixture
def sample_prediction_data():
    """示例预测数据"""
    from datetime import date
    import uuid
    return {
        "post_id": 1,
        "blogger_id": 1,
        "fund_code": f"TEST{uuid.uuid4().hex[:6].upper()}",
        "fund_name": "测试基金",
        "sector": "白酒",
        "prediction_type": "bullish",
        "prediction_content": "看好白酒板块",
        "confidence": 70,
        "prediction_date": date.today(),
        "target_date": date.today()
    }
