"""前端数据导入结果展示测试"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_import_result_shows_imported_skipped_failed_and_warnings():
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "importResult.data.imported" in content
    assert "importResult.data.skipped" in content
    assert "importResult.data.failed" in content
    assert "importResult.data.warnings" in content
