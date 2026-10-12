"""前端加载行为测试"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_login_success_only_loads_first_screen_data():
    """登录成功后只加载首屏必要数据，其他视图数据按需加载"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    eager_load_sequence = "fetchStats(); fetchBloggers(); loadAdviceHistory(); loadConfig(); loadTestData(); fetchSummaryStats();"

    assert eager_load_sequence not in content
    assert "fetchStats(); fetchBloggers();" in content


def test_advice_view_loads_history_on_demand():
    """进入投资建议视图时按需加载：历史照拉；最新一条也拉（2026-10-12 v2——
    以前刷新页面就回"暂无投资建议"空态，后端明明有 GET /api/advice，页面没接）。"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "else if (view === 'advice') {" in content
    assert "await loadAdviceHistory(); }" in content
    assert "if (!advice.value) { await loadLatestAdvice(); }" in content


def test_config_button_loads_config_on_demand():
    """打开配置弹窗时按需加载配置和测试数据"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "@click=\"openConfig\"" in content
    assert "const openConfig = async () =>" in content
    assert "await loadConfig();" in content
    assert "await loadTestData();" in content
