"""Shell 取信必须遵守 MCP 收件规则，且不能重新注册宿主身份。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_agent_mailbox.config import load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import HostIdentity
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel


def test_shell_inbox_keeps_read_pending_excludes_completed_and_preserves_connection(tmp_path):
    settings = load_settings(data_dir=tmp_path / "mailbox")
    broker = Broker(settings, auto_waker=False)
    try:
        sender, _ = broker.accounts.register_account(
            HostIdentity("dsh", "default", "sender-test"), display_name="sender",
            capability_level=HostCapabilityLevel.TOOLS_ONLY,
        )
        target, _ = broker.accounts.register_account(
            HostIdentity("codex", "default", "codex-fallback-test"), display_name="target",
            capability_level=HostCapabilityLevel.TOOLS_ONLY,
        )
        sender_connection = broker.accounts.open_connection(sender.account_id, host_pid=os.getpid())
        connection = broker.accounts.open_connection(target.account_id, host_pid=os.getpid())
        first = broker.conversations.start_conversation(
            connection_id=sender_connection.connection_id, to_account_id=target.account_id,
            text="已阅读尚未回执",
        )
        second = broker.conversations.send_message(
            connection_id=sender_connection.connection_id, conversation_id=first.conversation_id,
            text="已完成",
        )
        broker.conversations.read_conversation(
            connection_id=connection.connection_id, conversation_id=first.conversation_id,
        )
        broker.conversations.set_message_status(
            connection_id=connection.connection_id, message_id=second.message.message_id,
            processing="completed",
        )
        env = os.environ.copy()
        env.update(MAILBOX_HOME=str(settings.data_dir), CODEX_SESSION_ID="codex-fallback-test", PYTHONIOENCODING="utf-8",
                   USERPROFILE=str(tmp_path), CODEX_HOME=str(tmp_path / "codex-config"), MAILBOX_HOST_INSTANCE_ID="default")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "tools" / "codex_mailbox.py"), "inbox"],
            env=env, text=True, encoding="utf-8", capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        inbox = json.loads(result.stdout)
        assert inbox["pending_total"] == 1
        assert [row["message_id"] for row in inbox["messages"]] == [first.message.message_id]
        with broker.unit_of_work().transaction() as uow:
            current = uow.connections.current_for_account(target.account_id)
            accounts = uow.accounts.list_accounts()
        assert len(accounts) == 2
        assert current.connection_id == connection.connection_id
        assert current.generation == connection.generation
    finally:
        broker.stop()


@pytest.fixture
def fallback_environment(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MAILBOX_", "BOARD_MCP_"))
           and key not in {"CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_HOME"}}
    env.update(CODEX_SESSION_ID="fallback-config-test", CODEX_HOME=str(tmp_path / "codex-config"),
               USERPROFILE=str(tmp_path / "userhome"), PYTHONIOENCODING="utf-8")
    return env


@pytest.fixture
def seeded_mailboxes():
    brokers = []

    def seed(path, instance, text, *, online=True):
        broker = Broker(load_settings(data_dir=path), auto_waker=False)
        brokers.append(broker)
        sender, _ = broker.accounts.register_account(
            HostIdentity("dsh", instance, "source"), display_name="不应改变的发送者",
            capability_level=HostCapabilityLevel.TOOLS_ONLY)
        target, _ = broker.accounts.register_account(
            HostIdentity("codex", instance, "fallback-config-test"), display_name="不应改变的收件人",
            capability_level=HostCapabilityLevel.TOOLS_ONLY)
        source = broker.accounts.open_connection(sender.account_id, host_pid=os.getpid())
        if online:
            broker.accounts.open_connection(target.account_id, host_pid=os.getpid())
        broker.conversations.start_conversation(
            connection_id=source.connection_id, to_account_id=target.account_id, text=text)
        return broker

    yield seed
    for broker in brokers:
        broker.stop()


def _run_fallback(env):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "tools" / "codex_mailbox.py"), "inbox"],
        env=env, text=True, encoding="utf-8", capture_output=True, timeout=30)


def _identity_snapshot(broker):
    with broker.unit_of_work().transaction() as uow:
        accounts = uow.accounts.list_accounts()
        return {
            account.account_id: (account.to_dict(), [item.to_dict() for item in
                uow.connections.list_for_account(account.account_id, include_closed=True)])
            for account in accounts
        }


def _write_mailbox_config(env, path, instance):
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '[mcp_servers.mcp-agent-mailbox]\n'
        'command = "configured-python"\n'
        'args = ["-m", "mcp_agent_mailbox.cli", "serve"]\n'
        '[mcp_servers.mcp-agent-mailbox.env]\n'
        f"MAILBOX_HOME = '{path}'\n"
        f"MAILBOX_HOST_INSTANCE_ID = '{instance}'\n"
        "MAILBOX_SESSION_ID = 'must-not-override-shell-session'\n", encoding="utf-8")
    return config


def test_fallback_reads_configured_mailbox_and_instance_without_mutating_identity(tmp_path, fallback_environment, seeded_mailboxes):
    old = seeded_mailboxes(tmp_path / "userhome" / ".board-mcp", "default", "旧库消息，不应取到")
    new = seeded_mailboxes(tmp_path / "new-mailbox", "custom-instance", "新库消息，应该取到")
    _write_mailbox_config(fallback_environment, new.settings.data_dir, "custom-instance")
    snapshots = [_identity_snapshot(broker) for broker in (old, new)]
    result = _run_fallback(fallback_environment)
    assert result.returncode == 0, result.stderr
    assert [item["content"] for item in json.loads(result.stdout)["messages"]] == ["新库消息，应该取到"]
    assert [_identity_snapshot(broker) for broker in (old, new)] == snapshots


@pytest.mark.parametrize("home_variable", ["MAILBOX_HOME", "BOARD_MCP_ROOT"])
def test_fallback_explicit_directory_and_instance_take_precedence_over_config(tmp_path, fallback_environment, seeded_mailboxes, home_variable):
    configured = seeded_mailboxes(tmp_path / "configured-mailbox", "configured-instance", "配置库消息")
    explicit = seeded_mailboxes(tmp_path / "explicit-mailbox", "explicit-instance", "显式环境选择的消息")
    _write_mailbox_config(fallback_environment, configured.settings.data_dir, "configured-instance")
    fallback_environment.update({home_variable: str(explicit.settings.data_dir), "MAILBOX_HOST_INSTANCE_ID": "explicit-instance"})
    snapshots = [_identity_snapshot(broker) for broker in (configured, explicit)]
    result = _run_fallback(fallback_environment)
    assert result.returncode == 0, result.stderr
    assert [item["content"] for item in json.loads(result.stdout)["messages"]] == ["显式环境选择的消息"]
    assert [_identity_snapshot(broker) for broker in (configured, explicit)] == snapshots


def test_fallback_explicit_instance_overrides_configured_instance(tmp_path, fallback_environment, seeded_mailboxes):
    path = tmp_path / "shared-mailbox"
    configured = seeded_mailboxes(path, "configured-instance", "配置实例消息")
    seeded_mailboxes(path, "explicit-instance", "显式实例消息")
    _write_mailbox_config(fallback_environment, path, "configured-instance")
    fallback_environment["MAILBOX_HOST_INSTANCE_ID"] = "explicit-instance"
    before = _identity_snapshot(configured)
    result = _run_fallback(fallback_environment)
    assert result.returncode == 0, result.stderr
    assert [item["content"] for item in json.loads(result.stdout)["messages"]] == ["显式实例消息"]
    assert _identity_snapshot(configured) == before


@pytest.mark.parametrize("server_name,args", [
    ("unrelated", '["-m", "mcp_agent_mailbox.cli", "serve"]'),
    ("mcp-agent-mailbox", '["unrelated-server.py"]'),
])
def test_fallback_does_not_use_unrelated_server_environment(tmp_path, fallback_environment, seeded_mailboxes, server_name, args):
    mailbox = seeded_mailboxes(tmp_path / "unrelated-mailbox", "unrelated-instance", "不能借用其他服务器环境")
    config = Path(fallback_environment["CODEX_HOME"]) / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[mcp_servers.{server_name}]\nargs = {args}\n'
        f'[mcp_servers.{server_name}.env]\n'
        f"MAILBOX_HOME = '{mailbox.settings.data_dir}'\n"
        "MAILBOX_HOST_INSTANCE_ID = 'unrelated-instance'\n", encoding="utf-8")
    before = _identity_snapshot(mailbox)
    result = _run_fallback(fallback_environment)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert _identity_snapshot(mailbox) == before


@pytest.mark.parametrize("config_text", [
    '[mcp_servers.mcp-agent-mailbox\nargs = [',
    '[mcp_servers.unrelated]\nargs = ["-m", "mcp_agent_mailbox.cli", "serve"]\n',
    '[mcp_servers.mcp-agent-mailbox]\nargs = ["some-other-server.py"]\n',
    '[mcp_servers.mcp-agent-mailbox]\nenabled = false\nargs = ["-m", "mcp_agent_mailbox.cli", "serve"]\n',
])
def test_fallback_rejects_invalid_unrelated_or_disabled_config_without_opening_default_db(tmp_path, fallback_environment, config_text):
    config = Path(fallback_environment["CODEX_HOME"]) / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(config_text, encoding="utf-8")
    result = _run_fallback(fallback_environment)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert result.stderr.strip()
    assert not (tmp_path / "userhome" / ".board-mcp" / "mailbox.sqlite3").exists()


def test_fallback_does_not_register_offline_account(tmp_path, fallback_environment, seeded_mailboxes):
    mailbox = seeded_mailboxes(tmp_path / "mailbox", "custom-instance", "离线收件", online=False)
    _write_mailbox_config(fallback_environment, mailbox.settings.data_dir, "custom-instance")
    before = _identity_snapshot(mailbox)
    result = _run_fallback(fallback_environment)
    assert result.returncode != 0
    assert "no online mailbox connection" in result.stderr
    assert _identity_snapshot(mailbox) == before
