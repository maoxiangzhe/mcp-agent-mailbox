"""会话寻址的 MCP 智能体邮箱。

分层（依赖方向单向向内，领域层不依赖 MCP SDK、SQLite 或任何具体宿主）：

    domain          纯领域模型与状态机，无 I/O
    ports           领域/应用层需要的外部能力协议（仓储、时钟、事件通道、宿主适配器）
    application     用例编排：账号、会话、投递、在线状态
    infrastructure  SQLite 仓储与迁移、传输、结构化日志、跨进程锁
    adapters        宿主适配器（DSH / Codex / ...），能力等级与降级策略
    daemon          常驻 Broker 与生命周期
    mcp             MCP 端点：连接上下文、工具注册、stdio 代理

唯一权威状态是 SQLite。Markdown / JSONL 只允许作为兼容输入或人类可读投影。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.1"
