"""进程内事件通道。

Broker 发布，适配器订阅。``publish`` 只把事件发到目标账号**当前主连接**的队列，
因此旧代次连接收不到新代次的唤醒事件（设计文档 §5.3）。

队列有界：适配器卡死时不能让 Broker 的内存无限增长；溢出时丢弃最旧的事件并计数，
因为权威事实已经在 SQLite 里，重投会由投递重试补上。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field

from ..infrastructure.observability import METRIC_COUNTER, get_logger
from ..ports.event_transport import Event, Subscription

__all__ = ["EventBus", "NullEventPublisher"]

_DEFAULT_QUEUE_SIZE = 512


@dataclass(slots=True)
class _Mailbox:
    subscription: Subscription
    queue: "queue.Queue[Event]" = field(default_factory=lambda: queue.Queue(maxsize=_DEFAULT_QUEUE_SIZE))
    dropped: int = 0

    def offer(self, event: Event) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            # 队列满：丢掉最旧的一条，把位置让给最新事件（唤醒比历史更重要）。
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except queue.Empty:  # pragma: no cover - 竞态兜底
                return False
            self.dropped += 1
            return False


class EventBus:
    """线程安全的进程内事件总线。"""

    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._lock = threading.RLock()
        self._by_connection: dict[str, _Mailbox] = {}
        self._current: dict[str, str] = {}  # account_id -> connection_id
        self._queue_size = queue_size
        self._logger = get_logger("events")

    # -- Broker 侧 --------------------------------------------------------

    def register(self, subscription: Subscription) -> None:
        """登记一个账号的订阅，并把它设为该账号的当前主连接。"""
        with self._lock:
            previous = self._current.get(subscription.account_id)
            if previous is not None and previous != subscription.connection_id:
                self._drop_locked(previous)
            self._by_connection[subscription.connection_id] = _Mailbox(subscription=subscription)
            self._current[subscription.account_id] = subscription.connection_id

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._drop_locked(connection_id)
            for account_id, current in list(self._current.items()):
                if current == connection_id:
                    del self._current[account_id]

    def _drop_locked(self, connection_id: str) -> None:
        self._by_connection.pop(connection_id, None)

    def publish(self, event: Event) -> None:
        """推给目标账号的当前主连接。没有订阅时是安全的空操作。"""
        with self._lock:
            connection_id = self._current.get(event.account_id)
            mailbox = self._by_connection.get(connection_id) if connection_id else None
        if mailbox is None:
            METRIC_COUNTER.increment("events.unsubscribed")
            return
        if event.generation is not None and event.generation != mailbox.subscription.generation:
            # 事件的目标代次不是当前主连接：丢弃而不是投给旧连接。
            METRIC_COUNTER.increment("events.stale_generation")
            return
        if not mailbox.offer(event):
            METRIC_COUNTER.increment("events.dropped")
            self._logger.warning(
                "事件队列溢出，已丢弃最旧事件",
                extra={
                    "event": "events.overflow",
                    "context": {
                        "account_id": event.account_id,
                        "connection_id": mailbox.subscription.connection_id,
                    },
                },
            )

    def push(self, subscription: Subscription, event: Event) -> bool:
        """直接推给指定连接（诊断与契约测试用）。"""
        with self._lock:
            mailbox = self._by_connection.get(subscription.connection_id)
        if mailbox is None:
            return False
        return mailbox.offer(event)

    # -- 适配器侧 ---------------------------------------------------------

    def receive(self, connection_id: str, timeout: float) -> Event | None:
        """阻塞至多 ``timeout`` 秒等待该连接的下一个事件。"""
        with self._lock:
            mailbox = self._by_connection.get(connection_id)
        if mailbox is None:
            return None
        try:
            return mailbox.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def pending(self, connection_id: str) -> int:
        with self._lock:
            mailbox = self._by_connection.get(connection_id)
        return mailbox.queue.qsize() if mailbox else 0

    def dropped(self, connection_id: str) -> int:
        with self._lock:
            mailbox = self._by_connection.get(connection_id)
        return mailbox.dropped if mailbox else 0

    def subscribers(self) -> list[Subscription]:
        with self._lock:
            return [mailbox.subscription for mailbox in self._by_connection.values()]

    def reset(self) -> None:
        with self._lock:
            self._by_connection.clear()
            self._current.clear()


class NullEventPublisher:
    """什么都不做的发布器：单测与"没有实时通道"的场景使用。"""

    def publish(self, event: Event) -> None:  # noqa: D102 - 协议实现
        return None

    def push(self, subscription: Subscription, event: Event) -> bool:  # noqa: D102
        return False

    def register(self, subscription: Subscription) -> None:  # noqa: D102
        return None

    def unregister(self, connection_id: str) -> None:  # noqa: D102
        return None

    def receive(self, connection_id: str, timeout: float) -> Event | None:  # noqa: D102
        return None
