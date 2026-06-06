from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_fund_update_uses_status_polling_instead_of_long_request_timeout():
    """基金全量更新应启动后台任务并轮询状态，而不是长时间等待同步请求"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "/api/funds/update-status" in content
    assert "pollFundUpdateStatus" in content
    assert "timeout: 300000" not in content


def test_fund_update_polling_handles_lost_background_status():
    """Render 实例重启导致内存任务状态丢失时，应提示用户重试"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "status.last_result === null" in content
    assert "更新状态已丢失，请重新触发更新" in content
