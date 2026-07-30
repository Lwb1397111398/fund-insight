"""首屏布局与手机端适配契约测试。

2026-07-30 用户反馈：
- 顶部 7 张卡片与下方 6 个按钮宽度不齐，观感乱 → 删准确率卡，两行都用等分网格
- 手机上观点页三个按钮横向溢出，必须左滑 → 改网格铺满
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "web" / "index.html"
COMMON_CSS = ROOT / "web" / "common.css"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return COMMON_CSS.read_text(encoding="utf-8")


def _media_block(css: str, header: str) -> str:
    """粗略截取某个 @media 块正文（到下一个顶层 @media 或文件末尾）。"""
    start = css.find(header)
    assert start != -1, f"找不到 {header}"
    rest = css[start + len(header):]
    nxt = rest.find("@media")
    return rest if nxt == -1 else rest[:nxt]


def test_accuracy_stat_card_is_removed_from_first_screen():
    """准确率卡片已删除；命中率仍在博主表内可查。"""
    html = _html()

    assert "stats.overall?.avg_accuracy" not in html
    assert "b.hit_rate" in html  # 博主表仍展示命中率


def test_stat_grid_and_action_bar_share_the_same_column_count():
    """六张卡片与六个按钮同为等分网格，左右边缘对齐。"""
    css = _css()

    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in css
    # action-bar 用 grid 自动等分，按钮数量变化时仍铺满整行
    assert "grid-auto-flow: column" in css
    assert "grid-auto-columns: minmax(0, 1fr)" in css


def _action_bar_blocks(css: str) -> list[str]:
    """取出所有 `.action-bar {` 规则块正文（含媒体查询内的覆盖）。"""
    blocks = []
    idx = 0
    while True:
        idx = css.find(".action-bar {", idx)
        if idx == -1:
            return blocks
        end = css.find("}", idx)
        blocks.append(css[idx:end])
        idx = end


def test_action_bar_is_grid_not_flex_column_on_mobile():
    """手机/平板下主按钮走等分网格，列数与同断点统计卡一致，不再纵向堆成长条。"""
    css = _css()
    blocks = _action_bar_blocks(css)

    assert blocks, "找不到 .action-bar 规则"
    for block in blocks:
        assert "flex-direction: column" not in block, block
    # 平板：三列（与 3 列统计卡对齐）；手机：两列（与 2 列统计卡对齐）
    three_col = [b for b in blocks if "grid-template-columns: repeat(3, minmax(0, 1fr))" in b]
    two_col = [b for b in blocks if "grid-template-columns: repeat(2, minmax(0, 1fr))" in b]
    assert three_col, "平板断点缺少三列网格"
    assert two_col, "手机断点缺少两列网格"


def test_viewpoint_buttons_wrap_instead_of_overflowing_on_phone():
    """观点页抓取/分析/汇总按钮在手机上换行铺满，不横向溢出。"""
    css = _css()
    block = _media_block(css, "@media (max-width: 560px)")

    assert ".viewpoint-fetch-control" in block
    assert "display: grid" in block
    assert "width: 100%" in block
    # 基础样式允许换行
    assert ".viewpoint-fetch-control { display: flex; gap: 6px; position: relative; flex-wrap: wrap; }" in css


def test_source_menu_stays_inside_viewport_on_phone():
    """来源下拉在小屏不超出视口宽度。"""
    css = _css()
    block = _media_block(css, "@media (max-width: 560px)")

    assert "calc(100vw - 32px)" in block


def test_get_requests_have_a_timeout_so_pages_cannot_hang_forever():
    """GET 请求兜底超时，避免服务假死时页面永远显示"加载中"。"""
    html = _html()

    assert "axios.interceptors.request.use" in html
    assert "cfg.timeout = 60000" in html
    assert "ECONNABORTED" in html  # 超时给出可重试文案
