"""基金管理页前端契约测试。"""
from pathlib import Path


def _content():
    return Path("web/index.html").read_text(encoding="utf-8")


def test_fund_view_has_loading_error_empty_and_pagination_states():
    content = _content()

    assert "fundLoading" in content
    assert "fundError" in content
    assert "fundPrevPage" in content
    assert "fundNextPage" in content
    assert "const skip = (fundPage.value - 1) * fundMeta.value.page_size" in content
    assert "skip=${skip}" in content
    assert "limit=${fundMeta.value.page_size}" in content


def test_fund_view_falls_back_to_all_when_prediction_tab_is_empty():
    content = _content()

    assert "fundFilter.value = 'all'" in content
