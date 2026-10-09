"""前端静态资源契约测试。

没有浏览器可跑，所以这里检查的是**可静态验证的硬约束**：安全渲染方式、无外部依赖、
只读、可访问性钩子、响应式断点、自动刷新选项。真正的 HTTP 行为由
``test_dashboard_api.py`` 用真实请求覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "mcp_agent_mailbox" / "dashboard" / "static"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 安全渲染
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"])
def test_frontend_never_uses_html_injection(app_js: str, marker: str) -> None:
    assert marker not in app_js, f"前端不得使用 {marker}"


def test_frontend_renders_text_through_textContent(app_js: str) -> None:
    assert "textContent" in app_js
    # 所有 DOM 构造都走 createElement 助手。
    assert "document.createElement" in app_js


def test_frontend_does_not_hardcode_demo_data(app_js: str) -> None:
    """不得用硬编码演示数据冒充数据库结果。"""
    for suspicious in ("[{\"account_id\":", "'acc_demo'", "MOCK_", "FAKE_"):
        assert suspicious not in app_js


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


def test_frontend_only_issues_get_requests(app_js: str) -> None:
    assert 'method: "GET"' in app_js
    for forbidden in ('method: "POST"', 'method: "PUT"', 'method: "PATCH"', 'method: "DELETE"'):
        assert forbidden not in app_js
    # 不得用 fetch 发写请求
    assert "POST" not in app_js.replace("POSTS", "")
    assert "DELETE" not in app_js


def test_page_has_no_write_controls(index_html: str) -> None:
    """页面上不应该出现任何可提交/可交互的写入口。

    页头那句"不提供发送、回复、标记已读、改状态或重试"是**声明**，所以这里检查的是
    结构而不是文案：没有表单、没有可编辑输入，按钮只有刷新与导航。
    """
    assert "<form" not in index_html
    assert "<textarea" not in index_html
    assert "<input" not in index_html
    buttons = re.findall(r"<button[^>]*>(.*?)</button>", index_html, flags=re.S)
    assert buttons, "页面应有按钮（导航与刷新）"
    allowed_labels = {"手动刷新"}
    nav_labels = {"对话", "监控"}
    for label in buttons:
        text = re.sub(r"<[^>]+>", "", label).strip()
        assert text in allowed_labels | nav_labels, f"出现非预期的按钮：{text}"
    assert "readonly-badge" in index_html
    assert "只读模式" in index_html


def test_page_explains_three_state_dimensions(index_html: str, app_js: str) -> None:
    for dimension in ("delivery", "visibility", "processing"):
        assert dimension in app_js
    assert "不等于" in app_js or "不代表" in app_js


def test_frontend_presence_labels_follow_the_process_rule(app_js: str) -> None:
    """前端必须按"只看进程"的规则显示，并且讲清注入依据。"""
    assert "在线 · 托管进程活着" in app_js
    assert "离线 · 消息排队等它上线" in app_js
    assert '"realtime"' not in app_js, "在线状态不再有 realtime 这一态"
    assert '"stale"' not in app_js, "在线状态不再有 stale 这一态"
    assert "wake_basis" in app_js, "前端要显示注入依据"
    assert "可直接注入开工" in app_js
    assert "无注入通道 · 等对方取信" in app_js
    assert "租约、心跳、能力等级都不参与这个判定" in app_js


def test_frontend_shows_host_process_not_lease(app_js: str) -> None:
    assert "托管进程 PID" in app_js
    assert "托管进程" in app_js
    assert "租约到期" not in app_js, "在线不看租约，界面上也不该把租约当在线证据"


# ---------------------------------------------------------------------------
# 令牌处理
# ---------------------------------------------------------------------------


def test_token_uses_session_storage_not_local_storage(app_js: str) -> None:
    assert "sessionStorage" in app_js
    assert "localStorage" not in app_js


def test_token_is_stripped_from_url(app_js: str) -> None:
    assert "replaceState" in app_js
    assert "hash" in app_js


def test_no_external_resources_anywhere(index_html: str, app_js: str, app_css: str) -> None:
    """不使用 CDN、远程字体或远程脚本。"""
    combined = index_html + app_js + app_css
    for pattern in ("http://", "https://", "//cdn", "unpkg", "jsdelivr", "googleapis"):
        assert pattern not in combined, f"出现外部依赖：{pattern}"
    # 只有站内相对引用
    assert 'src="/app.js"' in index_html
    assert 'href="/app.css"' in index_html


# ---------------------------------------------------------------------------
# 结构与可访问性
# ---------------------------------------------------------------------------


def test_conversations_default_and_details_grouped_under_monitor(index_html: str) -> None:
    tabs = re.findall(r'<button[^>]*role="tab"[^>]*>(.*?)</button>', index_html)
    assert tabs == ["对话", "监控"]
    assert 'aria-selected="true" data-area="conversations"' in index_html
    monitor = index_html.split('<section id="panel-monitor"', 1)[1].split("</section>", 1)[0]
    for area in ("overview", "accounts", "deliveries", "diagnostics"):
        assert f'id="panel-{area}"' in monitor
        assert f'data-monitor="{area}"' in monitor
    assert 'id="auto-refresh"' in monitor
    assert 'id="last-success"' in monitor
    assert 'id="panel-message-monitor"' in monitor
    folds = re.findall(r"<details\b[^>]*>", monitor)
    assert len(folds) >= 6
    assert all(not re.search(r"\sopen(?:\s|=|>)", fold) for fold in folds)


def test_navigation_uses_tabs_with_aria(index_html: str) -> None:
    assert 'role="tablist"' in index_html
    assert 'role="tab"' in index_html
    assert 'role="tabpanel"' in index_html
    assert 'aria-selected="true"' in index_html
    assert "aria-controls" in index_html
    assert "aria-labelledby" in index_html


def test_keyboard_support_hooks(app_js: str, app_css: str) -> None:
    assert "ArrowRight" in app_js and "ArrowDown" in app_js
    assert "event.preventDefault()" in app_js
    assert "focus-visible" in app_css
    assert "skip-link" in app_css


def test_live_regions_for_status_and_errors(index_html: str) -> None:
    assert 'aria-live="polite"' in index_html
    assert 'aria-live="assertive"' in index_html


def test_states_use_text_not_only_colour(app_js: str) -> None:
    """状态必须同时有文字与图标，不能只靠颜色。"""
    assert "GLYPHS" in app_js
    assert "presence_explanation" in app_js
    assert "wake_basis_explanation" in app_js
    assert "state_explanation" in app_js
    # 关键限制必须用文字说清楚
    assert "无注入通道 · 等对方取信" in app_js
    assert "verified" in app_js


def test_verified_is_never_inferred_from_level(app_js: str) -> None:
    """capability_level=2 不得被当成 verified=true。"""
    assert "verified=false" in app_js or "verified" in app_js
    # 不得出现"level >= 2 就显示已验证"这类逻辑
    assert not re.search(r"verified\s*[:=]\s*[^,;]*capability_level\s*>=\s*2", app_js)


# ---------------------------------------------------------------------------
# 响应式与动画
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width", ["320", "480", "767", "1024"])
def test_responsive_breakpoints_defined(app_css: str, width: str) -> None:
    """每个关键宽度都必须有对应的断点，保证 320/768/1024/1440 都可用。"""
    assert f"max-width: {width}px" in app_css, f"缺少 {width}px 断点"


def test_page_level_horizontal_scroll_is_suppressed(app_css: str) -> None:
    assert "overflow-x: hidden" in app_css


def test_wide_tables_scroll_inside_their_container(app_css: str) -> None:
    assert ".table-wrap" in app_css
    assert "overflow-x: auto" in app_css


def test_reduced_motion_is_respected(app_css: str) -> None:
    assert "prefers-reduced-motion" in app_css


# ---------------------------------------------------------------------------
# 自动刷新与刷新语义
# ---------------------------------------------------------------------------


def test_auto_refresh_options_and_default_off(index_html: str, app_js: str) -> None:
    for value in ('value="0"', 'value="5"', 'value="15"', 'value="30"'):
        assert value in index_html
    assert 'value="0" selected' in index_html, "自动刷新默认必须关闭"
    assert "setInterval" in app_js
    assert "clearInterval" in app_js


def test_last_successful_refresh_is_shown_and_failures_marked_stale(index_html: str, app_js: str) -> None:
    assert "last-success" in index_html
    assert "最后成功刷新" in index_html
    assert "lastSuccessAt" in app_js
    # 失败时必须明确说明显示的是旧数据
    assert "旧数据" in app_js


def test_stale_response_guard_exists(app_js: str) -> None:
    """旧请求的结果不得覆盖新请求：否则筛选/翻页会出现乱序。"""
    assert "nextSeq" in app_js
    assert "isCurrent" in app_js


def test_debounce_on_filters(app_js: str) -> None:
    assert "setTimeout" in app_js
    assert "clearTimeout" in app_js


# ---------------------------------------------------------------------------
# 空/加载/错误状态
# ---------------------------------------------------------------------------


def test_distinct_states_are_handled(app_js: str, app_css: str) -> None:
    for marker in ("正在加载", "鉴权失败", "数据库忙", "资源消失"):
        assert marker in app_js, f"缺少状态处理：{marker}"
    # 空状态有专门的助手，并且有对应样式（不是只靠默认文案）。
    assert "emptyLine(" in app_js
    assert ".state.is-empty" in app_css


def test_long_and_markup_content_are_flagged_inline(app_js: str) -> None:
    """正文含标记字符时要显式提示，而不是悄悄渲染或截断。"""
    assert "looksLikeMarkup" in app_js
    assert "纯文本" in app_js


def _strip_css_comments(text: str) -> str:
    """去掉 CSS 注释后再做"禁用字体"检查。

    注释里会**说明**我们刻意避开哪些字体，那是文档而不是用法；只检查真实样式规则。
    """
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def test_frontend_fonts_are_not_generic_defaults(app_css: str) -> None:
    """字体栈不得以 Arial / Inter / Roboto 一类通用默认字体为设计语言。"""
    rules = _strip_css_comments(app_css)
    declarations = re.findall(r"font-family\s*:([^;}]+)", rules, flags=re.I)
    assert declarations, "应至少声明一次字体栈"
    for declaration in declarations:
        lowered = declaration.lower()
        for banned in ("arial", "roboto", "inter", "helvetica"):
            assert banned not in lowered, f"字体栈出现通用默认字体 {banned}：{declaration.strip()}"
    assert "--font-ui" in app_css
    assert "--font-mono" in app_css
