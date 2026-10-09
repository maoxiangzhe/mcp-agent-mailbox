"""MCP 层：连接上下文、工具注册与 stdio 服务器装配。

本层依赖 MCP SDK；领域层与应用层**不**依赖它，因此更换协议实现不会牵动业务逻辑。
"""

from __future__ import annotations

from .connection_context import (
    ENV_HOST_INSTANCE_ID,
    ENV_HOST_TYPE,
    ENV_SESSION_ID,
    EnvironmentSessionProvider,
    MailboxConnection,
    SessionDescriptor,
    SessionIdentityProvider,
    UnavailableSessionProvider,
    machine_instance_id,
)
from .server import MailboxServer, build_server, serve_stdio
from .tools import ToolRuntime, register_tools

__all__ = [
    "ENV_HOST_INSTANCE_ID",
    "ENV_HOST_TYPE",
    "ENV_SESSION_ID",
    "EnvironmentSessionProvider",
    "MailboxConnection",
    "MailboxServer",
    "SessionDescriptor",
    "SessionIdentityProvider",
    "ToolRuntime",
    "UnavailableSessionProvider",
    "build_server",
    "machine_instance_id",
    "register_tools",
    "serve_stdio",
]
