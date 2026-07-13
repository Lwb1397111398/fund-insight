"""静态页面入口清理测试"""
from pathlib import Path

from src.api.main import app


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"


def test_removed_debug_pages_are_not_registered_routes():
    route_paths = {getattr(route, "path", "") for route in app.routes}

    assert "/diagnostic.html" not in route_paths
    assert "/test.html" not in route_paths
    assert "/vue-test.html" not in route_paths
    assert "/simple.html" not in route_paths


def test_removed_simple_page_file_is_gone():
    assert not (WEB_DIR / "simple.html").exists()
