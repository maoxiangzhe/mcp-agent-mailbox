"""领域错误。

约定：
    - 领域层只抛这些错误，不抛 ``ValueError``／``RuntimeError`` 等内建异常，
      这样应用层和 MCP 层可以用一种方式区分"用户输入问题"和"系统故障"。
    - 每个错误都带 ``code``：MCP 工具把它放进结构化返回值，日志用它做聚合，
      调用方不应该靠匹配人类可读文本判分支。
"""

from __future__ import annotations

__all__ = [
    "ConversationClosedError",
    "DomainError",
    "IdentityMismatchError",
    "InvalidTransitionError",
    "LoopLimitExceededError",
    "NotAParticipantError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "RecipientBlockedError",
    "ValidationError",
]


class DomainError(Exception):
    """领域错误基类。``code`` 是稳定的机器可读标识。"""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class ValidationError(DomainError):
    """输入不满足领域约束（长度、格式、取值）。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """目标对象不存在，或对调用者不可见。"""

    code = "not_found"


class PermissionDeniedError(DomainError):
    """调用者无权执行该操作。"""

    code = "permission_denied"


class IdentityMismatchError(PermissionDeniedError):
    """连接身份与目标账号不一致。

    典型场景：旧代次连接试图确认新代次投递，或连接上下文被换成了别的账号。
    这是安全边界，必须拒绝而不是降级处理。
    """

    code = "identity_mismatch"


class NotAParticipantError(PermissionDeniedError):
    """账号不是该对话的参与者，不得读写信箱。"""

    code = "not_a_participant"


class RecipientBlockedError(PermissionDeniedError):
    """收件人拒绝了该发件人，或发件人拉黑了收件人。"""

    code = "recipient_blocked"


class InvalidTransitionError(DomainError):
    """状态机不允许的迁移。"""

    code = "invalid_transition"


class ConversationClosedError(DomainError):
    """对话已关闭，不再接受新消息。"""

    code = "conversation_closed"


class RateLimitedError(DomainError):
    """超过速率限制；``retry_after_ms`` 给出建议等待时间。"""

    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_ms: int = 0) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class LoopLimitExceededError(DomainError):
    """自动互聊达到硬上限；消息保留，但不再唤醒对端。"""

    code = "loop_limit_exceeded"

    def __init__(self, message: str, *, auto_turns: int, limit: int) -> None:
        super().__init__(message)
        self.auto_turns = auto_turns
        self.limit = limit
