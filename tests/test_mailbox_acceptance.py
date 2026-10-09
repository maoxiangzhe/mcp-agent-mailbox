"""验收：隔离的真实 stdio 会话交流与连接身份不可切换。

不使用真实客户端配置、凭据或邮箱库；宿主唤醒另由现场验收证明。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mcp_agent_mailbox.config import load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.errors import ValidationError
from mcp_agent_mailbox.mcp.connection_context import (
    ExplicitRegistrationProvider,
    MailboxConnection,
    SessionDescriptor,
    UnavailableSessionProvider,
)


def _register(connection, broker, session="验收-A", **changes):
    arguments = dict(host_type="codex", host_instance_id="acceptance", native_session_id=session,
                     display_name="验收账号", capability_level=0)
    arguments.update(changes)
    return connection.submit_adapter_registration(broker.accounts, **arguments)


@pytest.mark.parametrize("changed", [
    {"native_session_id": "验收-B"},
    {"host_type": "dsh"},
    {"host_instance_id": "另一个实例"},
])
def test_registered_connection_cannot_switch_identity_even_after_recovery(tmp_path, changed):
    broker = Broker(load_settings(data_dir=tmp_path / "mailbox"))
    connection = MailboxConnection(provider=UnavailableSessionProvider("测试"),
                                   allow_model_registration=True)
    try:
        first = _register(connection, broker)
        original = (first.account_id, first.connection_id, first.generation)
        with pytest.raises(ValidationError):
            _register(connection, broker, **changed)
        assert (connection.account_id, connection.connection_id, connection.generation) == original
        assert connection.describe_if_available().native_session_id == "验收-A"
        connection.fail("模拟连接回收")
        with pytest.raises(ValidationError):
            _register(connection, broker, **changed)
        connection.bind(broker.accounts)
        assert connection.account_id == original[0]
        assert connection.descriptor.native_session_id == "验收-A"
        with broker.unit_of_work().transaction() as uow:
            assert len(uow.accounts.list_accounts()) == 1
    finally:
        broker.stop()


def test_repeated_same_registration_preserves_connection_and_descriptor(tmp_path):
    broker = Broker(load_settings(data_dir=tmp_path / "mailbox"))
    connection = MailboxConnection(provider=UnavailableSessionProvider("测试"),
                                   allow_model_registration=True)
    try:
        _register(connection, broker)
        original = (connection.account_id, connection.connection_id, connection.generation)
        descriptor = connection.descriptor
        _register(connection, broker, display_name="不能通过重注册悄悄改名")
        assert (connection.account_id, connection.connection_id, connection.generation) == original
        assert connection.descriptor == descriptor
        assert connection.describe_if_available() == descriptor
        with broker.unit_of_work().transaction() as uow:
            assert len(uow.connections.list_for_account(connection.account_id, include_closed=True)) == 1
    finally:
        broker.stop()


def test_mutable_provider_cannot_rebind_connection_to_another_session(tmp_path):
    broker = Broker(load_settings(data_dir=tmp_path / "mailbox"))
    provider = ExplicitRegistrationProvider()
    provider.submit(SessionDescriptor("codex", "acceptance", "A", "账号 A"))
    connection = MailboxConnection(provider=provider)
    try:
        connection.bind(broker.accounts)
        original_id = connection.account_id
        provider.submit(SessionDescriptor("codex", "acceptance", "B", "账号 B"))
        with pytest.raises(ValidationError):
            connection.bind(broker.accounts)
        assert connection.account_id == original_id
        connection.fail("模拟连接回收")
        with pytest.raises(ValidationError):
            connection.bind(broker.accounts)
        with broker.unit_of_work().transaction() as uow:
            assert len(uow.accounts.list_accounts()) == 1
    finally:
        broker.stop()


@pytest.mark.parametrize("sessions", [("并发-A", "并发-B"), ("并发-A", "并发-A")])
def test_concurrent_registration_is_atomic_and_same_identity_is_idempotent(tmp_path, monkeypatch, sessions):
    """两个调用真实重叠：不同身份只有一个绑定，相同身份只生成一条连接。"""
    broker = Broker(load_settings(data_dir=tmp_path / "mailbox"))
    connection = MailboxConnection(provider=UnavailableSessionProvider("测试"),
                                   allow_model_registration=True)
    start = threading.Barrier(2)
    real_bind = broker.accounts.bind_session

    def slow_bind(*args, **kwargs):
        # 模拟注册事务耗时，令另一个请求到达；不依赖操作系统偶然调度抢中窗口。
        time.sleep(0.05)
        return real_bind(*args, **kwargs)

    monkeypatch.setattr(broker.accounts, "bind_session", slow_bind)

    def register(session):
        start.wait(timeout=5)
        try:
            result = _register(connection, broker, session=session)
            return ("ok", result.account_id, result.connection_id, result.generation)
        except ValidationError:
            return ("rejected",)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(register, session) for session in sessions]
            results = [future.result(timeout=10) for future in pending]
        successes = [result for result in results if result[0] == "ok"]
        assert len(successes) == (2 if sessions[0] == sessions[1] else 1)
        if sessions[0] == sessions[1]:
            assert successes[0] == successes[1]
        else:
            assert [result[0] for result in results].count("rejected") == 1
        assert connection.describe_if_available() == connection.descriptor
        with broker.unit_of_work().transaction() as uow:
            accounts = uow.accounts.list_accounts()
            assert len(accounts) == 1
            account = accounts[0]
            assert account.account_id == connection.account_id
            assert account.identity == connection.descriptor.identity
            connections = uow.connections.list_for_account(account.account_id, include_closed=True)
            assert len(connections) == 1
            assert connections[0].connection_id == connection.connection_id
            assert connection.generation == 1
    finally:
        broker.stop()


def test_real_stdio_independent_sessions_exchange_paginate_ack_and_reconnect(tmp_path):
    """协议验收覆盖用户实际流程；不把 SDK 连接等同于桌面已加载工具。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = Path(__file__).resolve().parents[1]
    home = tmp_path / "mailbox"

    @asynccontextmanager
    async def endpoint(session_id):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MAILBOX_", "BOARD_MCP_"))
               and key not in {"CODEX_SESSION_ID", "CODEX_THREAD_ID", "DSH_SESSION_ID",
                               "CLAUDE_SESSION_ID", "OPENCODE_SESSION_ID", "DSH_HOME"}}
        env.update(MAILBOX_HOME=str(home), MAILBOX_SESSION_ID=session_id,
                   MAILBOX_HOST_TYPE="codex", MAILBOX_HOST_INSTANCE_ID="acceptance-stdio",
                   MAILBOX_CAPABILITY_LEVEL="0", MAILBOX_DSH_HOME=str(tmp_path / "no-dsh"),
                   MAILBOX_CODEX_CLI=str(tmp_path / "no-codex-cli.exe"),
                   PYTHONIOENCODING="utf-8", PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE="1")
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "mcp_agent_mailbox.cli", "serve", "--allow-adapter-registration"],
                                      env=env, cwd=str(repo))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                yield client

    async def invoke(client, name, **arguments):
        result = await client.call_tool(name, arguments)
        payload = json.loads(result.content[0].text)
        assert payload.get("ok") is True, (name, payload)
        return payload

    async def exercise():
        async with endpoint("acceptance-A") as a:
            async with endpoint("acceptance-B") as b:
                a_who, b_who = await invoke(a, "whoami"), await invoke(b, "whoami")
                assert a_who["account_id"] != b_who["account_id"]
                assert a_who["native_session_id"] == "acceptance-A"
                assert b_who["native_session_id"] == "acceptance-B"
                assert a_who["can_wake"] is False
                original_b = (b_who["account_id"], b_who["connection_id"], b_who["generation"])
                repeated = await invoke(b, "connect_mailbox", session_id="acceptance-B", account_name="验收-B")
                assert repeated["account_id"] == original_b[0]
                assert (await invoke(b, "whoami"))["connection_id"] == original_b[1]
                rejected = json.loads((await b.call_tool("connect_mailbox", {
                    "session_id": "acceptance-A", "account_name": "错误会话",
                })).content[0].text)
                assert rejected["ok"] is False and rejected["error"] == "validation_error"
                assert (await invoke(b, "whoami"))["account_id"] == original_b[0]

                first = await invoke(a, "start_conversation", to_account_id=original_b[0],
                                     text="请答复：中文通讯验收", wait_for_reply=True)
                messages = [first]
                for index in range(1, 5):
                    messages.append(await invoke(a, "send_message", conversation_id=first["conversation_id"],
                                                 text=f"通知 {index}：只回执，无需回复", wait_for_reply=False))
                seen, cursor = [], None
                while True:
                    page = await invoke(b, "mailbox_inbox", limit=2, cursor=cursor)
                    assert page["pending_total"] == 5
                    seen.extend(page["messages"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert {item["message_id"] for item in seen} == {item["message_id"] for item in messages}
                assert len(seen) == 5
                assert {item["from_account_id"] for item in seen} == {a_who["account_id"]}
                assert sum(item["expects_reply"] for item in seen) == 1
                await invoke(b, "read_conversation", conversation_id=first["conversation_id"])
                assert (await invoke(b, "mailbox_inbox"))["pending_total"] == 5
                reply = await invoke(b, "reply_message", message_id=first["message_id"],
                                     text="中文验收通过：消息及回复已互通")
                assert reply["reply_to"] == first["message_id"]
                for notification in messages[1:]:
                    await invoke(b, "set_message_status", message_id=notification["message_id"],
                                 processing="completed")
                assert (await invoke(b, "mailbox_inbox"))["pending_total"] == 0
                a_inbox = await invoke(a, "mailbox_inbox")
                assert [item["content"] for item in a_inbox["messages"]] == ["中文验收通过：消息及回复已互通"]
                await invoke(a, "set_message_status", message_id=reply["message_id"], processing="completed")
                assert (await invoke(a, "mailbox_inbox"))["pending_total"] == 0
                assert len((await invoke(a, "read_conversation", conversation_id=first["conversation_id"]))["messages"]) == 6

            # B 的 MCP 子进程已退出，账号应保留，A 仍能投递积压。
            contacts = await invoke(a, "list_contacts")
            target = next(item for item in contacts["contacts"] if item["account_id"] == original_b[0])
            assert target["presence"] == "offline"
            offline = await invoke(a, "send_message", conversation_id=first["conversation_id"], text="离线积压：恢复后取信")
            async with endpoint("acceptance-B") as restored_b:
                restored = await invoke(restored_b, "whoami")
                assert restored["account_id"] == original_b[0]
                assert restored["generation"] > original_b[2]
                assert restored["connection_id"] != original_b[1]
                backlog = await invoke(restored_b, "mailbox_inbox")
                assert [item["message_id"] for item in backlog["messages"]] == [offline["message_id"]]
                await invoke(restored_b, "set_message_status", message_id=offline["message_id"], processing="completed")
                assert (await invoke(restored_b, "mailbox_inbox"))["pending_total"] == 0
                b_first = await invoke(restored_b, "start_conversation", to_account_id=a_who["account_id"], text="反向发起：请给结论")
                a_reply = await invoke(a, "reply_message", message_id=b_first["message_id"], text="反向交流通过")
                await invoke(restored_b, "set_message_status", message_id=a_reply["message_id"], processing="completed")
                assert (await invoke(a, "mailbox_inbox"))["pending_total"] == 0
                assert (await invoke(restored_b, "mailbox_inbox"))["pending_total"] == 0

    asyncio.run(asyncio.wait_for(exercise(), timeout=120))
