"""前端帖子流程静态测试"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_add_post_handles_api_success_false_without_closing_modal():
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "const addPost = async ()" in content
    assert "if (res.data.success)" in content
    assert "alert('添加失败: ' + (res.data.message || '未知错误'))" in content
