from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "web" / "index.html"


def test_frontend_uses_restrained_product_color_tokens():
    """产品界面应使用克制的 OKLCH 色彩变量，避免营销式渐变顶栏"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert "--color-primary: oklch(" in content
    assert "--color-bg: oklch(" in content
    assert ".header {\n            background: linear-gradient" not in content


def test_frontend_has_keyboard_focus_and_subtle_card_motion():
    """主要交互组件应有键盘焦点态，卡片 hover 不应依赖位移动画"""
    content = INDEX_HTML.read_text(encoding="utf-8")

    assert ":focus-visible" in content
    assert ".stat-mini:hover {\n            transform: translateY" not in content
    assert ".action-btn:hover {\n            transform: translateY" not in content
