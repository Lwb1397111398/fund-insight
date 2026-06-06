"""
前端清理按钮测试
"""
from pathlib import Path


def test_dashboard_has_independent_test_data_cleanup_button():
    """首页操作区应有独立的一键清理测试数据按钮"""
    content = Path("web/index.html").read_text(encoding="utf-8")
    action_bar = content.split('<div class="action-bar">', 1)[1].split('</div>', 1)[0]

    assert '@click="cleanupData"' in action_bar
    assert '@click="cleanupTestData"' in action_bar
    assert "一键清理测试数据" in action_bar
