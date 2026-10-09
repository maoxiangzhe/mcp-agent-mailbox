"""常驻 Broker 与生命周期管理。

Broker 是唯一权威状态中心；MCP 端点、适配器都通过它干活。本层不依赖 MCP SDK，
因此可以在没有客户端的情况下单独跑起来做诊断与维护。
"""

from __future__ import annotations

from .broker import Broker, BrokerStats, MaintenanceReport
from .event_bus import EventBus, NullEventPublisher

__all__ = [
    "Broker",
    "BrokerStats",
    "EventBus",
    "MaintenanceReport",
    "NullEventPublisher",
]
