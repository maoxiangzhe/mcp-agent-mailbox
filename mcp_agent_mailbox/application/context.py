"""应用服务共享上下文。

一次用例（"注册并绑定会话"、"发一条消息"）的全部外部依赖都在这里：工作单元工厂、
时钟、事件发布器、配置、日志。服务之间不互相持有对方，只共享这个上下文，避免
应用层内部出现循环依赖。

事务边界由各服务方法显式声明：``with self.uow.transaction()`` 包住一次用例的写入，
退出时提交或回滚。**消息创建与首次投递入队必须在同一个事务里**（设计文档 §12）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Settings
from ..ports.clock import Clock, SystemClock
from ..ports.event_transport import Event, EventPublisher
from ..ports.repositories import UnitOfWork, UnitOfWorkFactory


@dataclass(slots=True)
class ServiceContext:
    """应用服务共享上下文。"""

    settings: Settings
    unit_of_work: UnitOfWorkFactory
    publisher: EventPublisher
    clock: Clock = field(default_factory=SystemClock)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("mcp_agent_mailbox")
    )
    #: 宿主注入通道（例如 DSH 的本地 HTTP 唤醒）。为 ``None`` 时"在线但不能注入"：
    #: 消息保持 queued 等目标自己取信，绝不谎报 delivered。
    waker: object | None = None

    def uow(self) -> UnitOfWork:
        """创建一个新的工作单元。"""
        return self.unit_of_work()

    def publish(self, event: Event) -> None:
        """发布实时事件。

        发布失败不能影响业务事务（事务已经提交）：这里捕获异常并记日志，
        因为事件只是"更快送达"的通道，权威事实已经落库。
        """
        try:
            self.publisher.publish(event)
        except Exception:  # noqa: BLE001 - 事件通道故障不得回滚已提交的业务
            self.logger.exception(
                "事件发布失败",
                extra={"event": "event.publish_failed", "context": {"type": event.type}},
            )

    def audit(
        self,
        uow: UnitOfWork,
        event_type: str,
        *,
        now,
        account_id: str | None = None,
        connection_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        delivery_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """写审计事件（与业务写入同一事务，保证要么都有要么都没有）。"""
        audit = getattr(uow, "audit", None)
        if audit is None:
            return
        audit.append(
            event_type=event_type,
            created_at=now,
            account_id=account_id,
            connection_id=connection_id,
            conversation_id=conversation_id,
            message_id=message_id,
            delivery_id=delivery_id,
            detail=detail or {},
        )
