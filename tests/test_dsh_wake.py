"""DSH 唤醒通道测试：cookie 格式、密钥读取、注入决策。

这些测试**不联网**：HTTP 由注入的 ``post`` 替身完成。要点是"诚实"——
拿不到密钥就必须是"不可用"；注入成功才算 delivered；失败要分清"通道不可用"
（保持 queued，等目标自己取信）与"这次注入失败"（failed，可重试）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_agent_mailbox.adapters.dsh_wake import (
    DshWebWaker,
    WakeOutcome,
    build_cookie,
    cookie_name_for,
    read_browser_session_secret,
)
from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, Settings, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import HostIdentity
from mcp_agent_mailbox.domain.messages import DeliveryState
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresencePolicy
from mcp_agent_mailbox.domain.timestamps import utc_now
from mcp_agent_mailbox.ports.clock import FrozenClock

SECRET_BYTES = bytes(range(32))
SECRET_B64 = base64.urlsafe_b64encode(SECRET_BYTES).decode().rstrip("=")
DEAD_PID = 2_147_483_646  # 不可能存在的 PID = 托管进程已退出


def _credentials_text(secret: str = SECRET_B64) -> str:
    return (
        "version: 1\n"
        "refs:\n"
        "  A147_API_KEY: sk-test\n"
        "records:\n"
        "  client-connection/browser-session:\n"
        "    kind: grant\n"
        "    payload:\n"
        "      version: 1\n"
        f"      secret: {secret}\n"
        "  other/record:\n"
        "    kind: api-key\n"
        "    payload:\n"
        "      value: nope\n"
    )


# ---------------------------------------------------------------------------
# 密钥读取
# ---------------------------------------------------------------------------


def test_reads_secret_from_credentials_file(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.yaml"
    path.write_text(_credentials_text(), encoding="utf-8")
    assert read_browser_session_secret(path) == SECRET_BYTES


def test_missing_file_is_not_a_secret(tmp_path: Path) -> None:
    assert read_browser_session_secret(tmp_path / "nope.yaml") is None


def test_secret_must_decode_to_32_bytes(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.yaml"
    path.write_text(_credentials_text(secret="AAAA"), encoding="utf-8")
    assert read_browser_session_secret(path) is None, "长度不对就不能当密钥用"


def test_wrong_record_is_not_used(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.yaml"
    path.write_text(
        "version: 1\nrecords:\n  other/record:\n    kind: grant\n    payload:\n"
        f"      secret: {SECRET_B64}\n",
        encoding="utf-8",
    )
    assert read_browser_session_secret(path) is None, "只认 client-connection/browser-session"


# ---------------------------------------------------------------------------
# cookie 格式：与 DSH 自身实现对齐
# ---------------------------------------------------------------------------


def test_cookie_name_is_sha256_of_authority() -> None:
    authority = "127.0.0.1:19387"
    expected = "dsh-auth-" + base64.urlsafe_b64encode(
        hashlib.sha256(authority.encode()).digest()
    ).decode().rstrip("=")
    assert cookie_name_for(authority) == expected


def test_cookie_value_is_v1_body_signature() -> None:
    authority = "127.0.0.1:19387"
    moment = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    value = build_cookie(SECRET_BYTES, authority, ttl_seconds=600, now=moment)
    version, body, signature = value.split(".")
    assert version == "v1"

    payload = json.loads(base64.urlsafe_b64decode(body + "==").decode())
    assert payload == {
        "version": 1,
        "authority": authority,
        "issuedAt": int(moment.timestamp() * 1000),
        "expiresAt": int(moment.timestamp() * 1000) + 600_000,
    }
    expected_signature = hmac.new(SECRET_BYTES, body.encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "==") == expected_signature, (
        "签名必须是对 base64url 之后的 body 做 HMAC-SHA256"
    )


# ---------------------------------------------------------------------------
# 唤醒客户端
# ---------------------------------------------------------------------------


class _Recorder:
    """替身 HTTP：记录请求，按脚本返回。"""

    def __init__(self, status: int = 200, body: str | None = None) -> None:
        self.status = status
        self.body = body or json.dumps(
            {
                "type": "server-response",
                "rpcId": "x",
                "result": {"ok": True, "value": {"accepted": True}},
            }
        )
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    def __call__(self, url, headers, body, timeout):  # noqa: ANN001
        self.calls.append((url, headers, body))
        return self.status, self.body


def _waker(post: _Recorder) -> DshWebWaker:
    return DshWebWaker("http://127.0.0.1:19387", SECRET_BYTES, post=post)


def test_wake_posts_prompt_with_signed_cookie() -> None:
    recorder = _Recorder()
    outcome = _waker(recorder).wake("session-target", "取信")
    assert outcome.started is True

    url, headers, body = recorder.calls[0]
    assert url == "http://127.0.0.1:19387/api/session/prompt"
    assert headers["cookie"].startswith(cookie_name_for("127.0.0.1:19387") + "=v1.")
    envelope = json.loads(body)
    assert envelope["type"] == "client-request"
    assert envelope["method"] == "session/prompt"
    # DSH 的 Typert 网关：payload 里恰好一个 plain-object 的 args，且 session/prompt
    # 的 args 只接受一个 request 字段。参数放错层会被 DSH 直接拒（实测过）。
    request = envelope["payload"]["args"]["request"]
    assert request["sessionId"] == "session-target"
    assert request["mode"] == "queue"
    assert request["content"][0]["text"] == "取信"
    assert "requestId" in request


def test_wake_reports_rejection_honestly() -> None:
    outcome = _waker(_Recorder(status=401, body="unauthorized")).wake("session-target", "取信")
    assert outcome.started is False
    assert outcome.status == 401
    assert "401" in outcome.detail
    assert outcome.unavailable is True, "鉴权被拒 = 通道用不了，不是投递失败"


def test_wake_connection_error_is_unavailable() -> None:
    class _Boom:
        def __call__(self, url, headers, body, timeout):  # noqa: ANN001
            raise OSError("connection refused")

    outcome = _waker(_Boom()).wake("session-target", "取信")
    assert outcome.started is False
    assert outcome.unavailable is True, "连不上 = 通道用不了：消息该留在队列里"
    assert "连不上" in outcome.detail


def test_wake_reports_not_accepted() -> None:
    recorder = _Recorder(body=json.dumps({"result": {"ok": True, "value": {"accepted": False}}}))
    outcome = _waker(recorder).wake("session-target", "取信")
    assert outcome.started is False
    assert outcome.unavailable is False, "宿主明确拒收属于本次失败，不是通道不可用"


def test_wake_without_session_id_is_unavailable() -> None:
    outcome = _waker(_Recorder()).wake("", "取信")
    assert outcome.started is False and outcome.unavailable is True


def test_from_environment_is_none_without_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAILBOX_DSH_HOME", str(tmp_path))
    monkeypatch.setenv("MAILBOX_DSH_WEB_URL", "http://127.0.0.1:19387")
    assert DshWebWaker.from_environment() is None, "没有密钥就没有通道"

    (tmp_path / ".credentials.yaml").write_text(_credentials_text(), encoding="utf-8")
    waker = DshWebWaker.from_environment()
    assert waker is not None and waker.authority == "127.0.0.1:19387"


# ---------------------------------------------------------------------------
# 与投递的集成：只有真注入成功才 delivered
# ---------------------------------------------------------------------------


class _StubWaker:
    host_type = "dsh"

    def __init__(self, started: bool = True, unavailable: bool = False) -> None:
        self.started = started
        self.unavailable = unavailable
        self.session_ids: list[str] = []

    def wake(self, session_id: str, text: str) -> WakeOutcome:
        self.session_ids.append(session_id)
        return WakeOutcome(self.started, "stub", unavailable=self.unavailable)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return load_settings(
        data_dir=tmp_path / "mailbox",
        presence=PresencePolicy(),
        rate_limits=RateLimits(),
        delivery=DeliveryPolicy(),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(utc_now())


@pytest.fixture
def broker(settings, clock) -> Broker:
    instance = Broker(settings, clock=clock)
    try:
        yield instance
    finally:
        instance.stop()


def _bind(broker: Broker, session: str, *, pid: int, host: str = "dsh", level: int = 2):
    account, _ = broker.accounts.register_account(
        HostIdentity(host, "desktop-default", session),
        display_name=session,
        capability_level=HostCapabilityLevel(level),
    )
    connection = broker.accounts.open_connection(
        account.account_id, capability_level=HostCapabilityLevel(level), host_pid=pid
    )
    return account, connection


def _send(broker: Broker, connection_id: str, to_account_id: str, text: str = "你好"):
    return broker.conversations.start_conversation(
        connection_id=connection_id, to_account_id=to_account_id, text=text
    )


def _delivery(broker: Broker, delivery_id: str):
    with broker.unit_of_work().transaction() as uow:
        return uow.deliveries.get(delivery_id)


def test_online_target_with_channel_is_injected_and_delivered(broker) -> None:
    waker = _StubWaker(started=True)
    broker.context.waker = waker
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())

    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert waker.session_ids == ["session-target"], "必须按目标原生会话 ID 注入"
    assert report.dispatched == 1
    assert delivery is not None and delivery.state is DeliveryState.DELIVERED
    assert delivery.injected_at is not None


def test_message_burst_wakes_one_session_once(broker) -> None:
    waker = _StubWaker()
    broker.context.waker = waker
    _, sender = _bind(broker, "sender", pid=os.getpid())
    target, _ = _bind(broker, "target", pid=os.getpid())
    messages = [_send(broker, sender.connection_id, target.account_id, f"连续消息 {index}") for index in range(5)]
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 5
    assert waker.session_ids == ["target"]
    for message in messages:
        delivery = _delivery(broker, message.delivery_id)
        assert delivery.state is DeliveryState.DELIVERED
        assert delivery.attempt == 1
    assert broker.deliveries.dispatch_due().scanned == 0


def test_failed_burst_wake_defers_every_claimed_message(broker, clock) -> None:
    waker = _StubWaker(started=False)
    broker.context.waker = waker
    _, sender = _bind(broker, "sender", pid=os.getpid())
    target, _ = _bind(broker, "target", pid=os.getpid())
    messages = [_send(broker, sender.connection_id, target.account_id, f"待重试 {index}") for index in range(3)]
    assert broker.deliveries.dispatch_due().failed == 3
    assert waker.session_ids == ["target"]
    for message in messages:
        delivery = _delivery(broker, message.delivery_id)
        assert delivery.state is DeliveryState.FAILED
        assert delivery.next_attempt_at > clock.now()
    assert broker.deliveries.dispatch_due().scanned == 0
    clock.advance(3)
    assert broker.deliveries.dispatch_due().failed == 3
    assert waker.session_ids == ["target", "target"]


def test_completed_mail_does_not_wake_recipient(broker) -> None:
    waker = _StubWaker()
    broker.context.waker = waker
    _, sender = _bind(broker, "sender", pid=os.getpid())
    target, recipient = _bind(broker, "target", pid=os.getpid())
    sent = _send(broker, sender.connection_id, target.account_id)
    broker.conversations.set_message_status(
        connection_id=recipient.connection_id, message_id=sent.message.message_id, processing="completed",
    )
    assert broker.deliveries.dispatch_due().dispatched == 0
    assert waker.session_ids == []


def test_global_pause_keeps_mail_queued_without_waking(settings, clock) -> None:
    waker = _StubWaker()
    instance = Broker(settings.with_overrides(global_pause=True), clock=clock, waker=waker)
    try:
        _, sender = _bind(instance, "sender", pid=os.getpid())
        target, _ = _bind(instance, "target", pid=os.getpid())
        sent = _send(instance, sender.connection_id, target.account_id)
        assert instance.deliveries.dispatch_due().dispatched == 0
        assert waker.session_ids == []
        delivery = _delivery(instance, sent.delivery_id)
        assert delivery.state is DeliveryState.QUEUED
        assert delivery.attempt == 0
    finally:
        instance.stop()


def test_offline_backlog_cannot_starve_online_session(settings, clock) -> None:
    waker = _StubWaker()
    instance = Broker(settings.with_overrides(delivery=DeliveryPolicy(dispatch_batch_size=2)), clock=clock, waker=waker)
    try:
        _, sender = _bind(instance, "sender", pid=os.getpid())
        offline, _ = _bind(instance, "offline", pid=DEAD_PID)
        online, _ = _bind(instance, "online", pid=os.getpid())
        backlog = [_send(instance, sender.connection_id, offline.account_id, f"离线消息 {index}") for index in range(6)]
        live = _send(instance, sender.connection_id, online.account_id, "在线来信")
        for _ in range(4):
            instance.deliveries.dispatch_due()
            if _delivery(instance, live.delivery_id).state is DeliveryState.DELIVERED:
                break
        assert _delivery(instance, live.delivery_id).state is DeliveryState.DELIVERED
        assert waker.session_ids == ["online"]
        for message in backlog:
            delivery = _delivery(instance, message.delivery_id)
            assert delivery.state is DeliveryState.QUEUED
            assert delivery.attempt == 0
    finally:
        instance.stop()


def test_stale_offline_snapshot_cannot_defer_reconnected_recipient(broker, clock) -> None:
    waker = _StubWaker()
    broker.context.waker = waker
    _, sender = _bind(broker, "sender", pid=os.getpid())
    target, _ = _bind(broker, "target", pid=DEAD_PID)
    sent = _send(broker, sender.connection_id, target.account_id)
    stale_delivery = _delivery(broker, sent.delivery_id)
    _bind(broker, "target", pid=os.getpid())
    broker.deliveries._dispatch_one(stale_delivery, None, now=clock.now())
    current = _delivery(broker, sent.delivery_id)
    assert current.next_attempt_at is None or current.next_attempt_at <= clock.now()
    assert current.attempt == 0
    assert broker.deliveries.dispatch_due().dispatched == 1
    assert waker.session_ids == ["target"]


def test_two_brokers_competing_for_same_burst_wake_once(settings, clock) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    waker = _StubWaker()
    first = Broker(settings, clock=clock, waker=waker)
    second = Broker(settings, clock=clock, waker=waker)
    try:
        _, sender = _bind(first, "sender", pid=os.getpid())
        target, _ = _bind(first, "target", pid=os.getpid())
        messages = [_send(first, sender.connection_id, target.account_id, f"争抢消息 {index}") for index in range(5)]
        barrier = Barrier(2)

        def dispatch(instance):
            barrier.wait(timeout=5)
            return instance.deliveries.dispatch_due()

        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(executor.map(dispatch, [first, second]))
        assert sum(report.dispatched for report in reports) == 5
        assert waker.session_ids == ["target"]
        assert all(_delivery(first, message.delivery_id).state is DeliveryState.DELIVERED for message in messages)
    finally:
        first.stop()
        second.stop()


def test_online_target_without_channel_stays_queued(broker) -> None:
    """在线、也没声明任何注入能力：消息必须留在队列里等它自己取信。"""
    broker.context.waker = None
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid(), level=0)
    target, _ = _bind(broker, "session-target", pid=os.getpid(), level=0)

    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert report.held_for_pickup == 1
    assert delivery is not None and delivery.state is DeliveryState.QUEUED, (
        "没有注入通道时必须是 queued：不是 delivered，也不是 failed"
    )


def test_offline_target_is_queued_and_never_injected(broker) -> None:
    waker = _StubWaker(started=True)
    broker.context.waker = waker
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=DEAD_PID)

    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert report.skipped_offline == 1
    assert waker.session_ids == [], "离线账号绝不能被唤醒、更不能去打开程序"
    assert delivery is not None and delivery.state is DeliveryState.QUEUED


def test_failed_injection_is_retryable_failure(broker) -> None:
    broker.context.waker = _StubWaker(started=False)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())

    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert report.failed == 1
    assert delivery is not None and delivery.state is DeliveryState.FAILED
    assert delivery.next_attempt_at is not None


def test_unavailable_channel_is_not_a_failure(broker) -> None:
    broker.context.waker = _StubWaker(started=False, unavailable=True)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())

    result = _send(broker, sender_connection.connection_id, target.account_id)
    broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED, (
        "通道不可用不是投递失败：消息留在队列里等目标取信"
    )


def test_persistent_injection_failure_becomes_dead_letter(broker, clock, settings) -> None:
    """真失败重试到上限必须进死信，不能无限重试下去。"""
    broker.context.waker = _StubWaker(started=False)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())
    result = _send(broker, sender_connection.connection_id, target.account_id)

    state = None
    for _ in range(settings.delivery.max_attempts + 2):
        broker.deliveries.dispatch_due()
        with broker.unit_of_work().transaction() as uow:
            current = uow.deliveries.get(result.delivery_id)
        state = current.state
        if state is DeliveryState.DEAD_LETTER:
            break
        clock.advance(600)  # 越过退避窗口，让下一次重试成熟
    assert state is DeliveryState.DEAD_LETTER


def test_stalled_dispatch_to_dead_target_returns_to_queue(broker, clock) -> None:
    """派发后目标进程死了：消息退回队列（不是失败），等它上线再投。"""
    from datetime import timedelta

    broker.context.waker = _StubWaker(started=False)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=DEAD_PID)
    # 先让它"在线"并通过 adapter 模式派发出去（模拟派发后进程才退出）。
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.current_for_account(target.account_id)
        connection.host_pid = os.getpid()
        uow.connections.update(connection)
    broker.context.waker = None
    result = _send(broker, sender_connection.connection_id, target.account_id)
    broker.deliveries.dispatch_due()
    # 进程退出（改成死 PID），再让"派发超时"扫一轮。
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.current_for_account(target.account_id)
        connection.host_pid = DEAD_PID
        uow.connections.update(connection)
    clock.advance(120)
    requeued = broker.deliveries.requeue_stalled(older_than_seconds=60)
    assert requeued == 1
    delivery = _delivery(broker, result.delivery_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED
    assert delivery.attempt == 0, "退回队列不该消耗尝试次数"


def test_channel_only_applies_to_its_own_host(broker) -> None:
    """DSH 的通道不能拿去"唤醒"别的宿主账号。"""
    broker.context.waker = _StubWaker(started=True)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid(), level=0)
    codex, _ = _bind(broker, "thread-1", pid=os.getpid(), host="codex", level=0)

    result = _send(broker, sender_connection.connection_id, codex.account_id)
    report = broker.deliveries.dispatch_due()

    delivery = _delivery(broker, result.delivery_id)
    assert report.held_for_pickup == 1
    assert delivery is not None and delivery.state is DeliveryState.QUEUED
