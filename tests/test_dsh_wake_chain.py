"""DSH 注入通道 B（进程内插件）与"B 优先 → A 兜底 → 报错"通道链的测试。

全部不联网：HTTP 由注入的替身完成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_agent_mailbox.adapters.dsh_plugin_wake import DshPluginWaker
from mcp_agent_mailbox.adapters.dsh_wake import DshWebWaker, WakeOutcome
from mcp_agent_mailbox.adapters.dsh_wake_chain import DshWakeChain
from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, Settings, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import HostIdentity
from mcp_agent_mailbox.domain.messages import DeliveryState
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresencePolicy
from mcp_agent_mailbox.domain.timestamps import utc_now
from mcp_agent_mailbox.ports.clock import FrozenClock

TOKEN = "t" * 24
DEAD_PID = 2_147_483_646


class StubHttp:
    """替身 HTTP：按 (方法, URL 结尾) 决定返回什么，并记录调用。"""

    def __init__(self, responses=None, raise_on=None) -> None:
        #: key 形如 ('POST', '/dsh-wake') -> (status, body) 或 Exception
        self.responses = responses or {}
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=3.0):
        key = (method.upper(), "/" + url.rstrip("/").split("/", 3)[-1])
        self.calls.append((method.upper(), url, headers or {}, body))
        if key in self.raise_on:
            raise OSError("connection refused")
        for (want_method, want_tail), value in self.responses.items():
            if want_method == method.upper() and url.endswith(want_tail):
                if isinstance(value, Exception):
                    raise value
                return value
        raise OSError("connection refused")


def ok_wake(session_id: str = "session-target") -> tuple[int, str]:
    return 200, json.dumps({"accepted": True, "sessionId": session_id})


def ok_health() -> tuple[int, str]:
    return 200, json.dumps({"ok": True, "plugin": "dsh-mailbox-wake-bridge"})


# ---------------------------------------------------------------------------
# 通道 B 客户端
# ---------------------------------------------------------------------------


def test_plugin_waker_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("MAILBOX_DSH_WAKE_TOKEN", raising=False)
    assert DshPluginWaker.from_environment() is None, "没有令牌就不能开这条通道"


def test_plugin_waker_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_DSH_WAKE_TOKEN", TOKEN)
    monkeypatch.setenv("MAILBOX_DSH_WAKE_URL", "http://127.0.0.1:8799/dsh-wake")
    waker = DshPluginWaker.from_environment()
    assert waker is not None
    assert waker.host_type == "dsh"
    assert waker.channel_name == "plugin_channel"
    assert waker.wake_url.endswith("/dsh-wake")


def test_plugin_waker_success() -> None:
    stub = StubHttp({("POST", "/dsh-wake"): ok_wake()})
    waker = DshPluginWaker("http://127.0.0.1:8799/dsh-wake", TOKEN, request=stub)
    outcome = waker.wake("session-target", "去取信")
    assert outcome.started is True
    method, url, headers, body = stub.calls[0]
    assert method == "POST" and url.endswith("/dsh-wake")
    assert headers["x-dsh-wake-token"] == TOKEN
    assert json.loads(body)["sessionId"] == "session-target"


def test_plugin_waker_connection_refused_is_unavailable() -> None:
    stub = StubHttp({})  # 什么都连不上
    waker = DshPluginWaker("http://127.0.0.1:8799/dsh-wake", TOKEN, request=stub)
    outcome = waker.wake("session-target", "去取信")
    assert outcome.started is False
    assert outcome.unavailable is True, "连不上 = 通道不可用（消息留在队列里）"
    assert "通道B" in outcome.detail


def test_plugin_waker_401_is_unavailable() -> None:
    stub = StubHttp({("POST", "/dsh-wake"): (401, "invalid token")})
    waker = DshPluginWaker("http://127.0.0.1:8799/dsh-wake", TOKEN, request=stub)
    outcome = waker.wake("session-target", "去取信")
    assert outcome.unavailable is True


def test_plugin_waker_host_error_is_a_real_failure() -> None:
    stub = StubHttp({("POST", "/dsh-wake"): (409, json.dumps({"accepted": False, "error": "session/writer-held"}))})
    waker = DshPluginWaker("http://127.0.0.1:8799/dsh-wake", TOKEN, request=stub)
    outcome = waker.wake("session-target", "去取信")
    assert outcome.started is False
    assert outcome.unavailable is False, "宿主明确不可注入 = 这次失败，可重试"
    assert "writer-held" in outcome.detail


def test_plugin_waker_health_probe_is_cached() -> None:
    stub = StubHttp({("GET", "/healthz"): ok_health()})
    waker = DshPluginWaker("http://127.0.0.1:8799/dsh-wake", TOKEN, request=stub)
    assert waker.channel_available(now=100.0) is True
    assert waker.channel_available(now=101.0) is True
    assert len([c for c in stub.calls if c[0] == "GET"]) == 1, "5 秒内不重复探测"


# ---------------------------------------------------------------------------
# 通道链：B 优先 → A 兜底 → 报错
# ---------------------------------------------------------------------------


class StubWaker:
    host_type = "dsh"

    def __init__(self, *, name: str, started: bool, unavailable: bool = False, available: bool = True):
        self.channel_name = name
        self.started = started
        self.unavailable = unavailable
        self._available = available
        self.calls = 0

    def channel_available(self) -> bool:
        return self._available

    def reason_unavailable(self) -> str:
        return f"{self.channel_name} 不可用"

    def wake(self, session_id: str, text: str) -> WakeOutcome:
        self.calls += 1
        return WakeOutcome(self.started, f"{self.channel_name} 结果", unavailable=self.unavailable)


def test_chain_prefers_plugin() -> None:
    plugin = StubWaker(name="plugin_channel", started=True)
    http = StubWaker(name="http_channel", started=True)
    chain = DshWakeChain(plugin, http)
    outcome = chain.wake("s", "text")
    assert outcome.started is True
    assert plugin.calls == 1 and http.calls == 0, "B 成功就不该动 A"
    assert chain.channel_name == "plugin_channel"


def test_chain_falls_back_to_http() -> None:
    plugin = StubWaker(name="plugin_channel", started=False, unavailable=True, available=False)
    http = StubWaker(name="http_channel", started=True)
    chain = DshWakeChain(plugin, http)
    outcome = chain.wake("s", "text")
    assert outcome.started is True
    assert "通道A" in outcome.detail, "兜底成功要标明是哪条通道干的"
    assert http.calls == 1


def test_chain_tries_http_even_after_a_real_plugin_failure() -> None:
    plugin = StubWaker(name="plugin_channel", started=False, unavailable=False)
    http = StubWaker(name="http_channel", started=True)
    chain = DshWakeChain(plugin, http)
    outcome = chain.wake("s", "text")
    assert outcome.started is True and http.calls == 1


def test_chain_reports_error_when_both_unavailable() -> None:
    plugin = StubWaker(name="plugin_channel", started=False, unavailable=True, available=False)
    http = StubWaker(name="http_channel", started=False, unavailable=True, available=False)
    chain = DshWakeChain(plugin, http)
    outcome = chain.wake("s", "text")
    assert outcome.started is False
    assert outcome.unavailable is True
    assert "两个 DSH 注入通道都不可用" in outcome.detail, "报错3：必须说清两条通道都不行"
    assert "plugin_channel" in outcome.detail and "http_channel" in outcome.detail
    assert chain.channel_name is None
    assert chain.channel_available() is False


def test_chain_without_any_channel_reports_how_to_fix() -> None:
    chain = DshWakeChain(None, None)
    outcome = chain.wake("s", "text")
    assert outcome.started is False and outcome.unavailable is True
    assert "MAILBOX_DSH_WAKE_TOKEN" in outcome.detail
    assert "credentials.yaml" in outcome.detail
    status = chain.channel_status()
    assert status["active"] is None
    assert status["order"] == ["plugin_channel", "http_channel"]


def test_chain_status_lists_both_channels() -> None:
    plugin = StubWaker(name="plugin_channel", started=True, available=True)
    chain = DshWakeChain(plugin, None)
    status = chain.channel_status()
    assert status["plugin_channel"]["available"] is True
    assert status["http_channel"]["configured"] is False
    assert status["active"] == "plugin_channel"


# ---------------------------------------------------------------------------
# 与投递的集成：B 成功 -> delivered；两个都不可用 -> queued 且原因写清
# ---------------------------------------------------------------------------


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
    instance = Broker(settings, clock=clock, auto_waker=False)
    try:
        yield instance
    finally:
        instance.stop()


def _bind(broker: Broker, session: str, *, pid: int):
    account, _ = broker.accounts.register_account(
        HostIdentity("dsh", "desktop-default", session),
        display_name=session,
        capability_level=HostCapabilityLevel.TOOLS_ONLY,
    )
    connection = broker.accounts.open_connection(
        account.account_id, capability_level=HostCapabilityLevel.TOOLS_ONLY, host_pid=pid
    )
    return account, connection


def _send(broker: Broker, connection_id: str, to_account_id: str):
    return broker.conversations.start_conversation(
        connection_id=connection_id, to_account_id=to_account_id, text="你好"
    )


def _delivery(broker: Broker, delivery_id: str):
    with broker.unit_of_work().transaction() as uow:
        return uow.deliveries.get(delivery_id)


def test_delivery_via_plugin_channel_is_delivered(broker) -> None:
    broker.context.waker = DshWakeChain(
        StubWaker(name="plugin_channel", started=True),
        StubWaker(name="http_channel", started=False),
    )
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())
    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()
    delivery = _delivery(broker, result.delivery_id)
    assert report.dispatched == 1
    assert delivery.state is DeliveryState.DELIVERED


def test_delivery_keeps_queued_and_explains_when_no_channel(broker) -> None:
    broker.context.waker = DshWakeChain(None, None)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=os.getpid())
    result = _send(broker, sender_connection.connection_id, target.account_id)
    broker.deliveries.dispatch_due()
    delivery = _delivery(broker, result.delivery_id)
    assert delivery.state is DeliveryState.QUEUED
    assert "MAILBOX_DSH_WAKE_TOKEN" in (delivery.last_error or ""), "报错3：把怎么修写进记录"


def test_offline_target_is_never_injected_even_with_channel(broker) -> None:
    plugin = StubWaker(name="plugin_channel", started=True)
    broker.context.waker = DshWakeChain(plugin, None)
    _sender, sender_connection = _bind(broker, "session-sender", pid=os.getpid())
    target, _ = _bind(broker, "session-target", pid=DEAD_PID)
    result = _send(broker, sender_connection.connection_id, target.account_id)
    report = broker.deliveries.dispatch_due()
    delivery = _delivery(broker, result.delivery_id)
    assert report.skipped_offline == 1
    assert plugin.calls == 0, "离线账号绝不能被叫醒"
    assert delivery.state is DeliveryState.QUEUED
