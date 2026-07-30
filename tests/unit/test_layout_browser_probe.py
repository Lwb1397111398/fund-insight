"""用真实浏览器量首屏对齐与手机端不溢出（跑不动浏览器时自动 skip）。

纯 CSS 文本断言只能证明"写了规则"，证明不了"渲染出来是齐的"。
这里起真实 app + Chromium，量像素。
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "layout-probe-pass"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    import uvicorn

    db_path = tmp_path_factory.mktemp("layout") / "probe.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["ACCESS_PASSWORD"] = PASSWORD

    from src.models.database import Base, engine

    Base.metadata.create_all(engine)
    from src.api.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        pytest.skip("本地服务未能启动")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # 浏览器未安装等环境问题
            pytest.skip(f"Chromium 不可用: {exc}")
        yield b
        b.close()


def _open_app(browser, base_url, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.add_init_script(f"localStorage.setItem('access_password', {PASSWORD!r})")
    page.goto(f"{base_url}/web/index.html", wait_until="networkidle")
    page.wait_for_selector(".stats-mini-grid", timeout=15000)
    return page


@pytest.mark.parametrize(
    "width,height",
    [(1440, 900), (1280, 900), (1024, 800), (900, 800), (768, 800), (600, 800), (480, 800), (390, 844), (360, 780)],
)
def test_stat_cards_and_action_buttons_align_on_desktop(browser, live_server, width, height):
    """各宽度下六张卡与主按钮左右边缘对齐、同行列宽一致、无横向溢出。"""
    page = _open_app(browser, live_server, width, height)
    try:
        cards = page.eval_on_selector_all(
            ".stats-mini-grid .stat-mini", "els => els.map(e => e.getBoundingClientRect())"
        )
        buttons = page.eval_on_selector_all(
            ".action-bar .action-btn", "els => els.map(e => e.getBoundingClientRect())"
        )
        assert len(cards) == 6, f"首屏卡片应为 6 张，实际 {len(cards)}"
        assert buttons, "找不到主操作按钮"

        # 两行左右边缘对齐（容差 1px 处理亚像素）
        assert abs(cards[0]["x"] - buttons[0]["x"]) <= 1
        card_right = cards[-1]["x"] + cards[-1]["width"]
        btn_right = buttons[-1]["x"] + buttons[-1]["width"]
        assert abs(card_right - btn_right) <= 1, f"右缘不齐: {card_right} vs {btn_right} @{width}px"

        # 卡片列宽一致
        assert max(c["width"] for c in cards) - min(c["width"] for c in cards) <= 1

        # 无横向溢出
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth > window.innerWidth + 1"
        )
        assert overflow is False, f"页面横向溢出 @{width}px"

        # 单行且按钮数等于卡片数时，逐列对齐
        rows = {round(b["y"]) for b in buttons}
        if len(rows) == 1 and len(buttons) == len(cards):
            for card, btn in zip(cards, buttons):
                assert abs(card["x"] - btn["x"]) <= 1
                assert abs(card["width"] - btn["width"]) <= 1
    finally:
        page.close()


def test_action_bar_row_gap_matches_stat_grid(browser, live_server):
    """桌面端两行水平间距一致——间距不同则列不可能真正对齐。"""
    page = _open_app(browser, live_server, 1440, 900)
    try:
        def gaps(selector):
            return page.eval_on_selector_all(
                selector,
                "els => els.length < 2 ? null :"
                " Math.round(els[1].getBoundingClientRect().left"
                " - els[0].getBoundingClientRect().right)",
            )

        card_gap = gaps(".stats-mini-grid .stat-mini")
        btn_gap = gaps(".action-bar .action-btn")
        assert card_gap is not None and btn_gap is not None
        assert abs(card_gap - btn_gap) <= 1, f"两行间距不一致: 卡片 {card_gap}px vs 按钮 {btn_gap}px"
    finally:
        page.close()


@pytest.mark.parametrize("width,height", [(390, 844), (360, 800)])
def test_nothing_overflows_horizontally_on_phone(browser, live_server, width, height):
    """手机端：页面无横向滚动，观点页按钮不出屏也无需左滑。"""
    page = _open_app(browser, live_server, width, height)
    try:
        page.click("text=管理观点")
        page.wait_for_selector(".viewpoint-fetch-control", timeout=15000)

        overflow = page.evaluate(
            "() => ({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
        )
        assert overflow["doc"] <= overflow["win"] + 1, f"页面横向溢出: {overflow}"

        # 容器自身不需要横向滑动（用户原话：每次使用必须左滑）
        scroll = page.eval_on_selector(
            ".viewpoint-fetch-control",
            "e => ({scroll: e.scrollWidth, client: e.clientWidth})",
        )
        assert scroll["scroll"] <= scroll["client"] + 1, f"按钮区仍需左滑: {scroll}"

        # 观点页三个按钮 + 来源下拉都在视口内，且文字不被裁切
        items = page.eval_on_selector_all(
            ".viewpoint-fetch-control > *",
            "els => els.filter(e => e.offsetParent !== null).map(e => {"
            " const r = e.getBoundingClientRect();"
            " return {left: r.left, right: r.right,"
            "  clipped: e.scrollWidth > e.clientWidth + 1}; })",
        )
        assert items, "找不到观点页操作按钮"
        for it in items:
            assert it["left"] >= -1, f"按钮左出屏: {it}"
            assert it["right"] <= overflow["win"] + 1, f"按钮右出屏: {it} (视口 {overflow['win']})"
            assert it["clipped"] is False, f"按钮文字被裁切: {it}"
    finally:
        page.close()


@pytest.mark.parametrize("view_label", ["管理预测", "管理观点", "投资建议"])
def test_every_view_fits_phone_width(browser, live_server, view_label):
    """逐视图确认手机端无横向溢出，并报出最宽越界元素便于定位。"""
    page = _open_app(browser, live_server, 360, 780)
    try:
        page.click(f"text={view_label}")
        page.wait_for_timeout(500)
        worst = page.evaluate(
            """() => { let over = 0, sel = '';
              document.querySelectorAll('*').forEach(e => {
                const r = e.getBoundingClientRect();
                if (r.width > 0 && r.right - window.innerWidth > over) {
                  over = r.right - window.innerWidth; sel = e.className || e.tagName; }
              });
              return {over: Math.round(over), sel: String(sel).slice(0, 60),
                      doc: document.documentElement.scrollWidth, win: window.innerWidth}; }"""
        )
        assert worst["doc"] <= worst["win"] + 1, f"{view_label} 横向溢出: {worst}"
        assert worst["over"] <= 1, f"{view_label} 有元素越界 {worst['over']}px: {worst['sel']}"
    finally:
        page.close()


def test_main_action_buttons_fit_viewport_on_phone(browser, live_server):
    """手机端主按钮在视口内，且不是每个都独占一行（避免长条）。"""
    page = _open_app(browser, live_server, 390, 844)
    try:
        buttons = page.eval_on_selector_all(
            ".action-bar .action-btn",
            "els => els.map(e => { const r = e.getBoundingClientRect();"
            " return {left: r.left, right: r.right, top: Math.round(r.top)}; })",
        )
        assert buttons, "找不到主操作按钮"
        win = page.evaluate("() => window.innerWidth")
        for b in buttons:
            assert b["left"] >= -1 and b["right"] <= win + 1, f"主按钮出屏: {b}"
        rows = {b["top"] for b in buttons}
        assert len(rows) < len(buttons), "每个按钮各占一行，说明仍在纵向堆叠"
    finally:
        page.close()
