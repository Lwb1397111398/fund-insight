"""
服务层单元测试
"""
import pytest
from datetime import date


class TestBloggerService:
    """博主服务测试"""
    
    def test_create_blogger(self, db_session, sample_blogger_data):
        """测试创建博主"""
        from src.services import BloggerService
        
        service = BloggerService(db_session)
        blogger = service.create(sample_blogger_data)
        
        assert blogger.id is not None
        assert blogger.name == sample_blogger_data["name"]
        assert blogger.platform == sample_blogger_data["platform"]
    
    def test_get_blogger(self, db_session, sample_blogger_data):
        """测试获取博主"""
        from src.services import BloggerService
        
        service = BloggerService(db_session)
        created = service.create(sample_blogger_data)
        
        found = service.get(created.id)
        
        assert found is not None
        assert found.id == created.id
    
    def test_get_by_name(self, db_session, sample_blogger_data):
        """测试根据名称获取博主"""
        from src.services import BloggerService
        
        service = BloggerService(db_session)
        service.create(sample_blogger_data)
        
        found = service.get_by_name(sample_blogger_data["name"])
        
        assert found is not None
        assert found.name == sample_blogger_data["name"]
    
    def test_update_blogger(self, db_session, sample_blogger_data):
        """测试更新博主"""
        from src.services import BloggerService
        
        service = BloggerService(db_session)
        created = service.create(sample_blogger_data)
        
        updated = service.update(created.id, {"description": "更新后的描述"})
        
        assert updated is not None
        assert updated.description == "更新后的描述"
    
    def test_delete_blogger(self, db_session, sample_blogger_data):
        """测试删除博主"""
        from src.services import BloggerService
        
        service = BloggerService(db_session)
        created = service.create(sample_blogger_data)
        
        result = service.delete(created.id)
        
        assert result is True
        
        found = service.get(created.id)
        assert found is None


class TestPostService:
    """帖子服务测试"""
    
    def test_create_post(self, db_session, sample_blogger_data, sample_post_data):
        """测试创建帖子"""
        from src.services import BloggerService, PostService
        
        blogger_service = BloggerService(db_session)
        blogger = blogger_service.create(sample_blogger_data)
        
        sample_post_data["blogger_id"] = blogger.id
        post_service = PostService(db_session)
        post = post_service.create(sample_post_data)
        
        assert post.id is not None
        assert post.content == sample_post_data["content"]
    
    def test_get_by_blogger(self, db_session, sample_blogger_data, sample_post_data):
        """测试获取博主帖子"""
        from src.services import BloggerService, PostService
        
        blogger_service = BloggerService(db_session)
        blogger = blogger_service.create(sample_blogger_data)
        
        sample_post_data["blogger_id"] = blogger.id
        post_service = PostService(db_session)
        post_service.create(sample_post_data)
        
        posts = post_service.get_by_blogger(blogger.id)
        
        assert len(posts) == 1


class TestPredictionService:
    """预测服务测试"""
    
    def test_create_prediction(self, db_session, sample_blogger_data, sample_post_data, sample_prediction_data):
        """测试创建预测"""
        from src.services import BloggerService, PostService, PredictionService
        
        blogger_service = BloggerService(db_session)
        blogger = blogger_service.create(sample_blogger_data)
        
        sample_post_data["blogger_id"] = blogger.id
        post_service = PostService(db_session)
        post = post_service.create(sample_post_data)
        
        sample_prediction_data["post_id"] = post.id
        sample_prediction_data["blogger_id"] = blogger.id
        
        prediction_service = PredictionService(db_session)
        prediction = prediction_service.create(sample_prediction_data)
        
        assert prediction.id is not None
        assert prediction.fund_code == sample_prediction_data["fund_code"]
    
    def test_get_stats(self, db_session):
        """测试获取统计"""
        from src.services import PredictionService
        
        service = PredictionService(db_session)
        stats = service.get_stats()
        
        assert "total" in stats
        assert "accuracy" in stats


class TestFundService:
    """基金服务测试"""
    
    def test_create_fund(self, db_session):
        """测试创建基金"""
        import uuid
        from src.services import FundService

        code = f"TST{uuid.uuid4().hex[:6].upper()}"
        service = FundService(db_session)
        fund = service.create({
            "fund_code": code,
            "fund_name": "华夏成长混合"
        })

        assert fund.id is not None
        assert fund.fund_code == code

    def test_get_by_code(self, db_session):
        """测试根据代码获取基金"""
        import uuid
        from src.services import FundService

        code = f"TST{uuid.uuid4().hex[:6].upper()}"
        service = FundService(db_session)
        service.create({
            "fund_code": code,
            "fund_name": "测试基金"
        })

        found = service.get_by_code(code)

        assert found is not None
        assert found.fund_code == code


class TestViewpointService:
    """观点服务测试"""
    
    def test_create_viewpoint(self, db_session):
        """测试创建观点"""
        from src.services import ViewpointService
        
        service = ViewpointService(db_session)
        viewpoint = service.create({
            "content": "看好白酒板块反弹",
            "author": "测试作者",
            "source": "manual",
            "market_direction": "bullish",
            "confidence": 70,
            "viewpoint_date": date.today()
        })
        
        assert viewpoint.id is not None
        assert viewpoint.content == "看好白酒板块反弹"
    
    def test_get_stats(self, db_session):
        """测试获取观点统计"""
        from src.services import ViewpointService
        
        service = ViewpointService(db_session)
        stats = service.get_stats()
        
        assert "total" in stats
        assert "bullish" in stats
