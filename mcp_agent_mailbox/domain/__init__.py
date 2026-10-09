"""领域层：纯模型、状态机与领域错误。

本层不允许出现 ``sqlite3``、``mcp`` 或任何宿主 SDK 的导入；它只描述业务事实。
"""

from __future__ import annotations

from .accounts import Account, Connection, ConnectionState, HostIdentity
from .errors import (
    ConversationClosedError,
    DomainError,
    IdentityMismatchError,
    InvalidTransitionError,
    LoopLimitExceededError,
    NotAParticipantError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    RecipientBlockedError,
    ValidationError,
)
from .messages import (
    ContentType,
    DeliveryRecord,
    DeliveryState,
    Message,
    MessageView,
    ProcessingRecord,
    ProcessingState,
    VisibilityState,
)
from .presence import HostCapabilityLevel, PresencePolicy, PresenceState, compute_presence

__all__ = [
    "Account",
    "Connection",
    "ConnectionState",
    "ContentType",
    "ConversationClosedError",
    "DeliveryRecord",
    "DeliveryState",
    "DomainError",
    "HostCapabilityLevel",
    "HostIdentity",
    "IdentityMismatchError",
    "InvalidTransitionError",
    "LoopLimitExceededError",
    "Message",
    "MessageView",
    "NotAParticipantError",
    "NotFoundError",
    "PermissionDeniedError",
    "PresencePolicy",
    "PresenceState",
    "ProcessingRecord",
    "ProcessingState",
    "RateLimitedError",
    "RecipientBlockedError",
    "ValidationError",
    "VisibilityState",
    "compute_presence",
]
