"""只读 Web 监控台。

设计依据：``docs/read-only-dashboard-design.md``。

三层职责：

    queries.py      只读查询门面（包装只读 SQLite 连接 + 既有仓储）
    serializers.py  领域对象/行 -> 稳定 JSON DTO
    server.py       回环 HTTP 服务、鉴权、路由、安全头、静态资源

本包**不得**导入任何会写库的东西：不导入 ``Broker``、不导入应用服务、不导入迁移。
"""

from __future__ import annotations

from .queries import DashboardQueries
from .server import DashboardServer, DashboardToken, serve_dashboard

__all__ = [
    "DashboardQueries",
    "DashboardServer",
    "DashboardToken",
    "serve_dashboard",
]
