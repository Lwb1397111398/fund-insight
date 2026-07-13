"""
前端清理按钮测试
"""
from pathlib import Path


def test_cleanup_view_guards_all_hard_delete_buttons():
    """待清理页默认只预览；硬删除按钮必须由运行时安全开关控制。"""
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
    assert cleanup_actions.count('v-if="cleanupEnabled"') == 3
    assert 'v-if="testData && testData.cleanup_enabled"' in cleanup_actions


def test_cleanup_requests_send_the_danger_confirmation_header():
    """维护环境开启后，前端也必须发送统一确认头。"""
    content = Path("web/index.html").read_text(encoding="utf-8")

    assert "cleanupEnabled = ref(false)" in content
    assert "cleanupEnabled.value = Boolean(res.data.data?.cleanup_enabled)" in content
    assert "'X-Danger-Confirm': 'cleanup-data'" in content


def test_cleanup_actions_are_responsive_grid():
    """清理按钮组应支持电脑四列、手机两列布局"""
    content = Path("web/index.html").read_text(encoding="utf-8")

    assert ".cleanup-actions" in content
    assert "grid-template-columns: repeat(4, 1fr)" in content
    assert "grid-template-columns: repeat(2, 1fr)" in content
