"""MCP 层契约测试：工具形状、身份绑定、结构化结果、stdio 往返。

安全断言是重点：
    - 任何发送类工具都**不能**出现 from / agent / sender 参数；
    - 模型不能通过参数指定发送者；
    - 未取得会话身份时必须明确失败，而不是偷偷注册一个假账号。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresencePolicy
from mcp_agent_mailbox.mcp.connection_context import (
    ENV_HOST_TYPE,
    ENV_SESSION_ID,
    EnvironmentSessionProvider,
    MailboxConnection,
    SessionDescriptor,
    UnavailableSessionProvider,
)
from mcp_agent_mailbox.mcp.server import build_server
from mcp_agent_mailbox.mcp.tools import ToolRuntime, tool_error

FORBIDDEN_PARAMS = {"from", "sender", "agent", "from_account_id", "sender_account_id"}


def make_settings(data_dir: Path, **overrides):
    return load_settings(
        data_dir=data_dir,
        presence=PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10),
        rate_limits=RateLimits(),
        delivery=DeliveryPolicy(),
        **overrides,
    )


def make_server(data_dir: Path, session: str, *, host: str = "dsh", capability: int = 2, rate_limits=None):
    """构造一个绑定到共享 Broker 数据库的 MCP 服务器。

    多个服务器指向**同一个数据目录**是刻意的：设计文档要求所有 MCP 端点连接同一个
    Broker，状态只有一份。不同会话用不同会话 ID 区分账号。
    """
    settings = make_settings(data_dir)
    if rate_limits is not None:
        settings = settings.with_overrides(rate_limits=rate_limits)
    broker = Broker(settings)
    provider = EnvironmentSessionProvider(
        data_dir=settings.data_dir,
        host_type=host,
        default_capability=HostCapabilityLevel(capability),
        environ={
            ENV_SESSION_ID: session,
            ENV_HOST_TYPE: host,
            "MAILBOX_HOST_INSTANCE_ID": "test-instance",
        },
    )
    server = build_server(
        settings,
        broker=broker,
        provider=provider,
        host_type=host,
        default_capability=HostCapabilityLevel(capability),
    )
    return server


def call(server, name: str, **kwargs) -> dict:
    """经真实 MCP 工具入口调用，返回解析后的结构化结果。

    刻意走 ``mcp.call_tool`` 而不是直接调用实现函数：每个工具结果的形状
    （含统一的 ``pending`` 未读提示）是在注册层加工的，直接调实现就测不到它。
    """
    result = asyncio.run(server.mcp.call_tool(name, dict(kwargs)))
    payload = _first_text(result)
    parsed = json.loads(payload)
    assert isinstance(parsed, dict), f"{name} 返回的不是 JSON 对象"
    return parsed


def _first_text(result) -> str:
    """从 FastMCP 的返回值里取出文本内容。"""
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                return text
    text = getattr(content, "text", None)
    if text is not None:
        return text
    raise AssertionError(f"无法从工具结果中取出文本：{result!r}")


@pytest.fixture
def mailbox_home(tmp_path) -> Path:
    """所有服务器共享的数据目录。"""
    return tmp_path / "home"


@pytest.fixture
def server_a(mailbox_home):
    instance = make_server(mailbox_home, "session-a")
    try:
        yield instance
    finally:
        instance.stop()


# ---------------------------------------------------------------------------
# 工具形状
# ---------------------------------------------------------------------------


def test_tool_catalog_has_required_tools(mailbox_home) -> None:
    server = make_server(mailbox_home, "session-a")
    try:
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
    finally:
        server.stop()
    required = {
        "whoami",
        "list_contacts",
        "start_conversation",
        "send_message",
        "reply_message",
        "list_conversations",
        "read_conversation",
        "set_message_status",
    }
    assert required <= names
    assert {"mailbox_register", "mailbox_heartbeat", "mailbox_ack_delivery"} <= names


def test_connect_mailbox_has_narrow_model_facing_schema(mailbox_home) -> None:
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings),
        provider=UnavailableSessionProvider("没有会话身份（测试）"),
        host_type="codex",
        allow_adapter_registration=True,
    )
    try:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    finally:
        server.stop()

    assert "connect_mailbox" in tools
    properties = set((tools["connect_mailbox"].inputSchema or {}).get("properties", {}))
    assert properties == {"session_id", "account_name", "workspace_hint"}


def test_no_tool_accepts_sender_parameters(mailbox_home) -> None:
    """模型不能自行声明发送者：这是整个身份模型的硬约束。"""
    server = make_server(mailbox_home, "session-a")
    try:
        for tool in asyncio.run(server.mcp.list_tools()):
            properties = set((tool.inputSchema or {}).get("properties", {}))
            leaked = properties & FORBIDDEN_PARAMS
            assert not leaked, f"{tool.name} 暴露了发送者参数：{leaked}"
    finally:
        server.stop()


def test_whoami_reports_unbound_without_session_identity(mailbox_home) -> None:
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings),
        provider=UnavailableSessionProvider("没有会话身份（测试）"),
        host_type="dsh",
    )
    try:
        payload = call(server, "whoami")
    finally:
        server.stop()
    assert payload["ok"] is True
    assert payload["bound"] is False
    assert payload["database_path"] == str(settings.database_path.resolve())
    assert payload["can_wake"] is False
    assert "没有会话身份" in payload["reason"]
    assert "connect_mailbox" in payload["recovery"]


def test_send_without_identity_fails_explicitly(mailbox_home) -> None:
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings),
        provider=UnavailableSessionProvider("没有会话身份（测试）"),
        host_type="dsh",
    )
    try:
        payload = call(server, "list_contacts")
    finally:
        server.stop()
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert "尚未绑定" in payload["message"]


def test_whoami_reports_binding_details(tmp_path, server_a) -> None:
    payload = call(server_a, "whoami")
    assert payload["ok"] is True
    assert payload["bound"] is True
    assert payload["database_path"] == str(server_a.broker.settings.database_path.resolve())
    assert payload["account_id"].startswith("acc_")
    assert payload["native_session_id"] == "session-a"
    assert payload["presence"] == "connected"
    assert payload["online"] is True
    assert payload["capability_level"] == 2
    assert payload["can_wake"] is True
    assert payload["generation"] == 1


def test_tools_only_capability_is_online_but_not_wakeable(mailbox_home) -> None:
    server = make_server(mailbox_home, "session-a", capability=0)
    try:
        payload = call(server, "whoami")
    finally:
        server.stop()
    assert payload["presence"] == "connected"
    assert payload["online"] is True
    assert payload["can_receive"] is True
    assert payload["can_wake"] is False, "级别不够只代表不能被注入开工，不代表离线"


# ---------------------------------------------------------------------------
# 发送与读取
# ---------------------------------------------------------------------------


def test_start_conversation_returns_structured_result(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        payload = call(a, "start_conversation", to_account_id=b_id, text="你好")
        assert payload["ok"] is True
        assert payload["conversation_id"].startswith("conv_")
        assert payload["delivery_id"].startswith("del_")
        assert payload["delivery_state"] == "queued"
        assert payload["duplicate"] is False
        assert "不代表模型已阅读或任务完成" in payload["note"]
        assert "reply_message" in payload["note"]
    finally:
        a.stop()
        b.stop()


def test_send_is_idempotent_with_same_key(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        first = call(a, "start_conversation", to_account_id=b_id, text="只发一次", idempotency_key="k")
        second = call(a, "start_conversation", to_account_id=b_id, text="只发一次", idempotency_key="k")
        assert second["duplicate"] is True
        assert second["message_id"] == first["message_id"]
    finally:
        a.stop()
        b.stop()


def test_read_conversation_marks_seen_and_reports_states(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="读我")
        page = call(b, "read_conversation", conversation_id=started["conversation_id"])
        assert page["ok"] is True
        assert page["unread_count"] == 0
        message = page["messages"][0]
        assert message["visibility"] == "seen"
        assert message["delivery"] == "queued"
        assert message["processing"] == "pending"
        assert message["is_reply"] is False
    finally:
        a.stop()
        b.stop()


def test_reply_and_status_roundtrip(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="请复核")
        # 处理状态由**收件人**（这里是 B）回传；A 是原发送者，无权改 B 的处理状态。
        forbidden = call(a, "set_message_status", message_id=started["message_id"], processing="running")
        assert forbidden["ok"] is False
        assert forbidden["error"] == "not_a_participant"

        status = call(b, "set_message_status", message_id=started["message_id"], processing="running")
        assert status["ok"] is True
        assert status["record"]["state"] == "running"
        reply = call(b, "reply_message", message_id=started["message_id"], text="已复核")
        assert reply["ok"] is True
        assert reply["reply_to"] == started["message_id"]
        done = call(
            b,
            "set_message_status",
            message_id=started["message_id"],
            processing="completed",
            result="通过",
        )
        assert done["record"]["state"] == "completed"
        invalid = call(b, "set_message_status", message_id=started["message_id"], processing="running")
        assert invalid["ok"] is False
        assert invalid["error"] == "invalid_transition"
    finally:
        a.stop()
        b.stop()


def test_outside_account_cannot_read_others_conversation(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    c = make_server(mailbox_home, "session-c")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="私事")
        payload = call(c, "read_conversation", conversation_id=started["conversation_id"])
        assert payload["ok"] is False
        assert payload["error"] == "not_a_participant"
    finally:
        for server in (a, b, c):
            server.stop()


def test_model_registration_is_refused_by_default(tmp_path, server_a) -> None:
    """模型不能把自己注册成别的会话：默认必须拒绝显式注册。"""
    payload = call(
        server_a,
        "mailbox_register",
        host_type="dsh",
        host_instance_id="other-instance",
        native_session_id="someone-elses-session",
        capability_level=2,
    )
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert "未允许显式注册" in payload["message"]


def test_connect_mailbox_is_refused_unless_enabled(mailbox_home) -> None:
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings),
        provider=UnavailableSessionProvider("没有会话身份（测试）"),
        host_type="codex",
    )
    try:
        payload = call(
            server,
            "connect_mailbox",
            session_id="codex-thread-a",
            account_name="codex-A",
        )
    finally:
        server.stop()

    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert "未开放会话自注册" in payload["message"]


def test_connect_mailbox_binds_codex_session_at_level_zero(mailbox_home) -> None:
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings, auto_waker=False),
        provider=UnavailableSessionProvider("Codex MCP 子进程未收到会话环境变量"),
        host_type="codex",
        host_instance_id="test-codex",
        allow_adapter_registration=True,
    )
    try:
        connected = call(
            server,
            "connect_mailbox",
            session_id="codex-thread-a",
            account_name="codex-A",
            workspace_hint="E:\\zcz",
        )
        identity = call(server, "whoami")
    finally:
        server.stop()

    assert connected["ok"] is True
    assert connected["display_name"] == "codex-A"
    assert connected["native_session_id"] == "codex-thread-a"
    assert connected["capability_level"] == 0
    assert identity["bound"] is True
    assert identity["display_name"] == "codex-A"
    assert identity["host_type"] == "codex"
    assert identity["host_instance_id"] == "test-codex"
    assert identity["presence"] == "connected"
    assert identity["can_wake"] is False


def test_connect_mailbox_reuses_session_account_and_updates_name(mailbox_home) -> None:
    def connect(account_name: str) -> dict:
        settings = make_settings(mailbox_home)
        server = build_server(
            settings,
            broker=Broker(settings),
            provider=UnavailableSessionProvider("没有会话身份（测试）"),
            host_type="codex",
            host_instance_id="test-codex",
            allow_adapter_registration=True,
        )
        try:
            return call(
                server,
                "connect_mailbox",
                session_id="stable-thread-id",
                account_name=account_name,
            )
        finally:
            server.stop()

    first = connect("codex-A")
    second = connect("codex-A-renamed")

    assert first["account_id"] == second["account_id"]
    assert first["created"] is True
    assert second["created"] is False
    assert second["display_name"] == "codex-A-renamed"


def test_explicit_self_registration_binds_codex_session(mailbox_home) -> None:
    """Codex 安装器显式开放后，会话可用自己的 CODEX_SESSION_ID 完成首次绑定。"""
    settings = make_settings(mailbox_home)
    broker = Broker(settings)
    server = build_server(
        settings,
        broker=broker,
        provider=UnavailableSessionProvider("Codex MCP 子进程未收到会话环境变量"),
        host_type="codex",
        allow_adapter_registration=True,
    )
    try:
        registered = call(
            server,
            "mailbox_register",
            host_type="codex",
            host_instance_id="default",
            native_session_id="codex-thread-a",
            display_name="codex-A",
            workspace_hint="E:\\zcz",
            capability_level=0,
            adapter_name="codex-self-register",
        )
        assert registered["ok"] is True
        assert registered["descriptor"]["native_session_id"] == "codex-thread-a"
        assert registered["descriptor"]["display_name"] == "codex-A"

        identity = call(server, "whoami")
        assert identity["bound"] is True
        assert identity["native_session_id"] == "codex-thread-a"
        assert identity["display_name"] == "codex-A"
    finally:
        server.stop()


def test_contacts_list_marks_reachability(mailbox_home) -> None:
    """在线只看进程：两个对端进程都在跑，所以都在线、都可投递；
    但只有 Level 2 的那个能被注入开工。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b", capability=2)
    c = make_server(mailbox_home, "session-c", capability=0)
    try:
        call(b, "whoami")
        call(c, "whoami")
        payload = call(a, "list_contacts")
        by_id = {row["account_id"]: row for row in payload["contacts"]}
        assert len(by_id) == 2
        online = [row for row in by_id.values() if row["presence"] == "connected"]
        assert len(online) == 2, "对端进程都活着 => 都在线"
        assert all(row["reachable_now"] is True for row in online)
        wakable = [row for row in online if row["capability_level"] == 2]
        assert len(wakable) == 1 and wakable[0]["can_wake"] is True
        tools_only = [row for row in online if row["capability_level"] == 0]
        assert len(tools_only) == 1 and tools_only[0]["can_wake"] is False
    finally:
        for server in (a, b, c):
            server.stop()


def test_list_contacts_rejects_bad_status(tmp_path, server_a) -> None:
    payload = call(server_a, "list_contacts", status="invented")
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"


def test_tool_error_maps_domain_codes() -> None:
    from mcp_agent_mailbox.domain.errors import RecipientBlockedError

    payload = tool_error(RecipientBlockedError("被阻止"))
    assert payload["error"] == "recipient_blocked"
    assert payload["message"] == "被阻止"


# ---------------------------------------------------------------------------
# 适配器协议工具
# ---------------------------------------------------------------------------


def test_adapter_protocol_roundtrip(mailbox_home) -> None:
    """完整走一遍：注册 → 心跳 → 取信 → 确认投递。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="适配器投递")
        # Broker 派发（与服务层同一入口）
        report = b.broker.deliveries.dispatch_due()
        assert report.dispatched == 1
        event = b.broker.events.receive(b.connection.connection_id, 0.5)
        assert event is not None
        delivery_id = str(event.to_dict()["delivery_id"])
        assert delivery_id == started["delivery_id"]

        heartbeat = call(b, "mailbox_heartbeat")
        assert heartbeat["ok"] is True and heartbeat["generation"] == 1

        fetched = call(b, "mailbox_fetch_delivery", delivery_id=delivery_id)
        assert fetched["ok"] is True
        assert fetched["message"]["content"] == "适配器投递"
        header = fetched["envelope"]["header"]
        assert "[External agent message]" in header
        assert "Trust: untrusted peer content" in header
        assert "From:" in header

        acked = call(b, "mailbox_ack_delivery", delivery_id=delivery_id)
        assert acked["ok"] is True
        assert acked["delivery"]["state"] == "delivered"
    finally:
        a.stop()
        b.stop()


def test_fetch_delivery_is_identity_scoped(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    c = make_server(mailbox_home, "session-c")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="只给 B")
        payload = call(c, "mailbox_fetch_delivery", delivery_id=started["delivery_id"])
        assert payload["ok"] is False
        assert payload["error"] == "identity_mismatch"
    finally:
        for server in (a, b, c):
            server.stop()


def test_fail_delivery_marks_failed_and_schedules_retry(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="会失败")
        b.broker.deliveries.dispatch_due()
        payload = call(
            b, "mailbox_fail_delivery", delivery_id=started["delivery_id"], reason="注入失败"
        )
        assert payload["ok"] is True
        assert payload["delivery"]["state"] == "failed"
        assert "复用同一个 delivery_id" in payload["note"]
    finally:
        a.stop()
        b.stop()


# ---------------------------------------------------------------------------
# 收信闭环：MCP 不能主动推送，所以每个工具结果都带未读提示
# ---------------------------------------------------------------------------


def test_tool_results_carry_pending_notice(mailbox_home) -> None:
    """每个工具结果都附未读计数：这是 Level 0/1 会话发现新消息的唯一途径。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        # B 一开始没有未读。
        assert call(b, "whoami")["pending"]["count"] == 0

        call(a, "start_conversation", to_account_id=b_id, text="给你一条")
        # B 任何一次工具调用都能看到未读计数变化，不需要额外轮询机制。
        assert call(b, "whoami")["pending"]["count"] == 1
        assert call(b, "list_contacts")["pending"]["count"] == 1
        assert call(b, "list_conversations")["pending"]["count"] == 1
    finally:
        a.stop()
        b.stop()


def test_pending_notice_never_breaks_the_tool_result(mailbox_home) -> None:
    """未绑定时提示必须为 0，而不是把工具调用搞崩。"""
    settings = make_settings(mailbox_home)
    server = build_server(
        settings,
        broker=Broker(settings),
        provider=UnavailableSessionProvider("没有会话身份（测试）"),
        host_type="dsh",
    )
    try:
        payload = call(server, "whoami")
        assert payload["ok"] is True
        assert payload["pending"]["count"] == 0
    finally:
        server.stop()


def test_mailbox_inbox_returns_workable_context(mailbox_home) -> None:
    """收件侧开工入口：一次调用拿到正文 + conversation_id + 现成的回复参数。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    c = make_server(mailbox_home, "session-c")
    try:
        b_id = call(b, "whoami")["account_id"]
        c_id = call(c, "whoami")["account_id"]
        ignored = call(a, "start_conversation", to_account_id=c_id, text="给 C 的")
        first = call(a, "start_conversation", to_account_id=b_id, text="给 B 的第一条")
        call(a, "send_message", conversation_id=first["conversation_id"], text="给 B 的第二条")

        payload = call(b, "mailbox_inbox")
        assert payload["ok"] is True
        assert payload["pending_total"] == 2
        assert payload["count"] == 2
        # 只返回发给自己的内容，不给别人的。
        assert ignored["conversation_id"] not in {
            row["conversation_id"] for row in payload["messages"]
        }
        contents = [row["content"] for row in payload["messages"]]
        assert contents == ["给 B 的第一条", "给 B 的第二条"]
        # 每条消息都带可直接使用的下一步参数。
        for row in payload["messages"]:
            assert row["conversation_id"] == first["conversation_id"]
            assert row["from_display_name"]
            assert row["reply_with"]["tool"] == "reply_message"
            assert row["reply_with"]["args"]["message_id"] == row["message_id"]
            assert row["mark_done_with"]["tool"] == "set_message_status"
        assert "无需回复" in payload["work_order"]
    finally:
        a.stop()
        b.stop()
        c.stop()


def test_mailbox_inbox_is_read_only(mailbox_home) -> None:
    """取件不得改变未读或投递状态：开工与否由收件方自己决定。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        call(a, "start_conversation", to_account_id=b_id, text="只读取件")
        before = call(b, "whoami")["pending"]["count"]

        call(b, "mailbox_inbox")

        assert call(b, "whoami")["pending"]["count"] == before, "取件不应清未读"
        assert call(b, "mailbox_inbox")["count"] == 1, "再取一次仍然看得到"
    finally:
        a.stop()
        b.stop()


def test_inbox_finds_new_mail_after_completed_long_history(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a", rate_limits=RateLimits(
        max_sends_per_minute=500, max_messages_per_conversation_per_minute=500,
    ))
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        first = call(a, "start_conversation", to_account_id=b_id, text="历史 0")
        old_ids = [first["message_id"]]
        for index in range(1, 105):
            sent = call(a, "send_message", conversation_id=first["conversation_id"], text=f"历史 {index}")
            assert sent["ok"], sent
            old_ids.append(sent["message_id"])
        for message_id in old_ids:
            receipt = call(b, "set_message_status", message_id=message_id, processing="completed")
            assert receipt["ok"], receipt
        call(b, "read_conversation", conversation_id=first["conversation_id"], limit=200)
        newest = call(a, "send_message", conversation_id=first["conversation_id"], text="新的来信")
        inbox = call(b, "mailbox_inbox")
        assert inbox["pending_total"] == 1
        assert [row["message_id"] for row in inbox["messages"]] == [newest["message_id"]]
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("processing", ["completed", "cancelled", "failed"])
def test_inbox_excludes_terminal_receipts_without_creating_reply(mailbox_home, processing) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        sent = call(a, "start_conversation", to_account_id=b_id, text="无需回复的通知")
        receipt = call(b, "set_message_status", message_id=sent["message_id"], processing=processing)
        assert receipt["ok"], receipt
        assert call(b, "mailbox_inbox")["messages"] == []
        assert call(b, "mailbox_inbox")["pending_total"] == 0
        page = call(a, "read_conversation", conversation_id=sent["conversation_id"], mark_seen=False)
        assert len(page["messages"]) == 1, "回执不能生成新的聊天消息"
    finally:
        a.stop()
        b.stop()


def test_reply_acknowledges_original_incoming_message(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        sent = call(a, "start_conversation", to_account_id=b_id, text="请回答", wait_for_reply=True)
        replied = call(b, "reply_message", message_id=sent["message_id"], text="答案", idempotency_key="reply-once")
        assert replied["ok"], replied
        assert call(b, "mailbox_inbox")["pending_total"] == 0
        with b.broker.unit_of_work().transaction() as uow:
            receipt = uow.processing.get(sent["message_id"], b_id)
        assert receipt is not None and receipt.state.value == "completed"
        retry = call(b, "reply_message", message_id=sent["message_id"], text="答案", idempotency_key="reply-once")
        assert retry["duplicate"] is True
        assert retry["message_id"] == replied["message_id"]
        inbox = call(a, "mailbox_inbox")
        assert inbox["count"] == 1
        assert inbox["messages"][0]["reply_to"] == sent["message_id"]
        assert inbox["messages"][0]["conversation_id"] == sent["conversation_id"]
    finally:
        a.stop()
        b.stop()


def test_inbox_exposes_reply_expectation_without_requiring_notification_reply(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        notice = call(a, "start_conversation", to_account_id=b_id, text="文件已更新，无需回复")
        request = call(a, "send_message", conversation_id=notice["conversation_id"], text="请确认版本", wait_for_reply=True)
        inbox = call(b, "mailbox_inbox")
        rows = {row["message_id"]: row for row in inbox["messages"]}
        assert rows[notice["message_id"]]["expects_reply"] is False
        assert rows[request["message_id"]]["expects_reply"] is True
        assert "无需回复" in inbox["work_order"]
        with a.broker.unit_of_work().transaction() as uow:
            persisted = uow.messages.get(request["message_id"])
        assert persisted.metadata["expects_reply"] == "true"
    finally:
        a.stop()
        b.stop()


def test_inbox_cursor_returns_every_pending_message_once(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        first = call(a, "start_conversation", to_account_id=b_id, text="第 0 条")
        expected = [first["message_id"]]
        for index in range(1, 5):
            sent = call(a, "send_message", conversation_id=first["conversation_id"], text=f"第 {index} 条")
            expected.append(sent["message_id"])
        received = []
        cursor = None
        for _ in range(4):
            inbox = call(b, "mailbox_inbox", limit=2, cursor=cursor)
            assert inbox["pending_total"] == 5
            received.extend(row["message_id"] for row in inbox["messages"])
            cursor = inbox["next_cursor"]
            if cursor is None:
                break
        assert cursor is None
        assert received == expected
    finally:
        a.stop()
        b.stop()


def test_rejected_reply_does_not_ack_original_and_sender_reply_cannot_ack_recipient(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        sent = call(a, "start_conversation", to_account_id=b_id, text="需要处理")
        rejected = call(b, "reply_message", message_id=sent["message_id"], text="")
        assert rejected["ok"] is False
        assert call(b, "mailbox_inbox")["pending_total"] == 1
        followup = call(a, "reply_message", message_id=sent["message_id"], text="补充说明")
        assert followup["ok"], followup
        assert call(b, "mailbox_inbox")["pending_total"] == 2
        with b.broker.unit_of_work().transaction() as uow:
            receipt = uow.processing.get(sent["message_id"], b_id)
        assert receipt is None or receipt.state.value == "pending"
    finally:
        a.stop()
        b.stop()


def test_receipt_retries_are_idempotent_and_reply_preserves_terminal_status(mailbox_home) -> None:
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        sent = call(a, "start_conversation", to_account_id=b_id, text="取消此请求")
        assert call(b, "set_message_status", message_id=sent["message_id"], processing="cancelled", result="已撤销")["ok"]
        with b.broker.unit_of_work().transaction() as uow:
            first = uow.processing.get(sent["message_id"], b_id)
        assert call(b, "set_message_status", message_id=sent["message_id"], processing="cancelled")["ok"]
        replied = call(b, "reply_message", message_id=sent["message_id"], text="收到撤销信息")
        assert replied["ok"], replied
        with b.broker.unit_of_work().transaction() as uow:
            last = uow.processing.get(sent["message_id"], b_id)
        assert last.state.value == "cancelled"
        assert last.result == "已撤销"
        assert last.attempt_count == first.attempt_count
    finally:
        a.stop()
        b.stop()


def test_reading_conversation_keeps_unfinished_mail_pending(mailbox_home) -> None:
    """阅读历史只清未读，尚未回执的消息仍可继续处理。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="读我")
        assert call(b, "whoami")["pending"]["count"] == 1

        page = call(b, "read_conversation", conversation_id=started["conversation_id"])
        assert page["unread_count"] == 0
        assert call(b, "whoami")["pending"]["count"] == 1
        inbox = call(b, "mailbox_inbox")
        assert inbox["pending_total"] == 1
        assert inbox["messages"][0]["message_id"] == started["message_id"]
    finally:
        a.stop()
        b.stop()


def test_read_only_mode_keeps_pending_count(mailbox_home) -> None:
    """mark_seen=False 是只读：看一眼不应该把未读清掉。"""
    a = make_server(mailbox_home, "session-a")
    b = make_server(mailbox_home, "session-b")
    try:
        b_id = call(b, "whoami")["account_id"]
        started = call(a, "start_conversation", to_account_id=b_id, text="只读看我")
        call(
            b,
            "read_conversation",
            conversation_id=started["conversation_id"],
            mark_seen=False,
        )
        assert call(b, "whoami")["pending"]["count"] == 1
    finally:
        a.stop()
        b.stop()


# ---------------------------------------------------------------------------
# 真·stdio 往返（真实 MCP 协议，子进程隔离）
# ---------------------------------------------------------------------------


def test_stdio_roundtrip_over_real_protocol(db_path_unused=None) -> None:  # noqa: ARG001
    """用真实 MCP stdio 传输跑通一次收发。

    子进程使用独立临时数据目录，绝不写真实用户目录；会话身份通过环境变量注入，
    这正是适配器应采用的接入方式。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        import tempfile

        # 两个会话共享同一个数据目录：所有端点连的是同一个 Broker，状态只有一份。
        root = tempfile.mkdtemp(prefix="mailbox-stdio-")
        repo = Path(__file__).resolve().parent.parent

        def params(session: str) -> StdioServerParameters:
            env = dict(os.environ)
            env.update(
                {
                    "MAILBOX_HOME": root,
                    "MAILBOX_SESSION_ID": session,
                    "MAILBOX_HOST_TYPE": "dsh",
                    "MAILBOX_HOST_INSTANCE_ID": "stdio-test",
                    "MAILBOX_CAPABILITY_LEVEL": "2",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(repo),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            return StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_agent_mailbox.cli", "serve"],
                env=env,
                cwd=str(repo),
            )

        async with stdio_client(params("session-a")) as (read, write):
            async with ClientSession(read, write) as session_a:
                await session_a.initialize()
                listing = await session_a.list_tools()
                names = {tool.name for tool in listing.tools}
                assert "whoami" in names and "send_message" in names
                for tool in listing.tools:
                    properties = set((tool.inputSchema or {}).get("properties", {}))
                    assert not (properties & FORBIDDEN_PARAMS), tool.name

                async with stdio_client(params("session-b")) as (read_b, write_b):
                    async with ClientSession(read_b, write_b) as session_b:
                        await session_b.initialize()
                        b_payload = json.loads(
                            (await session_b.call_tool("whoami", {})).content[0].text
                        )
                        assert b_payload["bound"] is True
                        assert b_payload["native_session_id"] == "session-b"
                        assert b_payload["presence"] == "connected"
                        assert b_payload["online"] is True
                        assert b_payload["can_wake"] is True

                        a_payload = json.loads(
                            (await session_a.call_tool("whoami", {})).content[0].text
                        )
                        assert a_payload["account_id"] != b_payload["account_id"]

                        sent = await session_a.call_tool(
                            "start_conversation",
                            {"to_account_id": b_payload["account_id"], "text": "stdio 往返"},
                        )
                        sent_payload = json.loads(sent.content[0].text)
                        assert sent_payload["ok"] is True, sent_payload
                        assert sent_payload["delivery_state"] == "queued"

                        # 同一 Broker：B 直接读得到这条消息（无需等事件通道）。
                        inbox = await session_b.call_tool(
                            "read_conversation",
                            {"conversation_id": sent_payload["conversation_id"]},
                        )
                        inbox_payload = json.loads(inbox.content[0].text)
                        assert inbox_payload["ok"] is True, inbox_payload
                        assert inbox_payload["messages"][0]["content"] == "stdio 往返"

                        replied = await session_b.call_tool(
                            "reply_message",
                            {"message_id": sent_payload["message_id"], "text": "stdio 应答"},
                        )
                        reply_payload = json.loads(replied.content[0].text)
                        assert reply_payload["ok"] is True
                        assert reply_payload["reply_to"] == sent_payload["message_id"]

    asyncio.run(exercise())


def test_stdio_full_conversation_loop() -> None:
    """端到端验收：两个会话经真实 MCP stdio 完成"发信 → 发现 → 取信 → 回复 → 回执"。

    这是"第一版能不能正常用"的最小闭环证明。整条链路都走真实协议与真实 SQLite，
    只有会话身份是测试注入的（这正是适配器接入的方式）。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        import tempfile

        root = tempfile.mkdtemp(prefix="mailbox-loop-")
        repo = Path(__file__).resolve().parent.parent

        def params(session: str) -> StdioServerParameters:
            env = dict(os.environ)
            env.update(
                {
                    "MAILBOX_HOME": root,
                    "MAILBOX_SESSION_ID": session,
                    "MAILBOX_HOST_TYPE": "dsh",
                    "MAILBOX_HOST_INSTANCE_ID": "loop-test",
                    "MAILBOX_CAPABILITY_LEVEL": "0",  # 诚实：会话不能唤醒，只能主动取信
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(repo),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            return StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_agent_mailbox.cli", "serve"],
                env=env,
                cwd=str(repo),
            )

        async def invoke(session, name: str, args: dict) -> dict:
            result = await session.call_tool(name, args)
            return json.loads(result.content[0].text)

        async with stdio_client(params("session-a")) as (read, write):
            async with ClientSession(read, write) as session_a:
                await session_a.initialize()
                async with stdio_client(params("session-b")) as (read_b, write_b):
                    async with ClientSession(read_b, write_b) as session_b:
                        await session_b.initialize()

                        # 会话身份与在线状态如实：Level 0 只显示 connected。
                        a_who = await invoke(session_a, "whoami", {})
                        b_who = await invoke(session_b, "whoami", {})
                        assert a_who["bound"] and b_who["bound"]
                        assert a_who["presence"] == "connected"
                        assert a_who["can_wake"] is False
                        assert a_who["account_id"] != b_who["account_id"]

                        # A 发信给 B。
                        sent = await invoke(
                            session_a,
                            "start_conversation",
                            {"to_account_id": b_who["account_id"], "text": "请复核这个改动"},
                        )
                        assert sent["ok"] is True
                        assert sent["delivery_state"] == "queued"

                        # B 在**任何一次工具调用**里就能发现未读（MCP 无主动推送）。
                        assert b_who["pending"]["count"] == 0
                        noticed = await invoke(session_b, "whoami", {})
                        assert noticed["pending"]["count"] == 1

                        # B 一次调用拿到正文与可直接开工的上下文。
                        inbox = await invoke(session_b, "mailbox_inbox", {})
                        assert inbox["pending_total"] == 1
                        assert inbox["messages"][0]["conversation_id"] == sent["conversation_id"]
                        assert inbox["messages"][0]["content"] == "请复核这个改动"
                        assert inbox["messages"][0]["reply_with"]["tool"] == "reply_message"

                        page = await invoke(
                            session_b,
                            "read_conversation",
                            {"conversation_id": sent["conversation_id"]},
                        )
                        assert page["messages"][0]["content"] == "请复核这个改动"
                        assert page["unread_count"] == 0

                        # 已读不等于回执，尚未答复仍待处理。
                        after = await invoke(session_b, "whoami", {})
                        assert after["pending"]["count"] == 1

                        # 回复自动确认原收件，可再补充完成摘要。
                        reply = await invoke(
                            session_b,
                            "reply_message",
                            {"message_id": sent["message_id"], "text": "复核通过"},
                        )
                        assert reply["ok"] is True and reply["reply_to"] == sent["message_id"]
                        ack = await invoke(
                            session_b,
                            "set_message_status",
                            {
                                "message_id": sent["message_id"],
                                "processing": "completed",
                                "result": "复核通过",
                            },
                        )
                        assert ack["record"]["state"] == "completed"

                        # A 侧对称地发现回复并读到结论。
                        a_noticed = await invoke(session_a, "whoami", {})
                        assert a_noticed["pending"]["count"] == 1
                        a_page = await invoke(
                            session_a,
                            "read_conversation",
                            {"conversation_id": sent["conversation_id"]},
                        )
                        assert [m["content"] for m in a_page["messages"]] == [
                            "请复核这个改动",
                            "复核通过",
                        ]
                        assert a_page["messages"][1]["is_reply"] is True

    asyncio.run(exercise())


def test_stdio_reports_missing_session_identity(tmp_path) -> None:
    """没有会话身份时，子进程必须明确报告不可用，而不是伪造账号。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        import tempfile

        repo = Path(__file__).resolve().parent.parent
        root = tempfile.mkdtemp(prefix="mailbox-stdio-nosession-")
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                ENV_SESSION_ID,
                "DSH_SESSION_ID",
                "CODEX_SESSION_ID",
                "CODEX_THREAD_ID",
                "CLAUDE_SESSION_ID",
            }
        }
        env.update(
            {
                "MAILBOX_HOME": root,
                "MAILBOX_HOST_TYPE": "dsh",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(repo),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_agent_mailbox.cli", "serve"],
            env=env,
            cwd=str(repo),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                payload = json.loads((await session.call_tool("whoami", {})).content[0].text)
                assert payload["bound"] is False
                assert payload["can_wake"] is False
                failure = json.loads((await session.call_tool("list_contacts", {})).content[0].text)
                assert failure["ok"] is False
                assert failure["error"] == "validation_error"

    asyncio.run(exercise())
