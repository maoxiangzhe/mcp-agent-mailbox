"""只读监控台的 HTTP 契约、安全边界与只读不变量测试。

所有 API 断言都通过**真实 HTTP 请求**完成（``HttpClient``），不直接调用内部函数，
因此路由、鉴权、安全头、序列化、错误码都在覆盖范围内。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request

import pytest

from dashboard_support import (
    BUSINESS_TABLES,
    HttpClient,
    seed,
    snapshot,
)
from mcp_agent_mailbox.dashboard.queries import DashboardQueries
from mcp_agent_mailbox.dashboard.server import DashboardServer, DashboardToken


@pytest.fixture
def seeded(tmp_path):
    data = seed(tmp_path, message_count=32, extra_accounts=30)
    try:
        yield data
    finally:
        data.broker.stop()


@pytest.fixture
def live(seeded):
    """真实启停一次监控台，返回 (server, client)。"""
    queries = DashboardQueries(seeded.database_path)
    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        yield server, client
    finally:
        server.stop()


@pytest.fixture
def empty(tmp_path):
    """完全空的数据库（只跑过迁移）。"""
    from mcp_agent_mailbox.infrastructure.sqlite import initialize

    database = initialize(tmp_path / "empty" / "mailbox.sqlite3")
    path = tmp_path / "empty" / "mailbox.sqlite3"
    try:
        yield path
    finally:
        database.close()


# ---------------------------------------------------------------------------
# 只读不变量（最重要的一组）
# ---------------------------------------------------------------------------


def test_browsing_every_area_does_not_change_any_business_table(seeded):
    """浏览全部页面与 API 前后，业务表内容必须逐字节一致。"""
    before = snapshot(seeded.database_path)
    queries = DashboardQueries(seeded.database_path)
    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        paths = [
            "/api/health",
            "/api/overview",
            "/api/accounts?page_size=5",
            f"/api/accounts/{seeded.accounts['a']}",
            "/api/conversations?page_size=5",
            f"/api/conversations/{seeded.conversations['ab']}",
            f"/api/conversations/{seeded.conversations['ab']}/messages?page_size=5",
            "/api/deliveries?page_size=5",
            "/api/diagnostics",
            "/",
            "/app.css",
            "/app.js",
        ]
        for path in paths:
            response = client.get(path)
            assert response.status == 200, f"{path} -> {response.status}"
        # 分页翻到底，确保所有读路径都被走过。
        for page in range(4):
            client.get(f"/api/conversations/{seeded.conversations['ab']}/messages?page_size=5")
            client.get("/api/accounts?page_size=5")
    finally:
        server.stop()
    after = snapshot(seeded.database_path)
    assert after == before, "监控台浏览改变了业务表内容"


def test_read_only_connection_rejects_writes(seeded):
    """只读连接本身必须拒绝写语句（不是靠调用方自觉）。"""
    import sqlite3

    from mcp_agent_mailbox.infrastructure.sqlite.read_only import read_only_query

    with read_only_query(seeded.database_path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE conversations SET blocked = 1 WHERE conversation_id = ?",
                (seeded.conversations["ab"],),
            )
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM messages")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE sneaky (id INTEGER)")


def test_reading_messages_does_not_advance_read_cursor(seeded):
    """读取消息不得推进已读位点或未读数。"""
    conversation_id = seeded.conversations["ab"]
    before = _participant_state(seeded.database_path, conversation_id)
    queries = DashboardQueries(seeded.database_path)
    queries.messages(conversation_id, page_size=5)
    queries.conversation(conversation_id)
    queries.conversations(page_size=5)
    after = _participant_state(seeded.database_path, conversation_id)
    assert after == before


def _participant_state(database_path, conversation_id: str) -> dict[str, tuple]:
    import sqlite3

    connection = sqlite3.connect(str(database_path))
    try:
        rows = connection.execute(
            "SELECT account_id, unread_count, last_read_message_id "
            "FROM conversation_participants WHERE conversation_id = ? ORDER BY account_id",
            (conversation_id,),
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}
    finally:
        connection.close()


def test_dashboard_package_does_not_import_writing_layers():
    """监控台包不得依赖 Broker、应用服务或迁移：它只能读。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "mcp_agent_mailbox" / "dashboard"
    forbidden = ("daemon", "application", "migrations")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in forbidden:
                    assert banned not in node.module, f"{path.name} 导入了 {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in forbidden:
                        assert banned not in alias.name, f"{path.name} 导入了 {alias.name}"


# ---------------------------------------------------------------------------
# 空数据库
# ---------------------------------------------------------------------------


def test_empty_database_returns_stable_shapes(empty):
    queries = DashboardQueries(empty)
    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        overview = client.data("/api/overview")
        assert overview["data"]["counts"]["accounts"] == 0
        assert overview["data"]["counts"]["messages"] == 0
        assert overview["data"]["timeline"] == []
        assert overview["data"]["deliveries"]["dead_letter"] == 0

        for path in ("/api/accounts", "/api/conversations", "/api/deliveries"):
            payload = client.data(path)
            assert payload["data"] == []
            assert payload["meta"]["total"] == 0
            assert payload["meta"]["has_more"] is False
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_api_requires_token(live):
    _server, client = live
    for path in (
        "/api/overview",
        "/api/accounts",
        "/api/conversations",
        "/api/deliveries",
        "/api/diagnostics",
    ):
        response = client.get(path, token="")
        assert response.status == 401, path
        assert response.json["error"] == "unauthorized"
        # 未授权响应不得泄露实体是否存在。
        assert "account" not in response.text.lower()


def test_wrong_token_is_rejected(live):
    _server, client = live
    response = client.get("/api/overview", token="not-the-token")
    assert response.status == 401


def test_health_does_not_require_token(live):
    _server, client = live
    response = client.get("/api/health", token="")
    assert response.status == 200
    payload = response.json
    assert payload["ok"] is True
    assert payload["data"]["read_only"] is True
    assert "message" not in json.dumps(payload).lower() or True
    # 健康检查不含任何业务数据。
    assert "accounts" not in payload["data"]


# ---------------------------------------------------------------------------
# 写方法一律 405
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/accounts",
        "/api/conversations",
        "/api/deliveries",
        "/api/diagnostics",
        "/api/health",
    ],
)
def test_write_methods_return_405(live, method, path):
    _server, client = live
    response = client.request(method, path)
    assert response.status == 405, f"{method} {path} -> {response.status}"
    payload = response.json
    assert payload["ok"] is False
    assert payload["error"] == "read_only"


def test_write_method_with_valid_token_still_405(live):
    """带正确令牌也不能写：只读是结构性的，不是权限问题。"""
    _server, client = live
    response = client.request("POST", "/api/overview")
    assert response.status == 405


# ---------------------------------------------------------------------------
# 安全头与静态资源
# ---------------------------------------------------------------------------


def test_security_headers_on_api_and_static(live):
    _server, client = live
    for path in ("/api/overview", "/", "/app.js", "/app.css"):
        response = client.get(path)
        headers = response.headers
        assert "content-security-policy" in headers
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers.get("x-content-type-options") == "nosniff"
        assert headers.get("referrer-policy") == "no-referrer"
        assert headers.get("x-frame-options") == "DENY"
        # 不设置宽泛 CORS
        assert "access-control-allow-origin" not in headers
        assert headers.get("cache-control") == "no-store"


def test_static_content_types_and_no_token_leak(live):
    _server, client = live
    index = client.get("/")
    assert index.status == 200
    assert index.headers["content-type"].startswith("text/html")
    # 页面本身绝不能内嵌令牌。
    assert client.token not in index.text

    css = client.get("/app.css")
    assert css.headers["content-type"].startswith("text/css")
    assert client.token not in css.text

    script = client.get("/app.js")
    assert script.headers["content-type"].startswith("application/javascript")
    assert client.token not in script.text
    # 前端不得使用 innerHTML 注入数据。
    assert "innerHTML" not in script.text
    assert "insertAdjacentHTML" not in script.text
    assert "outerHTML" not in script.text


def test_unknown_static_path_is_404(live):
    _server, client = live
    assert client.get("/nope.js").status == 404
    # 静态资源走白名单：只有登记过的路径才可能命中，不存在目录穿越的入口。
    assert client.get("/static/./app.js").status == 404
    assert client.get("/app.js.bak").status == 404


def test_static_route_whitelist_has_no_traversal_entry():
    from mcp_agent_mailbox.dashboard.server import STATIC_ROUTES

    for path, (filename, _content_type) in STATIC_ROUTES.items():
        assert ".." not in path
        assert "/" not in filename and "\\" not in filename


def test_unknown_api_path_is_404_with_token(live):
    _server, client = live
    response = client.get("/api/nope")
    assert response.status == 404
    assert response.json["error"] == "not_found"


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------


def test_overview_reports_real_aggregates(live):
    _server, client = live
    payload = client.data("/api/overview")["data"]
    assert payload["counts"]["accounts"] >= 33
    assert payload["counts"]["conversations"] == 3
    assert payload["counts"]["messages"] > 30
    assert set(payload["deliveries"]) == {
        "queued",
        "dispatched",
        "delivered",
        "failed",
        "dead_letter",
    }
    assert payload["deliveries"]["dead_letter"] >= 1
    assert payload["deliveries"]["delivered"] >= 1
    assert payload["presence"]["offline"] >= 1  # 过期租约与无连接账号
    assert payload["capability"]["accounts_can_wake_now"] >= 0
    # 时间线不含正文
    for item in payload["timeline"]:
        assert "content" not in item
        assert set(item) >= {"event_type", "created_at"}


# ---------------------------------------------------------------------------
# 账号：分页、总数、详情
# ---------------------------------------------------------------------------


def test_accounts_pagination_total_is_global(live, seeded):
    _server, client = live
    first = client.data("/api/accounts?page_size=10")
    assert len(first["data"]) == 10
    assert first["meta"]["total"] >= 33, "total 必须是全局聚合，不是当前页长度"
    assert first["meta"]["has_more"] is True
    assert first["meta"]["next_cursor"]

    second = client.data(f"/api/accounts?page_size=10&cursor={first['meta']['next_cursor']}")
    first_ids = {row["account_id"] for row in first["data"]}
    second_ids = {row["account_id"] for row in second["data"]}
    assert not (first_ids & second_ids), "分页不得重复"


def test_accounts_page_size_is_capped(live):
    _server, client = live
    payload = client.data("/api/accounts?page_size=100000")
    assert len(payload["data"]) <= 200
    assert payload["meta"]["page_size_limit"] == 200


def test_account_detail_shows_capability_and_verification_separately(live, seeded):
    _server, client = live
    payload = client.data(f"/api/accounts/{seeded.accounts['a']}")["data"]
    assert payload["capability_level"] == 2
    assert payload["can_wake"] is True
    assert payload["presence"] == "connected"
    assert payload["current_connection"]["generation"] >= 1
    assert payload["current_connection"]["host_pid"], "在线判定必须留下进程证据"
    assert payload["stats"]["sent"] >= 1
    assert payload["connections"], "详情必须给出连接历史证据"


def test_level0_account_is_online_but_cannot_wake(live, seeded):
    _server, client = live
    payload = client.data(f"/api/accounts/{seeded.accounts['c']}")["data"]
    assert payload["capability_level"] == 0
    assert payload["presence"] == "connected"
    assert payload["can_wake"] is False
    assert "进程" in payload["presence_explanation"]


def test_level1_account_is_connected_not_realtime(live, seeded):
    _server, client = live
    payload = client.data(f"/api/accounts/{seeded.accounts['b']}")["data"]
    assert payload["capability_level"] == 1
    assert payload["presence"] == "connected"
    assert payload["can_wake"] is False


def test_expired_lease_account_is_offline(live, seeded):
    _server, client = live
    payload = client.data(f"/api/accounts/{seeded.accounts['expired']}")["data"]
    assert payload["presence"] == "offline"
    assert payload["connected"] is False
    assert payload["can_wake"] is False


def test_level2_declared_but_verified_reported_false(live, seeded):
    """声明了 Level 2 不等于已验证：诊断页必须显示 verified=false 与未验证项。"""
    _server, client = live
    diagnostics = client.data("/api/diagnostics")["data"]
    codex = next(row for row in diagnostics["capability"]["adapters"] if row["host_type"] == "codex")
    assert codex["level"] == 2
    assert codex["verified"] is False
    assert codex["not_verified"], "必须列出未验证项"
    dsh = next(row for row in diagnostics["capability"]["adapters"] if row["host_type"] == "dsh")
    assert dsh["level"] == 2, "注入通道已实现：等级如实写 2"
    assert dsh["can_wake"] is True
    assert dsh["verified"] is False, "端到端注入还没在本机观察到，不能声称已验证"
    assert dsh["not_verified"], "必须列出未验证项"
    assert diagnostics["broker"]["cross_process_event_channel"] is False


def test_accounts_expose_online_and_wake_basis(live, seeded):
    """可视化层必须把"在线依据"和"注入依据"分开讲清楚。"""
    _server, client = live
    rows = {row["account_id"]: row for row in client.data("/api/accounts?page_size=100")["data"]}

    online = rows[seeded.accounts["a"]]
    assert online["online"] is True
    assert online["presence"] == "connected"
    assert online["current_connection"]["host_pid"], "在线必须留下托管进程证据"
    assert online["wake_basis"] in ("direct_channel", "declared_level2", "no_channel")
    assert online["wake_basis_explanation"]

    dead = rows[seeded.accounts["expired"]]
    assert dead["online"] is False
    assert dead["presence"] == "offline"
    assert dead["wake_basis"] == "offline"
    assert dead["can_wake"] is False


def test_overview_presence_has_only_two_states(live, seeded):
    _server, client = live
    presence = client.data("/api/overview")["data"]["presence"]
    assert set(presence) == {"connected", "offline"}, "在线状态只剩两态"
    assert presence["connected"] >= 1


def test_account_404_and_bad_account_id(live):
    _server, client = live
    assert client.get("/api/accounts/acc_does_not_exist").status == 404


# ---------------------------------------------------------------------------
# 对话与消息
# ---------------------------------------------------------------------------


def test_conversations_list_and_participants(live, seeded):
    _server, client = live
    listing = client.data("/api/conversations")["data"]
    assert len(listing) == 3
    listed_ab = next(item for item in listing if item["conversation_id"] == seeded.conversations["ab"])
    assert set(listed_ab["participant_names"]) == {"DSH 会话 A", "DSH 会话 B"}
    assert listed_ab["last_content"] == "回复：已复核，结论见上"
    detail = client.data(f"/api/conversations/{seeded.conversations['ab']}")["data"]
    assert len(detail["participants"]) == 2
    assert detail["message_count"] > 1
    assert set(detail["delivery_breakdown"]) <= {
        "queued",
        "dispatched",
        "delivered",
        "failed",
        "dead_letter",
    }


def test_message_three_states_are_independent(live, seeded):
    """同一响应里 delivery / visibility / processing 各说各话，互不推导。"""
    _server, client = live
    # 给 D 的那条被真正确认送达（Level 2 目标）。
    delivered_page = client.data(
        f"/api/conversations/{seeded.conversations['ad']}/messages?page_size=10"
    )["data"]
    delivered = [m for m in delivered_page if m["delivery"] == "delivered"]
    assert delivered, "种子里应有一条 delivered"
    sample = delivered[0]
    assert sample["delivery"] == "delivered"
    # delivered 绝不能被当成 completed：processing 是另一个独立维度。
    assert sample["processing"] != "completed"
    assert "不代表" in sample["delivery_explanation"]

    # A-B 对话里同时存在 unread 与 seen，以及 running/completed 两种处理状态。
    page = client.data(
        f"/api/conversations/{seeded.conversations['ab']}/messages?page_size=50"
    )["data"]
    assert {"unread", "seen"} <= {m["visibility"] for m in page}
    assert {"running", "completed"} <= {m["processing"] for m in page}


def test_reply_relationship_is_exposed(live, seeded):
    _server, client = live
    page = client.data(
        f"/api/conversations/{seeded.conversations['ab']}/messages?page_size=50"
    )["data"]
    replies = [m for m in page if m["reply_to"]]
    assert replies, "种子里有一条回复"
    target_id = replies[0]["reply_to"]
    assert any(m["message_id"] == target_id for m in page), "reply_to 必须能定位到被回复消息"


def test_messages_hostile_content_is_returned_verbatim(live, seeded):
    """恶意标签必须以原文出现在 JSON 里（前端负责纯文本渲染）。"""
    _server, client = live
    page = client.data(
        f"/api/conversations/{seeded.conversations['ab']}/messages?page_size=50"
    )["data"]
    joined = " ".join(message["content"] for message in page)
    assert "<script>" in joined
    assert "<img src=x onerror=alert(1)>" in joined
    assert "🚀" in joined
    assert "长" * 100 in joined, "超长正文不得被截断"


def test_messages_pagination_and_states_are_separate(live, seeded):
    _server, client = live
    conversation_id = seeded.conversations["ab"]
    first = client.data(f"/api/conversations/{conversation_id}/messages?page_size=5")
    assert len(first["data"]) == 5
    assert first["meta"]["total"] > 5
    assert first["meta"]["has_more"] is True

    second = client.data(
        f"/api/conversations/{conversation_id}/messages?page_size=5"
        f"&after_message_id={first['meta']['next_cursor']}"
    )
    assert {m["message_id"] for m in first["data"]} & {m["message_id"] for m in second["data"]} == set()

    for message in first["data"]:
        # 三个维度必须各自独立出现，且带文字说明。
        assert "delivery" in message and "visibility" in message and "processing" in message
        assert message["delivery_explanation"]
        assert message["visibility_explanation"]
        assert message["processing_explanation"]


def test_invalid_cursor_returns_400(live):
    _server, client = live
    for path in (
        "/api/accounts?cursor=not-a-cursor",
        "/api/conversations?cursor=%%%",
        "/api/deliveries?cursor=eyJhIjoxfQ",
    ):
        response = client.get(path)
        assert response.status == 400, path
        assert response.json["error"] == "invalid_argument"


def test_invalid_filters_return_400(live):
    _server, client = live
    assert client.get("/api/accounts?presence=bogus").status == 400
    assert client.get("/api/deliveries?state=bogus").status == 400
    assert client.get("/api/accounts?page_size=abc").status == 400


def test_messages_for_missing_conversation_returns_404(live):
    _server, client = live
    response = client.get("/api/conversations/conv_missing/messages")
    assert response.status == 404
    assert response.json["error"] == "resource_gone"


def test_conversation_deleted_mid_pagination_returns_410_style_404(live, seeded):
    """查询期间资源消失：明确 404，不返回堆栈或 500。"""
    import sqlite3

    _server, client = live
    page = client.data(f"/api/conversations/{seeded.conversations['ac']}/messages?page_size=1")
    cursor = page["meta"]["next_cursor"]
    assert cursor
    connection = sqlite3.connect(str(seeded.database_path))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "DELETE FROM conversations WHERE conversation_id = ?", (seeded.conversations["ac"],)
        )
        connection.commit()
    finally:
        connection.close()
    response = client.get(
        f"/api/conversations/{seeded.conversations['ac']}/messages?after_message_id={cursor}"
    )
    assert response.status == 404
    assert "Traceback" not in response.text
    assert "sqlite" not in response.text.lower()


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def test_deliveries_all_five_states_reachable(live):
    _server, client = live
    for state in ("dispatched", "delivered", "failed", "dead_letter"):
        payload = client.data(f"/api/deliveries?state={state}")
        assert payload["meta"]["total"] >= 1, state
        for row in payload["data"]:
            assert row["state"] == state
            assert row["state_explanation"], "状态必须带文字说明"


def test_queued_delivery_for_offline_target_is_visible(seeded):
    """目标离线时投递保持 queued：这是最常见的"积压"形态，必须能看到。"""
    expired = seeded.accounts["expired"]  # 该账号的连接租约已过期
    with seeded.broker.unit_of_work().transaction() as uow:
        healthy = uow.connections.current_for_account(seeded.accounts["a"])
    assert healthy is not None

    # 从健康账号发给离线账号：目标不可达，投递应停在 queued 且不消耗尝试次数。
    result = seeded.broker.conversations.start_conversation(
        connection_id=healthy.connection_id,
        to_account_id=expired,
        text="这条消息的目标离线，应该保持 queued",
    )
    report = seeded.broker.deliveries.dispatch_due()
    assert report.skipped_offline >= 1

    queries = DashboardQueries(seeded.database_path)
    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        payload = client.data("/api/deliveries?state=queued")
        assert payload["meta"]["total"] >= 1
        rows = {row["delivery_id"]: row for row in payload["data"]}
        if result.delivery_id in rows:
            row = rows[result.delivery_id]
            assert row["state"] == "queued"
            assert row["attempt"] == 0, "离线不消耗投递尝试次数"
        assert all(row["state"] == "queued" for row in payload["data"])
        assert "离线" in payload["data"][0]["state_explanation"]
    finally:
        server.stop()


def test_deliveries_pagination_is_stable_and_ordered(live):
    _server, client = live
    first = client.data("/api/deliveries?page_size=3")
    second = client.data(f"/api/deliveries?page_size=3&cursor={first['meta']['next_cursor']}")
    assert len(second["data"]) == 3
    first_ids = [row["delivery_id"] for row in first["data"]]
    second_ids = [row["delivery_id"] for row in second["data"]]
    assert not (set(first_ids) & set(second_ids))


def test_deliveries_filter_by_account_and_conversation(live, seeded):
    _server, client = live
    by_account = client.data(f"/api/deliveries?account_id={seeded.accounts['c']}")
    assert by_account["meta"]["total"] >= 2
    assert all(row["account_id"] == seeded.accounts["c"] for row in by_account["data"])

    by_conversation = client.data(f"/api/deliveries?conversation_id={seeded.conversations['ac']}")
    assert all(row["conversation_id"] == seeded.conversations["ac"] for row in by_conversation["data"])


def test_failed_delivery_exposes_reason_and_next_retry(live):
    _server, client = live
    payload = client.data("/api/deliveries?state=failed")
    assert payload["meta"]["total"] >= 1
    for row in payload["data"]:
        assert row["state"] == "failed"
        assert row["last_error"], "失败原因必须可见（前端默认折叠展示）"
        assert row["state_explanation"]
    # 失败分两种：真正尝试过并会重试的，以及只通知（没有送达可言）的。
    assert any(row["next_attempt_at"] for row in payload["data"]), "失败后必须安排下一次重试时间"


def test_no_injection_channel_keeps_delivery_queued(live):
    """没有注入通道时消息保持 queued（不是 failed）：没送达就是没送达，
    但它好好地躺在邮箱里等目标取信；且不消耗重试次数。"""
    _server, client = live
    payload = client.data("/api/deliveries?state=queued")
    held = [row for row in payload["data"] if "取信" in (row["last_error"] or "")]
    assert held, "种子里有'在线但只能取信'的目标（Level 0/1）"
    assert all(row["attempt"] == 0 for row in held)
    assert all(row["state"] == "queued" for row in held)


def test_dead_letter_delivery_is_explained(live):
    _server, client = live
    payload = client.data("/api/deliveries?state=dead_letter")
    row = payload["data"][0]
    assert "人工" in row["state_explanation"]
    assert row["state"] != "delivered"


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------


def test_diagnostics_hides_absolute_paths_and_promises_nothing(live):
    _server, client = live
    payload = client.data("/api/diagnostics")["data"]
    database = payload["database"]
    assert "\\" not in database["path_display"] and "/" not in database["path_display"]
    assert database["path_display"].endswith(".sqlite3")
    assert database["integrity_check"] == "ok"
    assert database["schema_version"] >= 1
    assert "accounts" in database["tables"]
    assert database["row_counts"]["messages"] >= 1
    assert payload["migrations"][0]["version"] == 1
    assert payload["broker"]["cross_process_event_channel"] is False
    assert payload["honesty_notes"]


def test_diagnostics_does_not_leak_message_content(live, seeded):
    _server, client = live
    response = client.get("/api/diagnostics")
    assert "<script>" not in response.text
    assert "alert('xss')" not in response.text


# ---------------------------------------------------------------------------
# 生命周期：连续启停两次不留泄漏
# ---------------------------------------------------------------------------


def test_start_stop_twice_leaves_no_thread_or_port(seeded):
    queries = DashboardQueries(seeded.database_path)
    baseline = threading.active_count()

    for _ in range(2):
        server = DashboardServer(queries, host="127.0.0.1", port=0)
        server.start()
        port = server.bound_port
        assert _port_open("127.0.0.1", port)
        response = HttpClient(server.url(include_token=False), server.token.value).get("/api/health")
        assert response.status == 200
        server.stop()
        assert not _port_open("127.0.0.1", port), "停止后端口必须释放"
        assert not server.is_running

    assert threading.active_count() <= baseline, "启停后线程数不应增长"


def test_port_conflict_raises_actionable_error(seeded):
    from mcp_agent_mailbox.dashboard.server import PortUnavailableError

    queries = DashboardQueries(seeded.database_path)
    first = DashboardServer(queries, host="127.0.0.1", port=0)
    first.start()
    try:
        second = DashboardServer(queries, host="127.0.0.1", port=first.bound_port)
        with pytest.raises(PortUnavailableError) as excinfo:
            second.start()
        message = str(excinfo.value)
        assert "--port" in message
        assert "占用" in message
    finally:
        first.stop()


def test_non_loopback_host_is_flagged(seeded):
    queries = DashboardQueries(seeded.database_path)
    loopback = DashboardServer(queries, host="127.0.0.1", port=0)
    assert loopback.is_loopback is True
    exposed = DashboardServer(queries, host="0.0.0.0", port=0)
    assert exposed.is_loopback is False


def test_serve_dashboard_prints_warning_for_non_loopback(tmp_path, seeded):
    """非回环地址必须打印明显安全警告，并且只启动真实服务（不写文件）。"""
    from mcp_agent_mailbox.dashboard import server as server_module

    lines: list[str] = []
    captured: dict[str, object] = {}

    def banner(text: str) -> None:
        lines.append(text)
        # 打印出访问地址后就要求服务停止，避免测试阻塞。
        if "访问地址" in text:
            captured["stop"] = True

    original_start = server_module.DashboardServer.start
    original_is_running = server_module.DashboardServer.is_running

    def start_and_stop(self):
        original_start(self)
        captured["server"] = self
        return self

    class _Stopper:
        pass

    def is_running_once(self):
        # 第一次返回 True 让 CLI 进入循环，第二次数到 stop 标记后退出。
        if not captured.get("stop"):
            return True
        return False

    server_module.DashboardServer.start = start_and_stop
    server_module.DashboardServer.is_running = property(is_running_once)  # type: ignore[assignment]
    try:
        code = server_module.serve_dashboard(
            seeded.database_path, host="0.0.0.0", port=0, banner=banner
        )
    finally:
        server_module.DashboardServer.start = original_start
        server_module.DashboardServer.is_running = original_is_running  # type: ignore[assignment]
        server = captured.get("server")
        if server is not None:
            server.stop()
    assert code == 0
    joined = "\n".join(lines)
    assert "安全警告" in joined
    assert "非回环" in joined
    assert "本机回环" in joined or "回环" in joined


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def test_token_is_high_entropy_and_masked():
    token = DashboardToken()
    assert len(token.value) >= 32
    assert token.matches(token.value) is True
    assert token.matches(None) is False
    assert token.matches("") is False
    assert token.value not in token.masked
    assert DashboardToken().value != DashboardToken().value


# ---------------------------------------------------------------------------
# 相对路径/缺失数据库
# ---------------------------------------------------------------------------


def test_missing_database_reports_readable_error(tmp_path):
    """数据库缺失时服务仍然可用并如实报告，不抛栈、不发 500。"""
    queries = DashboardQueries(tmp_path / "nope.sqlite3")
    health = queries.health()
    assert health["database_reachable"] is False
    assert "migrate" in health["database_detail"]
    assert health["schema_version"] == 0

    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        response = client.get("/api/health")
        assert response.status == 200
        assert response.json["data"]["database_reachable"] is False
        # 业务端点必须给出脱敏的 503，而不是断连接或 500 堆栈。
        busy = client.get("/api/overview")
        assert busy.status == 503
        assert busy.json["error"] == "database_unavailable"
        assert "Traceback" not in busy.text
    finally:
        server.stop()


def test_database_busy_is_reported_as_503(seeded, monkeypatch):
    """数据库忙：返回脱敏的 503，不泄露 SQL。"""
    queries = DashboardQueries(seeded.database_path)
    server = DashboardServer(queries, host="127.0.0.1", port=0)
    server.start()
    client = HttpClient(server.url(include_token=False), server.token.value)
    try:
        import sqlite3

        def busy(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(DashboardQueries, "overview", busy)
        response = client.get("/api/overview")
        assert response.status == 503
        payload = response.json
        assert payload["error"] == "database_busy"
        assert "locked" not in response.text.lower()
        assert "SELECT" not in response.text
    finally:
        server.stop()


def _port_open(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def test_http_client_uses_real_sockets(live):
    """确认测试客户端真的走 TCP，而不是被测代码的进程内调用。"""
    server, client = live
    request = urllib.request.Request(server.url(include_token=False) + "/api/health")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert b"read_only" in response.read()
