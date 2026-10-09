"""对话领域模型。

第一版只实现一对一直接对话（设计文档 §7.1）。群聊被有意排除：它会同时引入发言
顺序、成员变更和广播风暴三个问题，不适合和可靠投递一起首发。

并发写保护放在仓储层用数据库唯一约束表达（同一对账号最多一个未关闭的 direct 对话），
领域层只提供规范化的 ``participant_key`` 供其使用。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from .errors import ValidationError

__all__ = [
    "Conversation",
    "ConversationKind",
    "ConversationParticipant",
    "ParticipantRole",
    "participant_key",
]


class ConversationKind(enum.Enum):
    """对话类型。第一版只有 DIRECT。"""

    DIRECT = "direct"


class ParticipantRole(enum.Enum):
    """参与者在对话中的角色。

    ``INITIATOR`` 是发起方，``PEER`` 是对端。角色不授予额外权限，只用于展示与
    审计；权限判断统一走"是否参与者"。
    """

    INITIATOR = "initiator"
    PEER = "peer"


def participant_key(account_a: str, account_b: str) -> str:
    """一对账号的规范化键：与顺序无关，用于唯一约束。

    直接对话的"同一对账号"必须与谁先发起无关，否则会出现两条并行的 direct 对话。
    """
    if not account_a or not account_b:
        raise ValidationError("参与者账号不能为空")
    if account_a == account_b:
        raise ValidationError("不能和自己建立对话")
    low, high = sorted((account_a, account_b))
    return f"{low}|{high}"


@dataclass(slots=True)
class Conversation:
    """一对一会话线程。"""

    conversation_id: str
    kind: ConversationKind
    participant_key: str
    created_at: datetime
    created_by: str
    updated_at: datetime
    closed_at: datetime | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    #: 连续自动往返计数。每次由自动唤醒触发的回合 +1，人工回合清零（设计文档 §11）。
    auto_turn_count: int = 0
    last_message_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def is_writable(self) -> bool:
        """关闭或阻塞的对话不再接受新消息。"""
        return self.is_open and not self.blocked

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "kind": self.kind.value,
            "participants_key": self.participant_key,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "auto_turn_count": self.auto_turn_count,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
        }


@dataclass(frozen=True, slots=True)
class ConversationParticipant:
    """对话成员关系。"""

    conversation_id: str
    account_id: str
    role: ParticipantRole
    joined_at: datetime
    last_read_message_id: str | None = None
    unread_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "account_id": self.account_id,
            "role": self.role.value,
            "joined_at": self.joined_at.isoformat(),
            "last_read_message_id": self.last_read_message_id,
            "unread_count": self.unread_count,
        }
