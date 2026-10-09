"""仓储与工作单元端口。

事务边界的唯一负责人是 ``UnitOfWork``：应用服务用 ``with uow.transaction():`` 包住
一次用例，仓储方法在该事务内执行。**消息创建与首次投递入队必须在同一事务里**
（设计文档 §12），这一点靠调用方持有同一个 uow 保证，而不是靠仓储内部各自提交。

这里同时给出只读与可写两套仓储协议：读接口在只读事务里暴露给查询路径，
写接口只在写事务里可见。类型系统因此能挡掉"在只读路径上偷偷写库"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.accounts import Account, Connection, ConnectionState, HostIdentity
from ..domain.conversations import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    ParticipantRole,
)
from ..domain.messages import (
    DeliveryRecord,
    DeliveryState,
    Message,
    MessageView,
    ProcessingRecord,
    ProcessingState,
    VisibilityState,
)
from ..domain.presence import PresenceState

__all__ = [
    "AccountReader",
    "AccountWriter",
    "ConnectionReader",
    "ConnectionWriter",
    "ConversationReader",
    "ConversationWriter",
    "DeliveryReader",
    "DeliveryWriter",
    "MessagePage",
    "MessageReader",
    "MessageWriter",
    "ProcessingReader",
    "ProcessingWriter",
    "UnitOfWork",
    "UnitOfWorkFactory",
]

# ---------------------------------------------------------------------------
# 查询辅助类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessagePage:
    """一页消息。``next_cursor`` 为 ``None`` 表示已到末尾。"""

    items: tuple[MessageView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """一页对话（含未读数与最近一条消息摘要）。"""

    items: tuple["ConversationSummary", ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """对话列表项：投影，可由消息表重建。"""

    conversation: Conversation
    counterpart_account_id: str
    unread_count: int
    last_message_id: str | None
    last_message_at: datetime | None


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


@runtime_checkable
class AccountReader(Protocol):
    def get(self, account_id: str) -> Account | None:
        ...

    def find_by_identity(self, identity: HostIdentity) -> Account | None:
        ...

    def list_accounts(
        self,
        *,
        host_type: str | None = None,
        presence: PresenceState | None = None,
        exclude_account_id: str | None = None,
        limit: int = 200,
    ) -> list[Account]:
        ...


@runtime_checkable
class AccountWriter(Protocol):
    def add(self, account: Account) -> None:
        ...

    def update(self, account: Account) -> None:
        ...

    def next_generation(self, account_id: str) -> int:
        """返回下一个连接代次（账号级单调递增）。"""
        ...


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


@runtime_checkable
class ConnectionReader(Protocol):
    def get(self, connection_id: str) -> Connection | None:
        ...

    def current_for_account(self, account_id: str) -> Connection | None:
        """账号当前的主连接；没有则为 ``None``。"""
        ...

    def list_for_account(self, account_id: str, *, include_closed: bool = False) -> list[Connection]:
        ...


@runtime_checkable
class ConnectionWriter(Protocol):
    def add(self, connection: Connection) -> None:
        ...

    def update(self, connection: Connection) -> None:
        ...

    def demote_current(self, account_id: str, *, state: ConnectionState, at: datetime) -> int:
        """把账号当前主连接降级（换新连接或回收时调用），返回受影响行数。"""
        ...

    def max_generation(self, account_id: str) -> int:
        """账号已用过的最大连接代次；新连接取 ``max_generation + 1``。"""
        ...

    def mark_disconnected(self) -> list[str]:
        """把"托管进程已退出/无进程信息"的活动连接标记为过期，返回被影响的连接 ID。"""
        ...


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


@runtime_checkable
class ConversationReader(Protocol):
    def get(self, conversation_id: str) -> Conversation | None:
        ...

    def find_open_direct(self, participant_key: str) -> Conversation | None:
        ...

    def participants(self, conversation_id: str) -> list[ConversationParticipant]:
        ...

    def is_participant(self, conversation_id: str, account_id: str) -> bool:
        ...

    def list_for_account(
        self,
        account_id: str,
        *,
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[ConversationSummary], str | None]:
        ...


@runtime_checkable
class ConversationWriter(Protocol):
    def add(self, conversation: Conversation) -> None:
        ...

    def update(self, conversation: Conversation) -> None:
        ...

    def add_participant(self, participant: ConversationParticipant) -> None:
        ...

    def update_participant(self, participant: ConversationParticipant) -> None:
        ...

    def mark_read(
        self,
        conversation_id: str,
        account_id: str,
        *,
        last_read_message_id: str | None,
        unread_delta: int,
    ) -> None:
        ...

    def bump_unread(self, conversation_id: str, account_ids: list[str], delta: int = 1) -> None:
        ...


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@runtime_checkable
class MessageReader(Protocol):
    def get(self, message_id: str) -> Message | None:
        ...

    def find_by_idempotency_key(
        self, *, sender_account_id: str, idempotency_key: str
    ) -> Message | None:
        ...

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 50,
        viewer_account_id: str | None = None,
    ) -> MessagePage:
        ...

    def count_auto_turns(self, conversation_id: str, *, since: datetime) -> int:
        ...

    def list_pending_for_account(
        self, account_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> MessagePage:
        """未完成的收件，阅读与处理状态彼此独立。"""
        ...

    def pending_count(self, account_id: str) -> int:
        ...

    def unread_count(self, conversation_id: str, account_id: str) -> int:
        ...


@runtime_checkable
class MessageWriter(Protocol):
    def add(self, message: Message) -> None:
        ...

    def recent_content_hashes(
        self, conversation_id: str, *, limit: int = 10
    ) -> list[str]:
        ...


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


@runtime_checkable
class DeliveryReader(Protocol):
    def get(self, delivery_id: str) -> DeliveryRecord | None:
        ...

    def find_for_message(self, message_id: str, account_id: str) -> DeliveryRecord | None:
        ...

    def due_for_dispatch(
        self, *, now: datetime, limit: int = 100
    ) -> list[DeliveryRecord]:
        """到期可派发的投递（含重试退避已到的 FAILED）。"""
        ...

    def pending_for_account(self, account_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        ...

    def stalled_dispatched(self, *, before: datetime, limit: int = 100) -> list[DeliveryRecord]:
        """已派发但长时间未确认的投递（连接在确认前断开时靠它补投）。"""
        ...

    def count_recent_content(
        self, *, conversation_id: str, content_hash: str, since: datetime
    ) -> int:
        """同一对话在时间窗内出现过几次相同正文（循环检测）。"""
        ...


@runtime_checkable
class DeliveryWriter(Protocol):
    def defer_offline(self, delivery_id: str, *, at: datetime, until: datetime) -> None:
        ...

    def resume_offline(self, account_id: str, *, at: datetime) -> None:
        ...

    def add(self, delivery: DeliveryRecord) -> None:
        ...

    def update(self, delivery: DeliveryRecord) -> None:
        ...

    def claim_for_dispatch(self, delivery_id: str, *, at: datetime) -> bool:
        """原子地把 queued/failed 置为 dispatched 并 attempt+1。

        返回 ``False`` 表示别的派发者抢先了，调用方必须放弃本次派发。这是
        "至少一次"里防止重复派发的关键一步。
        """
        ...


# ---------------------------------------------------------------------------
# 处理状态
# ---------------------------------------------------------------------------


@runtime_checkable
class ProcessingReader(Protocol):
    def get(self, message_id: str, account_id: str) -> ProcessingRecord | None:
        ...


@runtime_checkable
class ProcessingWriter(Protocol):
    def upsert(self, record: ProcessingRecord) -> None:
        ...


# ---------------------------------------------------------------------------
# 工作单元
# ---------------------------------------------------------------------------


@runtime_checkable
class UnitOfWork(Protocol):
    """一次事务范围内的仓储集合。

    用法::

        with uow.transaction():
            uow.messages.add(message)
            uow.deliveries.add(delivery)

    退出 ``transaction()`` 时提交；异常时回滚并把异常继续抛出。
    """

    accounts: AccountReader
    connections: ConnectionReader
    conversations: ConversationReader
    messages: MessageReader
    deliveries: DeliveryReader
    processing: ProcessingReader

    def transaction(self) -> "TransactionScope":
        ...


@runtime_checkable
class TransactionScope(Protocol):
    """事务上下文，可作上下文管理器。"""

    def __enter__(self) -> "UnitOfWork":
        ...

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    """按需创建工作单元（Broker 持有，每请求一个）。"""

    def __call__(self) -> UnitOfWork:
        ...
