"""消息与状态机。

设计文档 §7.3 明确要求：不得用单一 ``status`` 同时表达传输、阅读和处理。因此这里
有三个互相独立的状态维度：

    delivery    queued -> dispatched -> delivered | failed | dead_letter
    visibility  unread -> seen
    processing  pending -> running -> completed | blocked | cancelled | failed

三者的语义边界必须守住：

    delivered   表示**宿主已可靠接收**，不表示模型已阅读或完成任务；
    seen        只表示调用者更新了可见性，不表示已处理；
    completed   是处理回执，不能替代回复；
    回复        是一条带 ``reply_to`` 的**新消息**，不是状态变更。
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from .errors import InvalidTransitionError, ValidationError

__all__ = [
    "MAX_MESSAGE_CHARS",
    "DeliveryRecord",
    "DeliveryState",
    "Message",
    "MessageView",
    "ProcessingRecord",
    "ProcessingState",
    "VisibilityState",
    "content_hash",
    "normalize_text",
]

#: 单条消息正文上限。超长必须明确拒绝，不截断（沿用旧版 4000 字的既有约定）。
MAX_MESSAGE_CHARS = 4000


class DeliveryState(enum.Enum):
    """投递（传输）状态。"""

    QUEUED = "queued"
    """已入队，尚未派发。目标离线时停在这里。"""

    DISPATCHED = "dispatched"
    """已把事件交给目标连接，等待适配器确认。"""

    DELIVERED = "delivered"
    """适配器确认宿主已持久化接收。**不代表任务完成**。"""

    FAILED = "failed"
    """本次尝试失败，仍有重试机会。"""

    DEAD_LETTER = "dead_letter"
    """超过最大尝试次数或不可重试，转入死信等待人工处理。"""

    @property
    def is_terminal(self) -> bool:
        return self in (DeliveryState.DELIVERED, DeliveryState.DEAD_LETTER)

    @property
    def needs_dispatch(self) -> bool:
        return self in (DeliveryState.QUEUED, DeliveryState.FAILED)


class VisibilityState(enum.Enum):
    """可见性状态。"""

    UNREAD = "unread"
    SEEN = "seen"


class ProcessingState(enum.Enum):
    """处理状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ProcessingState.COMPLETED,
            ProcessingState.CANCELLED,
            ProcessingState.FAILED,
        )


class ContentType(enum.Enum):
    """第一版只支持纯文本与 Markdown 文本。

    正文始终是**数据**，不是系统指令；注入宿主时必须附结构化来源信封。
    """

    TEXT = "text/plain"
    MARKDOWN = "text/markdown"


# ---------------------------------------------------------------------------
# 合法状态迁移表（设计文档 §8：状态迁移必须校验）
# ---------------------------------------------------------------------------

_ALLOWED_DELIVERY: dict[DeliveryState, frozenset[DeliveryState]] = {
    # queued 也能直接进 failed：适配器/宿主可能在"还没派发"时就报告失败
    # （例如明确拒绝注入）。注意"目标离线"不是失败——那仍然是 queued。
    #
    # 但 queued **不能**直接进 delivered：没把消息交给宿主就说"已送达"，
    # 正是我们要防的谎报。宿主确认（acknowledge）只对已派发的投递生效。
    DeliveryState.QUEUED: frozenset(
        {DeliveryState.DISPATCHED, DeliveryState.FAILED, DeliveryState.DEAD_LETTER}
    ),
    # dispatched 可以直接进死信：适配器报告"不可重试"时不必先绕一圈 failed。
    DeliveryState.DISPATCHED: frozenset(
        {
            DeliveryState.DELIVERED,
            DeliveryState.FAILED,
            DeliveryState.QUEUED,
            DeliveryState.DEAD_LETTER,
        }
    ),
    DeliveryState.FAILED: frozenset(
        {
            # 重试成功：失败过的投递再次派发并被确认是正常路径。
            # 少了这一条，任何"先失败后成功"的投递都会卡在 failed。
            DeliveryState.DELIVERED,
            DeliveryState.QUEUED,
            DeliveryState.DEAD_LETTER,
            # 幂等：适配器重复报告同一个失败不应报错。
            DeliveryState.FAILED,
        }
    ),
    DeliveryState.DELIVERED: frozenset(),
    DeliveryState.DEAD_LETTER: frozenset(),
}

_ALLOWED_PROCESSING: dict[ProcessingState, frozenset[ProcessingState]] = {
    # 允许 pending 一步直达终态或 blocked：适配器可能直接回传"已完成/已取消/失败/
    # 受阻"，中间没有经过 running，强行要求中间态只会逼调用方伪造状态。
    ProcessingState.PENDING: frozenset(
        {
            ProcessingState.RUNNING,
            ProcessingState.COMPLETED,
            ProcessingState.BLOCKED,
            ProcessingState.CANCELLED,
            ProcessingState.FAILED,
        }
    ),
    ProcessingState.RUNNING: frozenset(
        {
            ProcessingState.COMPLETED,
            ProcessingState.BLOCKED,
            ProcessingState.CANCELLED,
            ProcessingState.FAILED,
        }
    ),
    ProcessingState.BLOCKED: frozenset(
        {ProcessingState.RUNNING, ProcessingState.COMPLETED, ProcessingState.CANCELLED, ProcessingState.FAILED}
    ),
    # 终态不可回退：completed 默认不能退回 running。
    ProcessingState.COMPLETED: frozenset(),
    ProcessingState.CANCELLED: frozenset(),
    ProcessingState.FAILED: frozenset(),
}

_ALLOWED_VISIBILITY: dict[VisibilityState, frozenset[VisibilityState]] = {
    VisibilityState.UNREAD: frozenset({VisibilityState.SEEN}),
    # seen 可以退回 unread（例如标为未读），不涉及安全边界。
    VisibilityState.SEEN: frozenset({VisibilityState.UNREAD}),
}


def _check_transition(kind: str, table, current, target) -> None:
    if current is target:
        return
    allowed = table.get(current, frozenset())
    if target not in allowed:
        raise InvalidTransitionError(
            f"{kind} 状态不允许从 {current.value} 迁移到 {target.value}"
        )


def check_delivery_transition(current: DeliveryState, target: DeliveryState) -> None:
    """校验投递状态迁移，非法即抛 ``InvalidTransitionError``。"""
    _check_transition("投递", _ALLOWED_DELIVERY, current, target)


def check_processing_transition(current: ProcessingState, target: ProcessingState) -> None:
    """校验处理状态迁移，非法即抛 ``InvalidTransitionError``。"""
    _check_transition("处理", _ALLOWED_PROCESSING, current, target)


def check_visibility_transition(current: VisibilityState, target: VisibilityState) -> None:
    """校验可见性状态迁移，非法即抛 ``InvalidTransitionError``。"""
    _check_transition("可见性", _ALLOWED_VISIBILITY, current, target)


# ---------------------------------------------------------------------------
# 正文处理
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """校验并规范化正文。

    规则：
        - 必须是字符串（不接受 ``None`` 或字节）；
        - 去除首尾空白后不能为空；
        - 超过 ``MAX_MESSAGE_CHARS`` 明确拒绝，**不截断**——截断会静默改变语义；
        - 统一换行符，避免同一内容在不同平台哈希不同。
    """
    if not isinstance(text, str):
        raise ValidationError("消息正文必须是文本")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValidationError("消息正文不能为空")
    if len(normalized) > MAX_MESSAGE_CHARS:
        raise ValidationError(
            f"消息正文超过上限 {MAX_MESSAGE_CHARS} 字（实际 {len(normalized)} 字）；"
            "请改用文档引用，内容不会被截断"
        )
    return normalized


def content_hash(text: str) -> str:
    """正文哈希，用于循环检测（相同内容反复出现时暂停自动唤醒）。

    先做规范化再哈希，因此 ``"a\\r\\nb"`` 与 ``"a\\nb"`` 得到同一个值——不同平台的
    换行差异不应该让"同一句话"看起来像两句，否则循环检测会被绕过。
    这个函数对输入宽松（不校验长度、不去空白），因为它也会被用来哈希历史数据。
    """
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Message:
    """一条持久化消息。"""

    message_id: str
    conversation_id: str
    sender_account_id: str
    recipient_account_id: str
    content: str
    created_at: datetime
    content_type: ContentType = ContentType.TEXT
    reply_to: str | None = None
    idempotency_key: str | None = None
    content_hash: str = ""
    #: 是否由自动唤醒产生（用于自动互聊轮数统计与硬限制）。
    auto_generated: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sender_account_id == self.recipient_account_id:
            raise ValidationError("不能给自己发消息")
        if not self.content_hash:
            self.content_hash = content_hash(self.content)

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "reply_to": self.reply_to,
            "sender_account_id": self.sender_account_id,
            "recipient_account_id": self.recipient_account_id,
            "content_type": self.content_type.value,
            "content": self.content,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
            "auto_generated": self.auto_generated,
        }


@dataclass(slots=True)
class DeliveryRecord:
    """一条消息对某个收件账号的一次投递。

    重试必须复用同一个 ``delivery_id``：接收方按它去重，因此它是至少一次语义的
    幂等键（设计文档 §12）。
    """

    delivery_id: str
    message_id: str
    account_id: str
    conversation_id: str
    state: DeliveryState
    created_at: datetime
    updated_at: datetime
    attempt: int = 0
    #: 正文哈希（冗余自消息）：循环检测要按"对话 + 内容"查重复，一次查完比回表快。
    content_hash: str = ""
    acked_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    injected_at: datetime | None = None
    #: 最近一次"仅通知"（Level 1 通道）的时间：通知过不等于送达过。
    notified_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "message_id": self.message_id,
            "account_id": self.account_id,
            "conversation_id": self.conversation_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "notified_at": self.notified_at.isoformat() if self.notified_at else None,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class ProcessingRecord:
    """某账号对某条消息的处理状态。"""

    message_id: str
    account_id: str
    state: ProcessingState
    updated_at: datetime
    result: str | None = None
    block_reason: str | None = None
    attempt_count: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "account_id": self.account_id,
            "state": self.state.value,
            "result": self.result,
            "block_reason": self.block_reason,
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(slots=True)
class MessageView:
    """读接口返回的复合视图：消息 + 三个状态维度。

    分开返回三个状态而不是拼成一个字符串，是为了让调用方（尤其是模型）明确知道
    "已送达"不等于"已完成"。
    """

    message: Message
    delivery: DeliveryState | None = None
    visibility: VisibilityState = VisibilityState.UNREAD
    processing: ProcessingState = ProcessingState.PENDING
    processing_result: str | None = None

    def to_dict(self, *, include_content: bool = True) -> dict[str, object]:
        payload = self.message.to_dict()
        if not include_content:
            payload.pop("content", None)
        payload.update(
            {
                "delivery": self.delivery.value if self.delivery else None,
                "visibility": self.visibility.value,
                "processing": self.processing.value,
                "processing_result": self.processing_result,
                "is_reply": self.message.reply_to is not None,
            }
        )
        return payload
