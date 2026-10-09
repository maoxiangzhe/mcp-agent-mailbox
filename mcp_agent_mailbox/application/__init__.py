"""应用层：用例编排。

服务之间共享 ``ServiceContext``（工作单元工厂、时钟、事件发布器、配置），
不互相持有，避免应用层内部循环依赖。领域规则在 domain，事务与 SQL 在
infrastructure；本层只负责"一次用例按什么顺序做哪些事"。
"""

from __future__ import annotations

from .account_service import AccountService, BoundSession, ContactPolicyEntry, RegisterResult
from .context import ServiceContext
from .conversation_service import (
    ContactView,
    ConversationService,
    ConversationView,
    MessagePageView,
    SendResult,
)
from .delivery_service import DeliveryService, DispatchReport
from .presence_service import PresenceService, PresenceView

__all__ = [
    "AccountService",
    "BoundSession",
    "ContactPolicyEntry",
    "ContactView",
    "ConversationService",
    "ConversationView",
    "DeliveryService",
    "DispatchReport",
    "MessagePageView",
    "PresenceService",
    "PresenceView",
    "RegisterResult",
    "SendResult",
    "ServiceContext",
]
