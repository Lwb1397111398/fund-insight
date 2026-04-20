"""
API 集成测试
"""
import pytest
from fastapi.testclient import TestClient


class TestBloggersAPI:
    """博主 API 测试"""
    
    def test_get_bloggers(self):
        """测试获取博主列表"""
        from src.api.main import app
        
        client = TestClient(app)
        response = client.get("/api/bloggers")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
    
    def test_create_blogger(self):
        """测试创建博主"""
        from src.api.main import app
        
        client = TestClient(app)
        response = client.post(
            "/api/bloggers",
            params={
                "name": "API测试博主",
                "platform": "xiaohongshu"
            }
        )
        
        assert response.status_code == 200


class TestStatsAPI:
    """统计 API 测试"""
    
    def test_get_overview(self):
        """测试获取概览统计"""
        from src.api.main import app
        
        client = TestClient(app)
        response = client.get("/api/stats/overview")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "bloggers" in data["data"]


class TestHealthCheck:
    """健康检查测试"""
    
    def test_health(self):
        """测试健康检查"""
        from src.api.main import app
        
        client = TestClient(app)
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
