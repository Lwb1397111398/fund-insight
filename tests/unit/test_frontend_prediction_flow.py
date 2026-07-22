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
