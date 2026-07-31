"""前端帖子流程静态测试"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"
POST_MANAGER_JS = PROJECT_ROOT / "web" / "post-manager.js"


def test_add_post_handles_api_success_false_without_closing_modal():
    content = POST_MANAGER_JS.read_text(encoding="utf-8")

    assert "const addPost = async (enqueue = false)" in content
    assert "if (res.data.success)" in content
    assert "alert('添加失败: ' + (res.data.message || '未知错误'))" in content


def test_post_manager_is_split_and_uses_safe_management_apis():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = POST_MANAGER_JS.read_text(encoding="utf-8")

    assert '<script src="/web/post-manager.js"></script>' in html
    assert "window.createPostManager" in script
    assert "'/api/posts/analysis-jobs'" in script
    assert "delete-preview" in script
    assert "'X-Danger-Confirm': 'delete-post'" in script
    assert "post_analysis_task_id" in script
    assert "`/api/posts/analysis-jobs/${taskId}/resume`" in script
    assert "axios.patch(`/api/posts/${editingPost.id}`" in script


def test_post_progress_uses_dedicated_running_state():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = POST_MANAGER_JS.read_text(encoding="utf-8")

    assert "const postAnalysisRunning = ref(false)" in script
    assert "postAnalysisRunning.value = ['pending', 'running'].includes(data.status)" in script
    assert 'v-if="analysisJob && postAnalysisRunning"' in html
    assert ':disabled="postAnalysisRunning"' in html
    assert 'v-if="analysisJob && analyzing"' not in html


def test_post_view_has_filters_pagination_status_and_no_legacy_repair_buttons():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'v-model="postFilters.keyword"' in html
    assert 'v-model="postFilters.analysis_status"' in html
    assert 'v-model="postFilters.quality"' in html
    assert "postMeta.status_counts" in html
    assert "prediction_count" in html
    assert "postNextPage" in html
    assert "重置失败分析" not in html
    assert "修复板块匹配" not in html
