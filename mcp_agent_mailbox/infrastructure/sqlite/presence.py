"""在线状态的读路径计算。

presence 不落库：它取决于"托管进程现在是否还活着"，任何缓存下来的值都会过期。
因此一律在读取时用 ``domain.presence.compute_presence`` 从权威事实（连接的
``host_pid``）重算。这里只提供把多行连接一次性取出来做批处理的辅助函数，避免 N+1。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ...domain.presence import PresenceState, compute_presence

__all__ = ["presence_for_accounts", "presence_from_row"]

_CURRENT_CONNECTION_SQL = (
    "SELECT host_pid, state FROM connections WHERE account_id = ? AND is_current = 1"
)


def presence_from_row(row: sqlite3.Row | None, *, now: datetime | None = None) -> PresenceState:
    """由一行当前连接（或 ``None``）推导 presence。

    ``now`` 只为兼容旧调用点保留：在线判定不再依赖时间。
    """
    del now
    if row is None:
        return PresenceState.OFFLINE
    closed = str(row["state"]) != "active"
    return compute_presence(
        host_pid=(int(row["host_pid"]) if row["host_pid"] is not None else None),
        closed=closed,
    )


def presence_for_accounts(
    connection: sqlite3.Connection,
    account_ids: list[str],
    *,
    now: datetime | None = None,
    policy: object | None = None,
) -> dict[str, PresenceState]:
    """批量计算多个账号的 presence。

    ``now`` / ``policy`` 只为兼容旧调用点保留：在线只看进程，不看时间与租约。
    """
    del now, policy
    result: dict[str, PresenceState] = {}
    for account_id in account_ids:
        row = connection.execute(_CURRENT_CONNECTION_SQL, (account_id,)).fetchone()
        result[account_id] = presence_from_row(row)
    return result
