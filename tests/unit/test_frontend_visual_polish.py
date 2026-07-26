import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"
COMMON_CSS = PROJECT_ROOT / "web" / "common.css"


def _combined_assets() -> str:
    """合并 index.html 与 common.css，供样式相关断言使用。样式已从
    index.html 提取到 common.css，单读 index.html 会漏掉所有 CSS。"""
    parts = [INDEX_HTML.read_text(encoding="utf-8")]
    if COMMON_CSS.exists():
        parts.append(COMMON_CSS.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _rule_block(css: str, selector: str) -> str:
    """从 CSS 文本中截取某个选择器规则块（到第一个闭合大括号为止）。"""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    return m.group(1) if m else ""


def test_frontend_uses_restrained_product_color_tokens():
    """产品界面应使用克制的 OKLCH 色彩变量，避免营销式渐变顶栏"""
    content = _combined_assets()

    assert "--color-primary: oklch(" in content
    assert "--color-bg: oklch(" in content
    # 顶栏不应使用营销式渐变背景
    header_block = _rule_block(content, ".header")
    assert header_block, "缺少 .header 规则"
    assert "linear-gradient" not in header_block


def test_frontend_has_keyboard_focus_and_subtle_card_motion():
    """主要交互组件应有键盘焦点态，卡片 hover 不应依赖位移动画"""
    content = _combined_assets()

    assert ":focus-visible" in content
    # 卡片 hover 不应依赖 translateY 位移动画
    stat_mini_block = _rule_block(content, ".stat-mini:hover")
    assert stat_mini_block, "缺少 .stat-mini:hover 规则"
    assert "transform: translateY" not in stat_mini_block
    action_btn_block = _rule_block(content, ".action-btn:hover")
    assert action_btn_block, "缺少 .action-btn:hover 规则"
    assert "transform: translateY" not in action_btn_block


def test_index_html_has_no_orphan_css_in_head():
    """index.html 的 <head> 内不应残留未包裹在 <style> 标签里的 CSS 文本。
    样式已提取到 common.css；若 head 里直接出现 :root/oklch 等 CSS，
    说明提取时漏删了原 <style> 正文，浏览器会把它当文本渲染成乱码。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]

    # 没有 <style> 标签时，head 内不得出现裸 CSS 文本
    if "<style" not in head:
        for marker in (":root", "oklch(", "--color-primary:"):
            assert marker not in head, f"<head> 内出现孤立 CSS 文本: {marker}"
