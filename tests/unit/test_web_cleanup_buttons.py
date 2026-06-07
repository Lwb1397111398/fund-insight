"""
前端清理按钮测试
"""
from pathlib import Path


def test_cleanup_view_has_four_cleanup_buttons_together():
    """待清理页应把四个清理选项放在同一组"""
    content = Path("web/index.html").read_text(encoding="utf-8")
    cleanup_actions = content.split('<div class="cleanup-actions">', 1)[1].split('</div>', 1)[0]

    assert '@click="cleanupData"' in cleanup_actions
    assert '@click="cleanupOldestBatch(7)"' in cleanup_actions
    assert '@click="cleanupOldestBatch(14)"' in cleanup_actions
    assert '@click="cleanupTestData"' in cleanup_actions
    assert "一键清理过期数据" in cleanup_actions
    assert "温和清理（7天前）" in cleanup_actions
    assert "温和清理（14天前）" in cleanup_actions
    assert "一键清理测试数据" in cleanup_actions


def test_cleanup_actions_are_responsive_grid():
    """清理按钮组应支持电脑四列、手机两列布局"""
    content = Path("web/index.html").read_text(encoding="utf-8")

    assert ".cleanup-actions" in content
    assert "grid-template-columns: repeat(4, 1fr)" in content
    assert "grid-template-columns: repeat(2, 1fr)" in content
