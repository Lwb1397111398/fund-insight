from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "add_fund_performance_indexes.py"


def test_fund_index_script_is_safe_and_manual():
    """基金性能索引脚本应可人工审批后执行，且默认只预览 SQL"""
    content = SCRIPT.read_text(encoding="utf-8")

    assert "CREATE INDEX IF NOT EXISTS" in content
    assert "ix_fund_info_sector_type" in content
    assert "ix_fund_info_active_predictions" in content
    assert "dry_run: bool = True" in content
    assert "--execute" in content
