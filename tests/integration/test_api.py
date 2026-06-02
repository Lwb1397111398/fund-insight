"""
API 集成测试
"""
import os
import pytest
from fastapi.testclient import TestClient

# 确保测试环境有固定的密码，避免服务器生成随机密码导致认证失败
if not os.getenv("ACCESS_PASSWORD"):
    os.environ["ACCESS_PASSWORD"] = "test_password_123"

# 测试用的认证头
AUTH_HEADERS = {"X-Access-Password": os.environ["ACCESS_PASSWORD"]}


class TestBloggersAPI:
    """博主 API 测试"""

    def test_get_bloggers(self):
        """测试获取博主列表"""
        from src.api.main import app

        client = TestClient(app)
        response = client.get("/api/bloggers", headers=AUTH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_create_blogger(self):
        """测试创建博主"""
        import uuid
        from src.api.main import app

        client = TestClient(app)
        response = client.post(
            "/api/bloggers",
            json={
                "name": f"API测试博主_{uuid.uuid4().hex[:8]}",
                "platform": "xiaohongshu"
            },
            headers=AUTH_HEADERS
        )

        assert response.status_code == 200


class TestStatsAPI:
    """统计 API 测试"""

    def test_get_overview(self):
        """测试获取概览统计"""
        from src.api.main import app

        client = TestClient(app)
        response = client.get("/api/stats/overall", headers=AUTH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "active_bloggers" in data["data"]


class TestHealthCheck:
    """健康检查测试"""

    def test_health(self):
        """测试健康检查"""
        from src.api.main import app

        client = TestClient(app)
        response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
