"""实时事件通道端口。

两类角色刻意分开，因为它们的安全边界不同：

    EventPublisher  只应由 Broker 内部持有，向某个账号的主连接推送事件；
    EventTransport  只应由适配器持有，订阅"我"这个账号的事件。

事件只携带最小路由信息（``delivery_id`` / ``account_id`` / ``conversation_id`` /
``message_id`` / ``attempt``），正文必须通过受身份约束的接口单独获取。这样通知
通道即使被窃听，也不会泄露不属于该连接的消息（设计文档 §9.1）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "EVENT_ACCOUNT_REGISTERED",
    "EVENT_CONVERSATION_BLOCKED",
    "EVENT_DELIVERY_CANCELLED",
    "EVENT_DELIVERY_DISPATCHED",
    "EVENT_MAILBOX_MESSAGE",
    "EVENT_PRESENCE_CHANGED",
    "Event",
    "EventPublisher",
    "EventTransport",
    "Subscription",
]


class _EventType(str, enum.Enum):
    """实时事件类型（设计文档 §9.1 与 §14）。"""

    MAILBOX_MESSAGE = "mailbox/message"
    PRESENCE_CHANGED = "presence/changed"
    DELIVERY_CANCELLED = "delivery/cancelled"
    CONVERSATION_BLOCKED = "conversation/blocked"
    ACCOUNT_REGISTERED = "account/registered"
    CONNECTION_OPENED = "connection/opened"
    CONNECTION_RENEWED = "connection/renewed"
    CONNECTION_CLOSED = "connection/closed"
    DELIVERY_DISPATCHED = "delivery/dispatched"
    DELIVERY_ACKNOWLEDGED = "delivery/acknowledged"
    DELIVERY_FAILED = "delivery/failed"
    WAKE_REQUESTED = "wake/requested"
    WAKE_STARTED = "wake/started"
    WAKE_FAILED = "wake/failed"
    PROCESSING_CHANGED = "processing/changed"
    MESSAGE_CREATED = "message/created"
    REPLY_CREATED = "reply/created"


EVENT_MAILBOX_MESSAGE = _EventType.MAILBOX_MESSAGE
EVENT_PRESENCE_CHANGED = _EventType.PRESENCE_CHANGED
EVENT_DELIVERY_CANCELLED = _EventType.DELIVERY_CANCELLED
EVENT_CONVERSATION_BLOCKED = _EventType.CONVERSATION_BLOCKED
EVENT_DELIVERY_DISPATCHED = _EventType.DELIVERY_DISPATCHED
EVENT_ACCOUNT_REGISTERED = _EventType.ACCOUNT_REGISTERED


@dataclass(frozen=True, slots=True)
class Event:
    """一个实时事件。

    ``payload`` 只允许放标识符与状态，不得放消息正文、凭据或 Token（设计文档 §14）。
    """

    type: str
    account_id: str
    created_at: datetime
    payload: dict[str, object] = field(default_factory=dict)
    #: 目标账号的主连接代次；旧代次连接不得收到新代次的唤醒事件。
    generation: int | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "type": self.type,
            "account_id": self.account_id,
            "created_at": self.created_at.isoformat(),
            "generation": self.generation,
        }
        body.update(self.payload)
        return body


@dataclass(frozen=True, slots=True)
class Subscription:
    """一个账号的实时事件订阅。"""

    account_id: str
    connection_id: str
    generation: int
    capability_level: int

    @property
    def can_wake(self) -> bool:
        return self.capability_level >= 2


@runtime_checkable
class EventPublisher(Protocol):
    """Broker 侧：把事件推给某个账号当前的主连接。"""

    def publish(self, event: Event) -> None:
        """投递事件。没有活跃订阅时必须是安全的空操作（离线账号走持久化队列）。"""
        ...

    def push(self, subscription: Subscription, event: Event) -> bool:
        """直接推给指定订阅；返回是否成功写入。"""
        ...


@runtime_checkable
class EventTransport(Protocol):
    """适配器侧：订阅与接收自己账号的事件。"""

    def subscribe(self, subscription: Subscription) -> None:
        ...

    def unsubscribe(self, connection_id: str) -> None:
        ...

    def receive(self, timeout: float) -> Event | None:
        """阻塞至多 ``timeout`` 秒等待下一个事件；无事件返回 ``None``。"""
        ...
