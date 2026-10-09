"""联系人策略与速率计数仓储。

两者都属于"辅助约束"而不是核心消息事实，但都必须持久化：

- 联系人策略必须是跨进程一致的，因为发信检查在服务层、可能发生在不同 MCP 端点；
- 速率计数必须持久化，否则每次连接重置窗口就等于没有限制。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ...domain.timestamps import format_timestamp

__all__ = ["RateCounterRepository", "SqliteContactRepository"]


class SqliteContactRepository:
    """允许/阻止联系人列表。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def set_policy(
        self,
        *,
        owner_account_id: str,
        contact_account_id: str,
        policy: str,
        note: str | None,
        now: datetime,
    ) -> None:
        self._c.execute(
            "INSERT INTO account_contacts (owner_account_id, contact_account_id, policy, note, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (owner_account_id, contact_account_id) DO UPDATE SET "
            "policy = excluded.policy, note = excluded.note, updated_at = excluded.updated_at",
            (
                owner_account_id,
                contact_account_id,
                policy,
                note,
                format_timestamp(now),
                format_timestamp(now),
            ),
        )

    def clear_policy(self, owner_account_id: str, contact_account_id: str) -> int:
        cursor = self._c.execute(
            "DELETE FROM account_contacts WHERE owner_account_id = ? AND contact_account_id = ?",
            (owner_account_id, contact_account_id),
        )
        return cursor.rowcount

    def list_policies(self, owner_account_id: str) -> list[dict[str, object]]:
        rows = self._c.execute(
            "SELECT contact_account_id, policy, note FROM account_contacts "
            "WHERE owner_account_id = ? ORDER BY policy ASC, contact_account_id ASC",
            (owner_account_id,),
        ).fetchall()
        return [
            {
                "account_id": str(row["contact_account_id"]),
                "policy": str(row["policy"]),
                "note": row["note"],
            }
            for row in rows
        ]

    def policy_between(self, account_a: str, account_b: str) -> dict[tuple[str, str], str]:
        """取两个账号之间存在的策略。

        键是 ``(owner, contact)``；调用方需要分别检查两个方向。只查这两行而不是
        全表，避免把整个联系人表读进内存。
        """
        rows = self._c.execute(
            "SELECT owner_account_id, contact_account_id, policy FROM account_contacts "
            "WHERE (owner_account_id = ? AND contact_account_id = ?) "
            "   OR (owner_account_id = ? AND contact_account_id = ?)",
            (account_a, account_b, account_b, account_a),
        ).fetchall()
        return {
            (str(row["owner_account_id"]), str(row["contact_account_id"])): str(row["policy"])
            for row in rows
        }

    def blocked_owners_for(self, contact_account_id: str) -> list[str]:
        rows = self._c.execute(
            "SELECT owner_account_id FROM account_contacts "
            "WHERE contact_account_id = ? AND policy = 'block'",
            (contact_account_id,),
        ).fetchall()
        return [str(row["owner_account_id"]) for row in rows]


class RateCounterRepository:
    """固定窗口速率计数。

    窗口起点直接作为主键的一部分，因此"读取-比较-自增"不需要额外锁：同一窗口的
    并发自增都是 ``UPDATE ... count = count + 1``，由 SQLite 串行化。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def increment(
        self,
        *,
        scope_key: str,
        window_start: datetime,
        now: datetime,
        amount: int = 1,
    ) -> int:
        """在当前窗口计数 +``amount``，返回计数后的值。"""
        start = format_timestamp(window_start)
        self._c.execute(
            "INSERT INTO rate_counters (scope_key, window_start, count, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope_key, window_start) DO UPDATE SET "
            "count = rate_counters.count + excluded.count, updated_at = excluded.updated_at",
            (scope_key, start, amount, format_timestamp(now)),
        )
        return self.count(scope_key=scope_key, window_start=window_start)

    def count(self, *, scope_key: str, window_start: datetime) -> int:
        row = self._c.execute(
            "SELECT count FROM rate_counters WHERE scope_key = ? AND window_start = ?",
            (scope_key, format_timestamp(window_start)),
        ).fetchone()
        return int(row["count"]) if row else 0

    def prune(self, *, before: datetime) -> int:
        """清理过期窗口，避免计数表无限增长。"""
        cursor = self._c.execute(
            "DELETE FROM rate_counters WHERE window_start < ?", (format_timestamp(before),)
        )
        return cursor.rowcount
