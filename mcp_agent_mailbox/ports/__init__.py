"""端口层：应用层需要的外部能力协议。

每个端口都必须有真实使用者：
    repositories   -> application 的服务与 infrastructure.sqlite 的实现
    clock          -> application 与 daemon（测试注入可控时钟）
    event_transport-> daemon.Broker 发布、adapters 订阅
    host_adapter   -> adapters 实现、daemon 与契约测试使用
"""

from __future__ import annotations

from .clock import Clock, FrozenClock, SystemClock
from .event_transport import Event, EventPublisher, EventTransport, Subscription
from .host_adapter import (
    AdapterCapabilities,
    ExternalEnvelope,
    HostAdapter,
    InjectionResult,
    ProbeResult,
    Registration,
    SessionBinding,
    WakeResult,
    render_envelope,
)

__all__ = [
    "AdapterCapabilities",
    "Clock",
    "Event",
    "EventPublisher",
    "EventTransport",
    "ExternalEnvelope",
    "FrozenClock",
    "HostAdapter",
    "InjectionResult",
    "ProbeResult",
    "Registration",
    "SessionBinding",
    "Subscription",
    "SystemClock",
    "WakeResult",
    "render_envelope",
]
