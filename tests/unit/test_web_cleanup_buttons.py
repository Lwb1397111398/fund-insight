"""
前端清理按钮测试
"""
from pathlib import Path


def test_cleanup_view_guards_all_hard_delete_buttons():
    """待清理页只保留统一安全清理与独立测试数据清理。"""
    content = Path("web/index.html").read_text(encoding="utf-8")
    cleanup_actions = content.split('<div class="cleanup-actions">', 1)[1].split('</div>', 1)[0]

    assert '@click="cleanupData"' in cleanup_actions
    assert '@click="cleanupTestData"' in cleanup_actions
    assert "安全清理" in cleanup_actions
    assert "cleanupOldestBatch" not in cleanup_actions
    assert "温和清理" not in cleanup_actions
    assert "一键清理测试数据" in cleanup_actions
    assert 'v-if="cleanupEnabled"' not in cleanup_actions
    # 可点：仅 analyzing / 无清单 / 清单为空时禁用；开关关闭时用 alert 说明，避免“无反应”
    assert ':disabled="analyzing || !retentionPreview || !retentionPreview.total"' in cleanup_actions
    assert "清理开关已关闭" in content
    assert 'v-if="testData && testData.cleanup_enabled"' in cleanup_actions


def test_cleanup_requests_send_the_danger_confirmation_header():
    """维护环境开启后，前端也必须发送统一确认头。"""
    content = Path("web/index.html").read_text(encoding="utf-8")

    assert "cleanupEnabled = ref(false)" in content
    assert "cleanupPreviewError" in content
    assert "CLEANUP_POLL_TIMEOUT_MS" in content
    assert "cleanupEnabled.value = Boolean(d.cleanup_enabled)" in content
    assert "'X-Danger-Confirm': 'cleanup-data'" in content
    assert "preview_fingerprint" in content
    assert "/config/cleanup/tasks/" in content
    assert "await fetchRetentionPreview(); await fetchCleanupPreview(); await loadTestData();" in content
    # 三桶清理的核心护栏：已有验证结论的预测永不删
    assert "verified_ledger_excluded" in content


def test_cleanup_actions_are_responsive_grid():
    """清理按钮组应支持电脑四列、手机两列布局。样式提取到 common.css 后，
    需合并 index.html 与 common.css 一并断言。"""
    root = Path(__file__).resolve().parents[2]
    content = (root / "web" / "index.html").read_text(encoding="utf-8")
    css_path = root / "web" / "common.css"
    if css_path.exists():
        content += "\n" + css_path.read_text(encoding="utf-8")

    assert ".cleanup-actions" in content
    assert "grid-template-columns: repeat(2, 1fr)" in content
    assert "grid-template-columns: 1fr" in content


def test_cleanup_page_mentions_long_term_fund_and_accuracy_guard():
    content = Path("web/index.html").read_text(encoding="utf-8")
    assert "预测窗口完整保留" in content
    assert "归档累计" in content
    assert "long_term_fund_history" in content
