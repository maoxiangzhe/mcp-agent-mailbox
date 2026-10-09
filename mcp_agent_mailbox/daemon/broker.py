"""Mailbox Broker：本地常驻的权威服务。

职责（设计文档 §4.1）：

- 持有唯一权威存储（SQLite）与全部应用服务；
- 维护后台维护循环：回收过期租约、派发到期投递、补投未确认投递、清理过期计数；
- 作为**唯一**的状态中心：stdio 端点与适配器都通过它干活，不允许各自维护状态。

Broker 可以被两种方式使用：

1. **进程内**：MCP 服务器直接持有 Broker 对象（单进程模式，最简单）；
2. **跨进程**：Broker 作为常驻服务监听本地套接字，stdio 代理连上来远程调用
   （``infrastructure/transport`` 提供协议；见 ``docs/broker.md``）。

无论哪种方式，状态都只有一份。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta

from ..application.account_service import AccountService
from ..application.context import ServiceContext
from ..application.conversation_service import ConversationService
from ..application.delivery_service import DeliveryService
from ..application.presence_service import PresenceService
from ..config import Settings, load_settings
from ..domain.timestamps import utc_now
from ..infrastructure.observability import METRIC_COUNTER, get_logger, setup_diagnostic_logging
from ..infrastructure.sqlite import Database, initialize
from ..infrastructure.sqlite import SqliteUnitOfWorkFactory
from ..ports.clock import Clock, SystemClock
from .event_bus import EventBus

__all__ = ["Broker", "BrokerStats", "MaintenanceReport", "default_waker"]


def default_waker() -> object | None:
    """按环境自动装配宿主注入通道。

    DSH 的通道链是 **B（进程内插件，无需凭据）优先 → A（本地接口，需签名密钥）兜底
    → 两个都不行就明确报错**。两个都装不出来时返回 ``None``：那就等于"在线但不能注入"，
    消息保持 queued 等目标自己取信，绝不谎报 delivered。
    """
    try:
        from ..adapters.dsh_wake_chain import DshWakeChain

        from ..adapters.codex_wake import CodexAppServerWaker
        from ..adapters.codex_queue_wake import CodexQueueWaker
        from ..adapters.wake_router import WakeRouter

        channels = [DshWakeChain.from_environment()]
        try:
            channels.append(CodexAppServerWaker.from_environment() or CodexQueueWaker.from_environment())
        except ValueError:
            pass  # Invalid Codex configuration must not disable DSH.
        channels = [channel for channel in channels if channel is not None]
        if len(channels) == 1:
            return channels[0]
        return WakeRouter(channels) if channels else None
    except Exception:  # noqa: BLE001 - 自动装配失败不应该拖垮 Broker
        return None


def _channel_status(waker: object | None) -> dict[str, object] | None:
    """注入通道现状（给 doctor 看）：没有通道就返回 ``None``。"""
    status = getattr(waker, "channel_status", None)
    if not callable(status):
        return None
    try:
        return status()
    except Exception:  # noqa: BLE001 - 诊断不该因为探测失败而崩
        return None


@dataclass(slots=True)
class MaintenanceReport:
    """一轮维护的结果。诊断命令直接展示它。"""

    expired_connections: int = 0
    dispatched: int = 0
    notified_only: int = 0
    held_for_pickup: int = 0
    failed: int = 0
    skipped_offline: int = 0
    requeued: int = 0
    pruned_counters: int = 0
    dead_lettered: int = 0
    rounds: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "expired_connections": self.expired_connections,
            "dispatched": self.dispatched,
            "notified_only": self.notified_only,
            "held_for_pickup": self.held_for_pickup,
            "failed": self.failed,
            "skipped_offline": self.skipped_offline,
            "requeued": self.requeued,
            "pruned_counters": self.pruned_counters,
            "dead_lettered": self.dead_lettered,
            "rounds": self.rounds,
        }


@dataclass(slots=True)
class BrokerStats:
    """Broker 运行状态快照。"""

    database_path: str
    schema_version: int
    started_at: str
    uptime_seconds: float
    maintenance_rounds: int
    subscribers: int
    event_queue_depths: dict[str, int]
    counters: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "database_path": self.database_path,
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds, 3),
            "maintenance_rounds": self.maintenance_rounds,
            "subscribers": self.subscribers,
            "event_queue_depths": self.event_queue_depths,
            "counters": self.counters,
        }


class Broker:
    """权威状态中心。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
        database: Database | None = None,
        waker: object | None = None,
        auto_waker: bool = True,
    ) -> None:
        self.settings = settings or load_settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._logger = setup_diagnostic_logging(self.settings)
        self.database = database or initialize(self.settings.database_path)
        self.clock = clock or SystemClock()
        self.events = event_bus or EventBus()
        self.unit_of_work = SqliteUnitOfWorkFactory(self.database)
        if waker is None:
            waker = default_waker() if auto_waker else None
        self.context = ServiceContext(
            settings=self.settings,
            unit_of_work=self.unit_of_work,
            publisher=self.events,
            clock=self.clock,
            logger=self._logger,
            waker=waker,
        )
        self.accounts = AccountService(self.context)
        self.conversations = ConversationService(self.context)
        self.deliveries = DeliveryService(self.context)
        self.presence = PresenceService(self.context)

        self._started_at = self.clock.now()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._maintenance_rounds = 0
        self._maintenance_interval = 1.0
        self._last_maintenance: MaintenanceReport | None = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self, *, background: bool = True) -> None:
        """启动后台维护循环。

        适配器订阅（``EventBus.register``）由注册方负责，Broker 不替它们猜测
        能力等级。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        if not background:
            return
        self._thread = threading.Thread(
            target=self._maintenance_loop, name="mailbox-maintenance", daemon=True
        )
        self._thread.start()
        self._logger.info(
            "Broker 已启动",
            extra={
                "event": "broker.started",
                "context": {
                    "database": str(self.settings.database_path),
                    "heartbeat_seconds": self.settings.presence.heartbeat_interval_seconds,
                    "lease_seconds": self.settings.presence.lease_seconds,
                },
            },
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止维护循环并关闭数据库。可重复调用。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        try:
            close = getattr(self.context.waker, "close", None)
            if callable(close):
                close()
            self.database.close()
        finally:
            self._logger.info("Broker 已停止", extra={"event": "broker.stopped", "context": {}})

    def __enter__(self) -> "Broker":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        self.stop()
        return None

    # -- 维护循环 ---------------------------------------------------------

    def maintenance_once(self) -> MaintenanceReport:
        """跑一轮维护。测试直接调用它，不依赖后台线程。

        顺序有讲究：先回收"托管进程已退出"的连接（否则会朝已经死掉的连接派发），
        再补投"派发后未确认"的投递，最后派发到期的。
        """
        report = MaintenanceReport()
        now = utc_now()

        expired = self.accounts.sweep_disconnected_connections()
        report.expired_connections = len(expired)
        if expired:
            # 连接被回收后在线状态发生变化，广播一次，让对端列表立刻更新。
            for connection_id in expired:
                METRIC_COUNTER.increment("connections.recycled")

        report.requeued = self.deliveries.requeue_stalled(
            older_than_seconds=max(self.settings.presence.lease_seconds, 30.0)
        )
        dispatch = self.deliveries.dispatch_due()
        report.dispatched = dispatch.dispatched
        report.notified_only = dispatch.notified_only
        report.held_for_pickup = dispatch.held_for_pickup
        report.failed = dispatch.failed
        report.skipped_offline = dispatch.skipped_offline
        report.dead_lettered = dispatch.dead_lettered

        # 计数窗口清理：保留最近 2 小时即可覆盖所有窗口。
        with self.unit_of_work().transaction() as uow:
            report.pruned_counters = uow.rate_counters.prune(
                before=now - timedelta(hours=2)
            )

        self._maintenance_rounds += 1
        report.rounds = self._maintenance_rounds
        self._last_maintenance = report
        return report

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.maintenance_once()
            except Exception:  # noqa: BLE001 - 维护循环必须活下去
                self._logger.exception(
                    "维护循环出错（将继续运行）",
                    extra={"event": "broker.maintenance_failed", "context": {}},
                )
            self._stop.wait(self._maintenance_interval)

    # -- 诊断 -------------------------------------------------------------

    def stats(self) -> BrokerStats:
        """运行状态快照。"""
        from ..infrastructure.sqlite import schema_version

        subscribers = self.events.subscribers()
        depths = {
            subscription.connection_id: self.events.pending(subscription.connection_id)
            for subscription in subscribers
        }
        return BrokerStats(
            database_path=str(self.settings.database_path),
            schema_version=schema_version(self.database.connection()),
            started_at=self._started_at.isoformat(),
            uptime_seconds=(self.clock.now() - self._started_at).total_seconds(),
            maintenance_rounds=self._maintenance_rounds,
            subscribers=len(subscribers),
            event_queue_depths=depths,
            counters=METRIC_COUNTER.snapshot(),
        )

    def diagnose(self) -> dict[str, object]:
        """``mailbox doctor`` 用的综合诊断。"""
        connection = self.database.connection()
        with self.unit_of_work().transaction() as uow:
            accounts = uow.accounts.list_accounts(limit=10_000)
        presence = self.presence.for_accounts(accounts) if accounts else {}
        by_state: dict[str, int] = {}
        for view in presence.values():
            by_state[view.state.value] = by_state.get(view.state.value, 0) + 1

        pending_total = 0
        dead_letters = 0
        with self.unit_of_work().transaction() as uow:
            for account in accounts:
                pending_total += len(uow.deliveries.pending_for_account(account.account_id, limit=1000))
            row = connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE state = 'dead_letter'"
            ).fetchone()
            dead_letters = int(row[0]) if row else 0

        return {
            "database_path": str(self.settings.database_path),
            "integrity_check": self.database.integrity_check(),
            "account_count": len(accounts),
            # 注入通道现状：B（DSH 内插件）优先 → A（DSH 本地接口）兜底 → 都没有就报错。
            "wake_channels": _channel_status(self.context.waker),
            "presence_breakdown": by_state,
            "pending_deliveries_sampled": pending_total,
            "dead_letter_count": dead_letters,
            "stats": self.stats().to_dict(),
            "last_maintenance": (
                self._last_maintenance.to_dict() if self._last_maintenance else None
            ),
            "settings": {
                "heartbeat_seconds": self.settings.presence.heartbeat_interval_seconds,
                "lease_seconds": self.settings.presence.lease_seconds,
                "grace_seconds": self.settings.presence.grace_seconds,
                "max_auto_turns": self.settings.rate_limits.max_auto_turns_per_conversation,
                "max_delivery_attempts": self.settings.delivery.max_attempts,
                "global_pause": self.settings.global_pause,
                "legacy_tools_enabled": self.settings.legacy_tools_enabled,
            },
        }

    # -- 便捷入口（MCP 层与适配器使用） -----------------------------------

    def attach_adapter(self, adapter, connection_id: str, *, subscription=None) -> None:
        """把适配器的订阅登记到事件总线。

        适配器自己声明能力等级；Broker 不替它升级等级——这正是"不伪造 Level 2"
        的落点。
        """
        from ..ports.event_transport import Subscription

        sub = subscription or Subscription(
            account_id=adapter.account_id,
            connection_id=connection_id,
            generation=adapter.generation,
            capability_level=int(adapter.capabilities.level),
        )
        self.events.register(sub)

    def wait_for_events(self, connection_id: str, timeout: float):
        """从事件总线取事件（适配器循环用）。"""
        return self.events.receive(connection_id, timeout)

    def sleep(self, seconds: float) -> None:
        """可测试的等待：真实实现直接睡，测试可换成即时返回。"""
        time.sleep(seconds)
