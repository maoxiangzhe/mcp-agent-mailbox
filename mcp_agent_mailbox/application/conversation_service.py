"""会话与消息服务：一对一对话、幂等发送、权限校验、循环与速率限制。

这一层是**权限与安全模型**的执行点（设计文档 §8、§10、§11）：

- 所有写操作都从 ``connection_id`` 解析发送者；发送接口**没有** ``from`` / ``agent``
  参数，因此模型无法代表别的会话发信、确认或改状态；
- 账号只能访问自己参与的对话；
- 回复是一条带 ``reply_to`` 的新消息；回复收件成功时同时确认原消息已处理；
- 自动互聊有硬上限：达到上限后**消息仍然保留**，但对话被标记阻塞、不再唤醒；
- 速率限制与循环检测都在同一事务里记账，避免并发绕过。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from ..domain.accounts import Account
from ..domain.conversations import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    ParticipantRole,
    participant_key,
)
from ..domain.errors import (
    ConversationClosedError,
    LoopLimitExceededError,
    NotAParticipantError,
    NotFoundError,
    RateLimitedError,
    RecipientBlockedError,
    ValidationError,
)
from ..domain.ids import new_conversation_id, new_message_id
from ..domain.messages import (
    MAX_MESSAGE_CHARS,
    ContentType,
    Message,
    MessageView,
    ProcessingRecord,
    ProcessingState,
    VisibilityState,
    check_processing_transition,
    content_hash,
    normalize_text,
)
from ..domain.timestamps import format_timestamp, plus_seconds
from ..ports.event_transport import Event
from .account_service import AccountService
from .context import ServiceContext
from .delivery_service import DeliveryService
from .presence_service import PresenceService

__all__ = [
    "ContactView",
    "ConversationService",
    "ConversationView",
    "MessagePageView",
    "SendResult",
]


@dataclass(frozen=True, slots=True)
class SendResult:
    """一次发送的结果。

    刻意区分三件事，避免"投递成功"被读成"任务完成"：
        ``delivery_state``   传输状态：queued 表示目标离线，消息已持久化待投；
        ``duplicate``        本次调用命中幂等键，返回的是已有消息；
        ``wake_suppressed``  因循环上限或全局暂停而没有请求唤醒对端。
    """

    message: Message
    conversation_id: str
    delivery_id: str
    delivery_state: str
    recipient_presence: str
    duplicate: bool = False
    wait_for_reply: bool = False
    wake_suppressed: bool = False
    suppression_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "message_id": self.message.message_id,
            "conversation_id": self.conversation_id,
            "reply_to": self.message.reply_to,
            "delivery_id": self.delivery_id,
            "delivery_state": self.delivery_state,
            "recipient_presence": self.recipient_presence,
            "created_at": self.message.created_at.isoformat(),
            "duplicate": self.duplicate,
            "wait_for_reply": self.wait_for_reply,
            "wake_suppressed": self.wake_suppressed,
            "note": (
                "消息已持久化并进入投递队列。delivery=delivered 只表示宿主已接收，"
                "不代表任务完成；回复是带 reply_to 的新消息。"
            ),
        }
        if self.suppression_reason:
            payload["suppression_reason"] = self.suppression_reason
        return payload


@dataclass(frozen=True, slots=True)
class ConversationView:
    """对话列表项。"""

    conversation_id: str
    counterpart_account_id: str
    counterpart_display_name: str
    presence: str
    unread_count: int
    last_message_id: str | None
    last_message_at: datetime | None
    blocked: bool
    blocked_reason: str | None
    auto_turn_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "counterpart_account_id": self.counterpart_account_id,
            "counterpart_display_name": self.counterpart_display_name,
            "counterpart_presence": self.presence,
            "unread_count": self.unread_count,
            "last_message_id": self.last_message_id,
            "last_message_at": (
                self.last_message_at.isoformat() if self.last_message_at else None
            ),
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "auto_turn_count": self.auto_turn_count,
        }


@dataclass(frozen=True, slots=True)
class ContactView:
    """联系人列表项。"""

    account_id: str
    display_name: str
    address: str
    host_type: str
    presence: str
    capability_level: int
    capability_slug: str
    reachable: bool
    #: 能不能"直接投进去让它开工"（在线 + 适配器支持注入）。
    can_wake: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "address": self.address,
            "host_type": self.host_type,
            "presence": self.presence,
            "online": self.presence == "connected",
            "capability_level": self.capability_level,
            "capability_slug": self.capability_slug,
            "reachable_now": self.reachable,
            "can_wake": self.can_wake,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MessagePageView:
    """一页消息。"""

    conversation_id: str
    messages: tuple[MessageView, ...]
    next_cursor: str | None
    unread_count: int

    def to_dict(self, *, include_content: bool = True) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "messages": [m.to_dict(include_content=include_content) for m in self.messages],
            "next_cursor": self.next_cursor,
            "unread_count": self.unread_count,
        }


class ConversationService:
    """对话与消息用例。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._settings = context.settings
        self._clock = context.clock
        self._accounts = AccountService(context)
        self._deliveries = DeliveryService(context)
        self._presence = PresenceService(context)

    # -- 工具入口 ---------------------------------------------------------

    def resolve_caller(self, uow, connection_id: str) -> Account:
        """由连接上下文解析调用者账号。所有写操作的唯一身份来源。"""
        return self._accounts.account_for_connection(uow, connection_id)

    def open_conversation(
        self,
        *,
        connection_id: str,
        to_account_id: str,
    ) -> ConversationView:
        """直接打开与某个账号的一对一对话（没有就创建，有就复用）。

        **不发送任何消息**：这是"我要打开和某人的对话看一眼"的入口，用在
        ``read_conversation`` / ``send_message`` 之前拿 ``conversation_id``。
        幂等：同一对账号只会得到同一个对话。
        """
        _validate_account_id(to_account_id)
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            if caller.account_id == to_account_id:
                raise ValidationError("不能和自己建立对话")
            recipient = self._require_account(uow, to_account_id)
            self._accounts.assert_contact_allowed(uow, caller.account_id, recipient.account_id)
            key = participant_key(caller.account_id, recipient.account_id)
            conversation = uow.conversations.find_open_direct(key)
            created = conversation is None
            if conversation is None:
                conversation = Conversation(
                    conversation_id=new_conversation_id(),
                    kind=ConversationKind.DIRECT,
                    participant_key=key,
                    created_by=caller.account_id,
                    created_at=now,
                    updated_at=now,
                )
                uow.conversations.add(conversation)
                uow.conversations.add_participant(
                    ConversationParticipant(
                        conversation_id=conversation.conversation_id,
                        account_id=caller.account_id,
                        role=ParticipantRole.INITIATOR,
                        joined_at=now,
                    )
                )
                uow.conversations.add_participant(
                    ConversationParticipant(
                        conversation_id=conversation.conversation_id,
                        account_id=recipient.account_id,
                        role=ParticipantRole.PEER,
                        joined_at=now,
                    )
                )
                self._context.audit(
                    uow,
                    "conversation.created",
                    now=now,
                    account_id=caller.account_id,
                    conversation_id=conversation.conversation_id,
                    detail={"counterpart": recipient.account_id, "kind": "direct", "empty": True},
                )
            unread = uow.messages.unread_count(conversation.conversation_id, caller.account_id)
            message_count = len(
                uow.messages.list_for_conversation(conversation.conversation_id, limit=200).items
            )
        presence = self._presence.for_account(recipient.account_id)
        if created:
            self._context.publish(
                Event(
                    type="conversation/created",
                    account_id=caller.account_id,
                    created_at=now,
                    payload={
                        "conversation_id": conversation.conversation_id,
                        "counterpart_account_id": recipient.account_id,
                    },
                )
            )
        return ConversationView(
            conversation_id=conversation.conversation_id,
            counterpart_account_id=recipient.account_id,
            counterpart_display_name=recipient.display_name,
            presence=presence.state.value,
            unread_count=unread,
            last_message_id=None,
            last_message_at=conversation.last_message_at,
            blocked=conversation.blocked,
            blocked_reason=conversation.blocked_reason,
            auto_turn_count=conversation.auto_turn_count,
        )

    def start_conversation(
        self,
        *,
        connection_id: str,
        to_account_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> SendResult:
        """创建（或复用）一对一对话并发送首条消息。"""
        body = normalize_text(text)
        _validate_account_id(to_account_id)
        now = self._clock.now()

        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            if caller.account_id == to_account_id:
                raise ValidationError("不能和自己建立对话")
            recipient = self._require_account(uow, to_account_id)
            key = participant_key(caller.account_id, recipient.account_id)
            self._accounts.assert_contact_allowed(uow, caller.account_id, recipient.account_id)
            existing = uow.conversations.find_open_direct(key)
            created = existing is None
            if existing is None:
                conversation = Conversation(
                    conversation_id=new_conversation_id(),
                    kind=ConversationKind.DIRECT,
                    participant_key=key,
                    created_by=caller.account_id,
                    created_at=now,
                    updated_at=now,
                )
                uow.conversations.add(conversation)
                uow.conversations.add_participant(
                    ConversationParticipant(
                        conversation_id=conversation.conversation_id,
                        account_id=caller.account_id,
                        role=ParticipantRole.INITIATOR,
                        joined_at=now,
                    )
                )
                uow.conversations.add_participant(
                    ConversationParticipant(
                        conversation_id=conversation.conversation_id,
                        account_id=recipient.account_id,
                        role=ParticipantRole.PEER,
                        joined_at=now,
                    )
                )
                self._context.audit(
                    uow,
                    "conversation.created",
                    now=now,
                    account_id=caller.account_id,
                    conversation_id=conversation.conversation_id,
                    detail={"counterpart": recipient.account_id, "kind": "direct"},
                )
            else:
                conversation = existing

            result, deferred = self._append_message(
                uow,
                conversation=conversation,
                sender=caller,
                recipient=recipient,
                body=body,
                reply_to=None,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
                now=now,
            )
            conversation_id = conversation.conversation_id
            conversation_created = created
        if deferred is not None:
            raise deferred

        if conversation_created:
            self._context.publish(
                Event(
                    type="conversation/created",
                    account_id=caller.account_id,
                    created_at=now,
                    payload={
                        "conversation_id": conversation_id,
                        "counterpart_account_id": recipient.account_id,
                    },
                )
            )
        assert result is not None
        return result

    def send_message(
        self,
        *,
        connection_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> SendResult:
        """向已参与的对话发送新消息。"""
        body = normalize_text(text)
        now = self._clock.now()

        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            conversation = self._require_participation(uow, conversation_id, caller.account_id)
            if conversation.closed_at is not None:
                raise ConversationClosedError("对话已关闭，无法发送新消息")
            if conversation.blocked:
                raise ConversationClosedError(
                    f"对话已被阻塞（{conversation.blocked_reason or '原因未记录'}），"
                    "消息未发送；请人工确认后恢复"
                )
            recipient_id = self._counterpart(uow, conversation.conversation_id, caller.account_id)
            recipient = self._require_account(uow, recipient_id)
            self._accounts.assert_contact_allowed(uow, caller.account_id, recipient.account_id)
            result = self._append_message(
                uow,
                conversation=conversation,
                sender=caller,
                recipient=recipient,
                body=body,
                reply_to=None,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
                now=now,
            )
        if result[1] is not None:
            raise result[1]
        assert result[0] is not None
        return result[0]

    def reply_message(
        self,
        *,
        connection_id: str,
        message_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> SendResult:
        """回复指定消息。

        权限与一致性由这里保证：调用者必须是该消息所属对话的参与者，且回复方向
        必须是"发给原发送者"，因此模型无法借回复把消息转给第三方。
        """
        body = normalize_text(text)
        now = self._clock.now()

        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            original = uow.messages.get(message_id)
            if original is None:
                raise NotFoundError(f"被回复的消息不存在：{message_id}")
            conversation = self._require_participation(
                uow, original.conversation_id, caller.account_id
            )
            if conversation.closed_at is not None:
                raise ConversationClosedError("对话已关闭，无法回复")
            if conversation.blocked:
                raise ConversationClosedError(
                    f"对话已被阻塞（{conversation.blocked_reason or '原因未记录'}），消息未发送"
                )
            # 回复的收件人固定为原消息的发送者（如果原消息是自己发的，则发给原收件人）。
            if original.sender_account_id == caller.account_id:
                recipient_id = original.recipient_account_id
            else:
                recipient_id = original.sender_account_id
            if recipient_id == caller.account_id:
                raise ValidationError("不能回复给自己")
            recipient = self._require_account(uow, recipient_id)
            self._accounts.assert_contact_allowed(uow, caller.account_id, recipient.account_id)
            # 回复自动生成的消息 => 继续自动往返；回复人工消息 => 人工接手，计数清零。
            continuing_loop = bool(original.auto_generated)
            result = self._append_message(
                uow,
                conversation=conversation,
                sender=caller,
                recipient=recipient,
                body=body,
                reply_to=original.message_id,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
                now=now,
                continuing_loop=continuing_loop,
            )
            # 只有发送成功才能确认原收件；与回复持久化共用事务，失败不会丢收件。
            if result[0] is not None and original.recipient_account_id == caller.account_id:
                previous = uow.processing.get(original.message_id, caller.account_id)
                if previous is None or not previous.state.is_terminal:
                    self._set_processing(
                        uow, message=original, account_id=caller.account_id,
                        target_state=ProcessingState.COMPLETED, result=None, now=now,
                    )
        if result[1] is not None:
            raise result[1]
        assert result[0] is not None
        return result[0]

    # -- 查询 -------------------------------------------------------------

    def list_conversations(
        self,
        *,
        connection_id: str,
        cursor: str | None = None,
        unread_only: bool = False,
        limit: int = 50,
    ) -> tuple[list[ConversationView], str | None]:
        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            summaries, next_cursor = uow.conversations.list_for_account(
                caller.account_id,
                unread_only=unread_only,
                cursor=cursor,
                limit=max(1, min(limit, 200)),
            )
            counterpart_ids = [
                s.counterpart_account_id for s in summaries if s.counterpart_account_id
            ]
            names = {}
            for account_id in counterpart_ids:
                account = uow.accounts.get(account_id)
                names[account_id] = account.display_name if account else account_id
        presences = self._presence.for_account_ids(counterpart_ids)
        views = [
            ConversationView(
                conversation_id=summary.conversation.conversation_id,
                counterpart_account_id=summary.counterpart_account_id,
                counterpart_display_name=names.get(
                    summary.counterpart_account_id, summary.counterpart_account_id
                ),
                presence=presences[summary.counterpart_account_id].state.value
                if summary.counterpart_account_id in presences
                else "offline",
                unread_count=summary.unread_count,
                last_message_id=summary.last_message_id,
                last_message_at=summary.last_message_at,
                blocked=summary.conversation.blocked,
                blocked_reason=summary.conversation.blocked_reason,
                auto_turn_count=summary.conversation.auto_turn_count,
            )
            for summary in summaries
        ]
        return views, next_cursor

    def read_conversation(
        self,
        *,
        connection_id: str,
        conversation_id: str,
        after_message_id: str | None = None,
        limit: int = 50,
        mark_seen: bool = True,
    ) -> MessagePageView:
        """读取对话消息；默认推进可见性并在同一事务里把未读数清零。"""
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            self._require_participation(uow, conversation_id, caller.account_id)
            page = uow.messages.list_for_conversation(
                conversation_id,
                after_message_id=after_message_id,
                limit=max(1, min(limit, 200)),
                viewer_account_id=caller.account_id,
            )
            unread = uow.messages.unread_count(conversation_id, caller.account_id)
            seen_count = 0
            if mark_seen and page.items:
                last_id = page.items[-1].message.message_id
                seen_count = sum(
                    1
                    for view in page.items
                    if view.message.sender_account_id != caller.account_id
                    and view.visibility is VisibilityState.UNREAD
                )
                # 先落盘已读位点，再重算这一页的可见性：否则返回值里的
                # visibility 会停留在 mark_read 之前的旧状态，和库里的记录不一致。
                uow.conversations.mark_read(
                    conversation_id,
                    caller.account_id,
                    last_read_message_id=last_id,
                    unread_count=max(unread - seen_count, 0),
                )
                unread = max(unread - seen_count, 0)
                if seen_count:
                    self._context.audit(
                        uow,
                        "visibility.seen",
                        now=now,
                        account_id=caller.account_id,
                        conversation_id=conversation_id,
                        detail={"seen_count": seen_count, "last_read_message_id": last_id},
                    )
                page = uow.messages.list_for_conversation(
                    conversation_id,
                    after_message_id=after_message_id,
                    limit=max(1, min(limit, 200)),
                    viewer_account_id=caller.account_id,
                )
        return MessagePageView(
            conversation_id=conversation_id,
            messages=page.items,
            next_cursor=page.next_cursor,
            unread_count=unread,
        )

    def list_contacts(
        self,
        *,
        connection_id: str,
        status: str | None = None,
        host_type: str | None = None,
        limit: int = 200,
    ) -> list[ContactView]:
        """列出可联系的会话账号。

        ``status`` 过滤使用 presence 取值（``connected`` / ``offline``）：在线只看
        托管进程是否活着；``connected`` 表示可以立刻投递（对方进程在跑）。
        """
        from ..domain.presence import PresenceState

        caller = self._caller(connection_id)
        wanted: PresenceState | None = None
        if status:
            try:
                wanted = PresenceState(status.strip().lower())
            except ValueError as exc:
                raise ValidationError(
                    "status 只能是 connected / offline"
                ) from exc
        with self._context.uow().transaction() as uow:
            accounts = uow.accounts.list_accounts(
                host_type=host_type.strip().lower() if host_type else None,
                exclude_account_id=caller.account_id,
                limit=max(1, min(limit, 500)),
            )
            policies = {
                row["account_id"]: row
                for row in uow.contacts.list_policies(caller.account_id)
            }
        presences = self._presence.for_accounts(accounts)
        views: list[ContactView] = []
        for account in accounts:
            presence = presences[account.account_id]
            if wanted is not None and presence.state is not wanted:
                continue
            policy = policies.get(account.account_id)
            if policy is not None and policy["policy"] == "block":
                continue  # 被自己阻止的账号不出现在可联系列表里
            views.append(
                ContactView(
                    account_id=account.account_id,
                    display_name=account.display_name,
                    address=account.address,
                    host_type=account.host_type,
                    presence=presence.state.value,
                    capability_level=presence.capability_level,
                    capability_slug=presence.capability_slug,
                    reachable=presence.can_receive,
                    can_wake=presence.can_wake,
                    note=(str(policy["note"]) if policy and policy.get("note") else None),
                )
            )
        return views

    def set_message_status(
        self,
        *,
        connection_id: str,
        message_id: str,
        processing: str,
        result: str | None = None,
    ) -> ProcessingRecord:
        """更新处理状态。

        只有消息的**收件人**可以更新自己的处理状态；状态迁移必须合法（``completed``
        不能退回 ``running``）。这里不改变投递状态，也不等于回复。
        """
        target_state = _parse_processing(processing)
        if result is not None and len(result) > MAX_MESSAGE_CHARS:
            raise ValidationError(f"result 超过 {MAX_MESSAGE_CHARS} 字上限")
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            caller = self.resolve_caller(uow, connection_id)
            message = uow.messages.get(message_id)
            if message is None:
                raise NotFoundError(f"消息不存在：{message_id}")
            self._require_participation(uow, message.conversation_id, caller.account_id)
            if message.recipient_account_id != caller.account_id:
                raise NotAParticipantError(
                    "只有消息的收件人可以更新该消息的处理状态"
                )
            record, changed = self._set_processing(
                uow, message=message, account_id=caller.account_id,
                target_state=target_state, result=result, now=now,
            )
        if not changed:
            return record
        self._context.publish(
            Event(
                type="processing/changed",
                account_id=caller.account_id,
                created_at=now,
                payload={
                    "message_id": message_id,
                    "conversation_id": message.conversation_id,
                    "processing": target_state.value,
                },
            )
        )
        return record

    def _set_processing(
        self, uow, *, message: Message, account_id: str,
        target_state: ProcessingState, result: str | None, now: datetime,
    ) -> tuple[ProcessingRecord, bool]:
        previous = uow.processing.get(message.message_id, account_id)
        current_state = previous.state if previous else ProcessingState.PENDING
        check_processing_transition(current_state, target_state)
        # 重复回执既不增加尝试计数，也不擦掉已有摘要。
        if previous is not None and current_state is target_state:
            if result is None or previous.result == result:
                return previous, False
        record = ProcessingRecord(
            message_id=message.message_id, account_id=account_id, state=target_state,
            result=result,
            block_reason=result if target_state is ProcessingState.BLOCKED else None,
            attempt_count=(previous.attempt_count if previous else 0) + 1,
            updated_at=now,
        )
        uow.processing.upsert(record)
        self._context.audit(
            uow, "processing.changed", now=now, account_id=account_id,
            conversation_id=message.conversation_id, message_id=message.message_id,
            detail={"from": current_state.value, "to": target_state.value},
        )
        return record, True

    # -- 阻塞与恢复 -------------------------------------------------------

    def block_conversation(self, conversation_id: str, *, reason: str) -> bool:
        """标记对话为阻塞（达到自动互聊上限时调用）。

        只标记，不删消息：文档要求"超限后保留消息并记录原因"。
        """
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                raise NotFoundError(f"对话不存在：{conversation_id}")
            if conversation.blocked:
                return False
            conversation.blocked = True
            conversation.blocked_reason = reason
            conversation.updated_at = now
            uow.conversations.update(conversation)
            self._context.audit(
                uow,
                "conversation.blocked",
                now=now,
                conversation_id=conversation_id,
                detail={"reason": reason, "auto_turn_count": conversation.auto_turn_count},
            )
            participants = [
                p.account_id for p in uow.conversations.participants(conversation_id)
            ]
        for account_id in participants:
            self._context.publish(
                Event(
                    type="conversation/blocked",
                    account_id=account_id,
                    created_at=now,
                    payload={"conversation_id": conversation_id, "reason": reason},
                )
            )
        return True

    def unblock_conversation(self, conversation_id: str, *, reason: str = "manual_resume") -> bool:
        """人工恢复被阻塞的对话，并清零自动往返计数。"""
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                raise NotFoundError(f"对话不存在：{conversation_id}")
            if not conversation.blocked:
                return False
            conversation.blocked = False
            conversation.blocked_reason = None
            conversation.auto_turn_count = 0
            conversation.updated_at = now
            uow.conversations.update(conversation)
            self._context.audit(
                uow,
                "conversation.paused",
                now=now,
                conversation_id=conversation_id,
                detail={"reason": reason, "action": "unblocked"},
            )
        return True

    # -- 内部实现 ---------------------------------------------------------

    def _append_message(
        self,
        uow,
        *,
        conversation: Conversation,
        sender: Account,
        recipient: Account,
        body: str,
        reply_to: str | None,
        idempotency_key: str | None,
        wait_for_reply: bool,
        now: datetime,
        continuing_loop: bool = False,
    ) -> tuple[SendResult | None, LoopLimitExceededError | None]:
        """把一个消息用例的全部写入放进当前事务。

        返回 ``(结果, 延期错误)``：达上限时**不在此处抛异常**，因为 ``transaction()``
        一遇到异常就回滚，会把刚写下的"阻塞对话 + 原因"一起抹掉，违反"超限后保留
        消息并记录原因"。改为把错误交给调用方，在事务提交之后再抛。
        """
        try:
            result = self._append_message_inner(
                uow,
                conversation=conversation,
                sender=sender,
                recipient=recipient,
                body=body,
                reply_to=reply_to,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
                now=now,
                continuing_loop=continuing_loop,
            )
        except LoopLimitExceededError as exc:
            return None, exc
        return result, None

    def _append_message_inner(
        self,
        uow,
        *,
        conversation: Conversation,
        sender: Account,
        recipient: Account,
        body: str,
        reply_to: str | None,
        idempotency_key: str | None,
        wait_for_reply: bool,
        now: datetime,
        continuing_loop: bool = False,
    ) -> SendResult:
        """实际写入逻辑。顺序：幂等 -> 限流 -> 循环检测 -> 消息 -> 投递 -> 计数。"""
        # 1) 幂等：同一发送者 + 同一 idempotency_key 返回原消息，不重复发信。
        key = (idempotency_key or "").strip() or None
        if key is not None:
            existing = uow.messages.find_by_idempotency_key(
                sender_account_id=sender.account_id, idempotency_key=key
            )
            if existing is not None:
                if (
                    existing.conversation_id != conversation.conversation_id
                    or existing.recipient_account_id != recipient.account_id
                    or existing.reply_to != reply_to
                    or existing.content != body
                ):
                    raise ValidationError("idempotency_key 已用于另一条消息；请复用原参数或使用新键")
                self._context.audit(
                    uow,
                    "message.duplicate",
                    now=now,
                    account_id=sender.account_id,
                    conversation_id=existing.conversation_id,
                    message_id=existing.message_id,
                    detail={"idempotency_key": key},
                )
                delivery = uow.deliveries.find_for_message(
                    existing.message_id, existing.recipient_account_id
                )
                return SendResult(
                    message=existing,
                    conversation_id=existing.conversation_id,
                    delivery_id=delivery.delivery_id if delivery else "",
                    delivery_state=delivery.state.value if delivery else "unknown",
                    recipient_presence="unknown",
                    duplicate=True,
                    wait_for_reply=wait_for_reply,
                )

        # 2) 速率限制（账号级 + 对话级），在同一事务里记账。
        self._enforce_rate_limits(uow, conversation=conversation, sender=sender, now=now)

        # 3) 循环检测：同一内容反复出现即拒绝，并阻塞对话等人工介入。
        digit = content_hash(body)
        self._enforce_loop_limits(
            uow,
            conversation=conversation,
            sender=sender,
            body_hash=digit,
            continuing_loop=continuing_loop,
            now=now,
        )

        # 4) 写消息。
        message = Message(
            message_id=new_message_id(),
            conversation_id=conversation.conversation_id,
            sender_account_id=sender.account_id,
            recipient_account_id=recipient.account_id,
            content=body,
            content_type=ContentType.TEXT,
            reply_to=reply_to,
            idempotency_key=key,
            created_at=now,
            content_hash=digit,
            auto_generated=wait_for_reply,
            metadata={"expects_reply": "true" if wait_for_reply else "false"},
        )
        uow.messages.add(message)
        self._context.audit(
            uow,
            "reply.created" if reply_to else "message.created",
            now=now,
            account_id=sender.account_id,
            conversation_id=conversation.conversation_id,
            message_id=message.message_id,
            detail={
                "recipient_account_id": recipient.account_id,
                "reply_to": reply_to,
                "char_count": len(body),
            },
        )

        # 5) 首次投递入队——与消息写入同一事务，且带上正文哈希供循环检测使用。
        delivery = self._deliveries.enqueue(uow, message=message, now=now)

        # 6) 未读计数、对话时间戳、自动轮数。
        uow.conversations.bump_unread(conversation.conversation_id, [recipient.account_id], 1)
        conversation.last_message_at = now
        conversation.updated_at = now
        if continuing_loop or wait_for_reply:
            conversation.auto_turn_count = uow.conversations.bump_auto_turn(
                conversation.conversation_id, 1
            )
        elif reply_to is not None:
            # 人工回复：自动往返链被打断，计数清零。
            conversation.auto_turn_count = 0
        uow.conversations.update(conversation)

        presence = self._presence.for_account(recipient.account_id)
        return SendResult(
            message=message,
            conversation_id=conversation.conversation_id,
            delivery_id=delivery.delivery_id,
            delivery_state=delivery.state.value,
            recipient_presence=presence.state.value,
            duplicate=False,
            wait_for_reply=wait_for_reply,
        )

    def _enforce_rate_limits(self, uow, *, conversation: Conversation, sender: Account, now: datetime) -> None:
        limits = self._settings.rate_limits
        window = _window_start(now, self._settings.rate_limits.rate_window_seconds)
        sends = uow.rate_counters.increment(
            scope_key=f"send:{sender.account_id}", window_start=window, now=now
        )
        if sends > limits.max_sends_per_minute:
            self._context.audit(
                uow,
                "rate.limited",
                now=now,
                account_id=sender.account_id,
                conversation_id=conversation.conversation_id,
                detail={"scope": "account", "count": sends, "limit": limits.max_sends_per_minute},
            )
            raise RateLimitedError(
                f"账号 {sender.account_id} 每分钟最多发送 {limits.max_sends_per_minute} 条消息",
                retry_after_ms=limits.retry_after_ms,
            )
        conversation_sends = uow.rate_counters.increment(
            scope_key=f"conversation:{conversation.conversation_id}",
            window_start=window,
            now=now,
        )
        if conversation_sends > limits.max_messages_per_conversation_per_minute:
            self._context.audit(
                uow,
                "rate.limited",
                now=now,
                account_id=sender.account_id,
                conversation_id=conversation.conversation_id,
                detail={
                    "scope": "conversation",
                    "count": conversation_sends,
                    "limit": limits.max_messages_per_conversation_per_minute,
                },
            )
            raise RateLimitedError(
                f"对话 {conversation.conversation_id} 每分钟最多 "
                f"{limits.max_messages_per_conversation_per_minute} 条消息",
                retry_after_ms=limits.retry_after_ms,
            )

    def _enforce_loop_limits(
        self,
        uow,
        *,
        conversation: Conversation,
        sender: Account,
        body_hash: str,
        continuing_loop: bool,
        now: datetime,
    ) -> None:
        """自动互聊硬限制。

        两条独立防线：
            1. 连续自动往返次数达到上限 -> 消息仍被保留（由调用方决定），但对话阻塞；
            2. 同一正文在时间窗内重复出现 -> 判定为循环，同样阻塞。
        """
        limits = self._settings.rate_limits
        if conversation.auto_turn_count >= limits.max_auto_turns_per_conversation:
            self._block_within_transaction(
                uow,
                conversation,
                reason=(
                    f"自动互聊达到上限（{limits.max_auto_turns_per_conversation} 轮），"
                    "已停止自动唤醒，消息保留待人工确认"
                ),
                now=now,
                sender_id=sender.account_id,
            )
            raise LoopLimitExceededError(
                "自动互聊达到上限，对话已阻塞；消息未发送",
                auto_turns=conversation.auto_turn_count,
                limit=limits.max_auto_turns_per_conversation,
            )

        window_start = plus_seconds(now, -limits.repeated_content_window_seconds)
        repeats = uow.deliveries.count_recent_content(
            conversation_id=conversation.conversation_id,
            content_hash=body_hash,
            since=window_start,
        )
        if repeats >= limits.repeated_content_limit:
            self._context.audit(
                uow,
                "loop.detected",
                now=now,
                account_id=sender.account_id,
                conversation_id=conversation.conversation_id,
                detail={"repeats": repeats, "limit": limits.repeated_content_limit},
            )
            self._block_within_transaction(
                uow,
                conversation,
                reason=(
                    f"同一内容在 {limits.repeated_content_window_seconds} 秒内重复 "
                    f"{repeats} 次，判定为循环，已停止自动唤醒"
                ),
                now=now,
                sender_id=sender.account_id,
            )
            raise LoopLimitExceededError(
                "检测到重复内容循环，对话已阻塞；消息未发送",
                auto_turns=conversation.auto_turn_count,
                limit=limits.max_auto_turns_per_conversation,
            )

    def _block_within_transaction(self, uow, conversation: Conversation, *, reason: str, now: datetime, sender_id: str) -> None:
        conversation.blocked = True
        conversation.blocked_reason = reason
        conversation.updated_at = now
        uow.conversations.update(conversation)
        self._context.audit(
            uow,
            "conversation.blocked",
            now=now,
            account_id=sender_id,
            conversation_id=conversation.conversation_id,
            detail={"reason": reason, "auto_turn_count": conversation.auto_turn_count},
        )

    def _caller(self, connection_id: str) -> Account:
        """在独立事务里解析调用者（供不写库的查询路径使用）。"""
        with self._context.uow().transaction() as uow:
            return self.resolve_caller(uow, connection_id)

    def _require_account(self, uow, account_id: str) -> Account:
        account = uow.accounts.get(account_id)
        if account is None:
            raise NotFoundError(f"收件账号不存在：{account_id}")
        if account.blocked:
            raise ValidationError(f"收件账号已被停用：{account_id}")
        return account

    def _require_participation(self, uow, conversation_id: str, account_id: str) -> Conversation:
        conversation = uow.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"对话不存在：{conversation_id}")
        if not uow.conversations.is_participant(conversation_id, account_id):
            # 刻意不区分"不存在"和"无权访问"，避免泄露他人对话是否存在。
            raise NotAParticipantError("你不是该对话的参与者")
        return conversation

    def _counterpart(self, uow, conversation_id: str, account_id: str) -> str:
        for participant in uow.conversations.participants(conversation_id):
            if participant.account_id != account_id:
                return participant.account_id
        raise ValidationError("对话没有其他参与者，无法发送")

    @staticmethod
    def _account_stub(account_id: str) -> None:  # pragma: no cover
        """已废弃：批量 presence 现在按账号 ID 直接查询（``for_account_ids``）。

        保留这个方法只为给出明确的迁移提示，避免旧调用静默走到错误路径。
        """
        raise NotImplementedError(
            "批量 presence 请使用 PresenceService.for_account_ids（不再需要占位账号对象）"
        )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _validate_account_id(account_id: str) -> None:
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValidationError("目标账号不能为空")
    if any(ch.isspace() for ch in account_id):
        raise ValidationError("目标账号不能包含空白字符")


def _parse_processing(value: str) -> ProcessingState:
    try:
        return ProcessingState(value.strip().lower())
    except (ValueError, AttributeError) as exc:
        allowed = " / ".join(state.value for state in ProcessingState)
        raise ValidationError(f"processing 只能是：{allowed}") from exc


def _window_start(now: datetime, window_seconds: int) -> datetime:
    """固定窗口起点（按 Unix 秒对齐）。

    固定窗口实现简单、可解释；对"防止两个智能体互相刷屏"这个目标足够，不必上
    滑动窗口日志。
    """
    seconds = int(now.timestamp())
    aligned = seconds - (seconds % max(window_seconds, 1))
    return datetime.fromtimestamp(aligned, tz=now.tzinfo)
