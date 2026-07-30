from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "index.html"
MANAGER = ROOT / "web" / "prediction-manager.js"


def test_prediction_manager_is_loaded_and_owns_paginated_requests():
    html = INDEX.read_text(encoding="utf-8")
    script = MANAGER.read_text(encoding="utf-8")

    assert '<script src="/web/prediction-manager.js"></script>' in html
    assert "window.createPredictionManager" in script
    assert "page: predictionFilters.page" in script
    assert "page_size: predictionFilters.page_size" in script
    assert "Object.assign(predictionMeta" in script


def test_prediction_manager_supports_archive_restore_and_preview_first_maintenance():
    script = MANAGER.read_text(encoding="utf-8")

    assert "axios.delete(`/api/predictions/${id}`)" in script
    assert "axios.post(`/api/predictions/${id}/restore`)" in script
    assert "dry_run: true" in script
    assert "X-Danger-Confirm" in script
    assert "sync-prediction-mapping" in script
    assert "rollback-predictions" in script


def test_prediction_view_keeps_flat_rows_and_allows_pending_details():
    html = INDEX.read_text(encoding="utf-8")

    assert "prediction_type !== 'flat'" not in html
    assert '@click="viewPredictionDetail(p.id)"' in html
    assert 'v-if="p.lifecycle_status === \'archived\'"' in html
    assert "p.confidence !== null && p.confidence !== undefined" in html
    assert "predictionDetail.actual_change !== null" in html


def test_prediction_list_defaults_to_due_first_sorting():
    html = INDEX.read_text(encoding="utf-8")
    script = MANAGER.read_text(encoding="utf-8")

    # 默认排序与到期队列筛选都由前端明确传给后端
    assert "sort: 'due_first'" in script
    assert "sort: predictionFilters.sort || undefined" in script
    assert "lifecycle: predictionFilters.lifecycle || undefined" in script
    assert "setPredictionSort" in script

    # 到期/未到期快捷筛选与排序下拉在界面上可见
    assert 'setPredictionFilter(\'due\')' in html
    assert 'setPredictionFilter(\'upcoming\')' in html
    assert 'predictionMeta.facets.due' in html
    assert 'value="due_first"' in html
    assert "p.lifecycle === 'due_unverified'" in html


def test_cleanup_view_uses_three_bucket_endpoint():
    html = INDEX.read_text(encoding="utf-8")

    assert "'/api/config/cleanup/three-buckets/preview'" in html
    assert "axios.post('/api/config/cleanup/three-buckets'" in html
    assert "retentionPreview" in html
    assert "retentionBucketLabel" in html
    # 旧硬删入口不再被前端调用
    assert "axios.post('/api/config/cleanup'," not in html
