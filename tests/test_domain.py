"""领域层单元测试：身份、presence 租约、状态机、正文校验。

这些测试不碰数据库、不碰 MCP SDK，是整套系统里最快的反馈回路。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mcp_agent_mailbox.domain.accounts import HostIdentity, render_address
from mcp_agent_mailbox.domain.conversations import participant_key
from mcp_agent_mailbox.domain.errors import InvalidTransitionError, ValidationError
from mcp_agent_mailbox.domain.messages import (
    MAX_MESSAGE_CHARS,
    DeliveryState,
    ProcessingState,
    VisibilityState,
    check_delivery_transition,
    check_processing_transition,
    check_visibility_transition,
    content_hash,
    normalize_text,
)
from mcp_agent_mailbox.domain.presence import (
    HostCapabilityLevel,
    PresencePolicy,
    PresenceState,
    compute_presence,
)
from mcp_agent_mailbox.domain.timestamps import format_timestamp, parse_timestamp, utc_now

POLICY = PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10)


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


def test_host_identity_normalizes_host_type() -> None:
    identity = HostIdentity("  DSH ", "desktop-default", "session-1")
    assert identity.host_type == "dsh"
    assert identity.key == ("dsh", "desktop-default", "session-1")


@pytest.mark.parametrize(
    "host_type,instance,session",
    [
        ("", "i", "s"),
        ("dsh", "   ", "s"),
        ("dsh", "i", ""),
    ],
)
def test_host_identity_rejects_blank_parts(host_type: str, instance: str, session: str) -> None:
    with pytest.raises(ValueError):
        HostIdentity(host_type, instance, session)


def test_same_native_session_in_different_hosts_are_different_accounts() -> None:
    dsh = HostIdentity("dsh", "desktop-default", "session-1")
    codex = HostIdentity("codex", "desktop-default", "session-1")
    assert dsh.key != codex.key
    assert dsh != codex


def test_address_is_display_only() -> None:
    assert render_address("dsh", "测试", "desktop-default") == "dsh:测试@desktop-default"
    assert render_address("dsh", "   ", "i") == "dsh:unnamed@i"


# ---------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------


def test_presence_live_host_process_is_online() -> None:
    """在线严格参照进程：托管进程活着 -> 在线。"""
    assert (
        compute_presence(host_pid=4242, alive=True)
        is PresenceState.CONNECTED
    )


def test_presence_dead_host_process_is_offline() -> None:
    """进程退出 -> 离线（不管能力等级、也不管租约还剩多久）。"""
    assert (
        compute_presence(host_pid=4242, alive=False)
        is PresenceState.OFFLINE
    )


def test_presence_lease_does_not_matter() -> None:
    """租约/心跳不参与判定：没有进程信息就是离线，进程活着就是在线的唯一依据。"""
    assert compute_presence(host_pid=None) is PresenceState.OFFLINE
    assert compute_presence(host_pid=4242, alive=True) is PresenceState.CONNECTED


def test_presence_closed_connection_is_offline_even_with_live_process() -> None:
    assert (
        compute_presence(host_pid=4242, closed=True, alive=True)
        is PresenceState.OFFLINE
    )


def test_presence_state_predicates() -> None:
    assert PresenceState.CONNECTED.is_online
    assert PresenceState.CONNECTED.is_reachable
    assert PresenceState.CONNECTED.is_healthy
    assert PresenceState.CONNECTED.can_receive
    assert not PresenceState.OFFLINE.is_online
    assert not PresenceState.OFFLINE.is_reachable
    assert not PresenceState.OFFLINE.is_healthy
    assert not PresenceState.OFFLINE.can_receive


def test_process_alive_and_dead_are_distinguished() -> None:
    """回归：进程退出码恰好等于 259（Windows STILL_ACTIVE 的值）时**必须**判为已死。

    旧实现用 ``GetExitCodeProcess(...) == 259`` 判存活，而 259 同时是合法退出码，
    于是"已死的宿主"会被永久判成在线（账号一直显示 connected、维护循环也不回收）。
    """
    import os
    import subprocess
    import sys

    from mcp_agent_mailbox.domain.process import is_process_alive, process_started_at

    assert is_process_alive(os.getpid()) is True

    exited = subprocess.Popen([sys.executable, "-c", "import os; os._exit(259)"])
    exited.wait()
    assert is_process_alive(exited.pid) is False

    normal = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    normal.wait()
    assert is_process_alive(normal.pid) is False

    assert is_process_alive(2_147_483_646) is False
    assert process_started_at(os.getpid()) is not None, "要能拿到进程创建时间"
    assert process_started_at(None) is None


def test_policy_rejects_magic_number_mistakes() -> None:
    # 租约不参与在线判定，但参数本身仍必须自洽（负数/零是明显写错）。
    with pytest.raises(ValueError):
        PresencePolicy(lease_seconds=0)
    with pytest.raises(ValueError):
        PresencePolicy(heartbeat_interval_seconds=0)
    with pytest.raises(ValueError):
        PresencePolicy(grace_seconds=-1)


def test_capability_level_helpers() -> None:
    assert not HostCapabilityLevel.TOOLS_ONLY.can_receive_realtime
    assert HostCapabilityLevel.NOTIFY.can_receive_realtime
    assert not HostCapabilityLevel.NOTIFY.can_wake_session
    assert HostCapabilityLevel.WAKE.can_wake_session
    assert HostCapabilityLevel.WAKE.slug == "wake"


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


def test_delivery_happy_path() -> None:
    check_delivery_transition(DeliveryState.QUEUED, DeliveryState.DISPATCHED)
    check_delivery_transition(DeliveryState.DISPATCHED, DeliveryState.DELIVERED)


def test_delivery_delivered_is_terminal() -> None:
    with pytest.raises(InvalidTransitionError):
        check_delivery_transition(DeliveryState.DELIVERED, DeliveryState.QUEUED)
    with pytest.raises(InvalidTransitionError):
        check_delivery_transition(DeliveryState.DEAD_LETTER, DeliveryState.QUEUED)


def test_delivery_failed_can_be_requeued_or_dead_lettered() -> None:
    check_delivery_transition(DeliveryState.DISPATCHED, DeliveryState.FAILED)
    check_delivery_transition(DeliveryState.FAILED, DeliveryState.QUEUED)
    check_delivery_transition(DeliveryState.FAILED, DeliveryState.DEAD_LETTER)


def test_delivery_failed_can_succeed_on_retry() -> None:
    """先失败后成功的投递必须能被确认；否则重试永远无法真正完成。"""
    check_delivery_transition(DeliveryState.FAILED, DeliveryState.DELIVERED)


def test_delivery_cannot_skip_dispatch() -> None:
    with pytest.raises(InvalidTransitionError):
        check_delivery_transition(DeliveryState.QUEUED, DeliveryState.DELIVERED)


def test_dispatched_can_go_straight_to_dead_letter() -> None:
    """适配器报告"不可重试"时不必先绕一圈 failed。"""
    check_delivery_transition(DeliveryState.DISPATCHED, DeliveryState.DEAD_LETTER)


def test_processing_pending_can_complete_without_running() -> None:
    """适配器可以直接回传终态；强行要求中间态只会逼调用方伪造状态。"""
    check_processing_transition(ProcessingState.PENDING, ProcessingState.COMPLETED)
    check_processing_transition(ProcessingState.PENDING, ProcessingState.CANCELLED)
    check_processing_transition(ProcessingState.PENDING, ProcessingState.BLOCKED)


def test_processing_completed_cannot_go_back_to_running() -> None:
    check_processing_transition(ProcessingState.PENDING, ProcessingState.RUNNING)
    check_processing_transition(ProcessingState.RUNNING, ProcessingState.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        check_processing_transition(ProcessingState.COMPLETED, ProcessingState.RUNNING)


def test_processing_blocked_can_resume() -> None:
    check_processing_transition(ProcessingState.RUNNING, ProcessingState.BLOCKED)
    check_processing_transition(ProcessingState.BLOCKED, ProcessingState.RUNNING)
    check_processing_transition(ProcessingState.BLOCKED, ProcessingState.CANCELLED)


def test_processing_same_state_is_noop() -> None:
    check_processing_transition(ProcessingState.RUNNING, ProcessingState.RUNNING)


def test_visibility_transitions() -> None:
    check_visibility_transition(VisibilityState.UNREAD, VisibilityState.SEEN)
    check_visibility_transition(VisibilityState.SEEN, VisibilityState.UNREAD)


@pytest.mark.parametrize(
    "state,terminal",
    [
        (DeliveryState.QUEUED, False),
        (DeliveryState.DISPATCHED, False),
        (DeliveryState.DELIVERED, True),
        (DeliveryState.FAILED, False),
        (DeliveryState.DEAD_LETTER, True),
    ],
)
def test_delivery_terminal_flags(state: DeliveryState, terminal: bool) -> None:
    assert state.is_terminal is terminal


def test_delivery_needs_dispatch() -> None:
    assert DeliveryState.QUEUED.needs_dispatch
    assert DeliveryState.FAILED.needs_dispatch
    assert not DeliveryState.DISPATCHED.needs_dispatch
    assert not DeliveryState.DELIVERED.needs_dispatch


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------


def test_normalize_text_trims_and_unifies_newlines() -> None:
    assert normalize_text("  hello\r\nworld\r  ") == "hello\nworld"


@pytest.mark.parametrize("value", ["", "   ", "\n\n", None, 123])
def test_normalize_text_rejects_empty_or_non_string(value) -> None:
    with pytest.raises(ValidationError):
        normalize_text(value)


def test_oversize_text_is_rejected_not_truncated() -> None:
    payload = "x" * (MAX_MESSAGE_CHARS + 1)
    with pytest.raises(ValidationError) as excinfo:
        normalize_text(payload)
    assert str(MAX_MESSAGE_CHARS) in str(excinfo.value)
    assert "截断" in str(excinfo.value)


def test_max_length_text_is_accepted() -> None:
    payload = "x" * MAX_MESSAGE_CHARS
    assert normalize_text(payload) == payload


def test_content_hash_is_platform_independent() -> None:
    assert content_hash("a\r\nb") == content_hash("a\nb")
    assert content_hash("a") != content_hash("b")


# ---------------------------------------------------------------------------
# 对话参与者键
# ---------------------------------------------------------------------------


def test_participant_key_is_order_independent() -> None:
    assert participant_key("acc_a", "acc_b") == participant_key("acc_b", "acc_a")


def test_participant_key_rejects_self_and_blank() -> None:
    with pytest.raises(ValidationError):
        participant_key("acc_a", "acc_a")
    with pytest.raises(ValidationError):
        participant_key("", "acc_b")


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def test_timestamp_roundtrip_is_utc() -> None:
    moment = utc_now()
    text = format_timestamp(moment)
    assert text.endswith("+00:00")
    assert parse_timestamp(text) == moment.astimezone(moment.tzinfo)


def test_naive_timestamp_is_read_as_utc() -> None:
    parsed = parse_timestamp("2026-10-04T15:00:00")
    assert parsed.utcoffset() == timedelta(0)
