"""只读查询门面：监控台与存储实现之间的唯一边界。

规则：

- **只 SELECT**。所有语句跑在 ``infrastructure.sqlite.read_only`` 提供的
  ``mode=ro`` + ``query_only=ON`` 连接上，写语句会被 SQLite 直接拒绝；
- ``total`` 一律来自独立聚合查询，绝不用当前页长度冒充全局总数；
- 所有列表都有 page size 上限、稳定排序（都带唯一列做次序兜底）和不透明游标；
- 不导入 Broker、不导入应用服务、不导入迁移：监控台不能执行任何业务动作。

presence 与能力判定复用领域层的纯函数（``domain.presence``），保证监控台和 Broker
对"在线"的理解完全一致，而不是各写一套。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..domain.presence import HostCapabilityLevel, PresencePolicy, PresenceState, compute_presence
from ..domain.timestamps import parse_timestamp, utc_now
from ..infrastructure.sqlite.read_only import ReadOnlyDatabaseError, read_only_query
from . import serializers as S

__all__ = ["CursorError", "DashboardQueries", "Page", "ResourceGoneError"]

#: page size 硬上限：任何请求都不允许把整张表送进浏览器。
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25

#: 审计时间线一次最多取多少条（总览用）。
_TIMELINE_LIMIT = 20


class CursorError(ValueError):
    """游标非法（格式错误、字段缺失、与当前排序不匹配）。"""


class ResourceGoneError(LookupError):
    """游标指向的资源已经不存在（查询期间被删除）。"""


@dataclass(slots=True)
class Page:
    """一页结果 + 真实全局总数。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    has_more: bool = False

    def to_meta(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "count": len(self.items),
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "page_size_limit": MAX_PAGE_SIZE,
        }


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, expected: tuple[str, ...]) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError) as exc:
        raise CursorError("游标格式非法") from exc
    if not isinstance(data, dict) or tuple(sorted(data)) != tuple(sorted(expected)):
        raise CursorError("游标字段与当前查询不匹配")
    for value in data.values():
        if not isinstance(value, str):
            raise CursorError("游标字段类型非法")
    return data


def _clamp_page_size(value: int | None) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int):
        raise CursorError("page_size 必须是整数")
    if value < 1:
        raise CursorError("page_size 必须大于 0")
    return min(value, MAX_PAGE_SIZE)


def _presence_for(
    connection_row: sqlite3.Row | None,
    *,
    now: datetime | None = None,
    policy: PresencePolicy | None = None,
) -> PresenceState:
    """由连接行推导 presence（与 Broker 用同一套领域规则）。

    ``now`` / ``policy`` 仅为兼容旧调用点保留：在线只看托管进程，不看时间与租约。
    """
    del now, policy
    if connection_row is None:
        return PresenceState.OFFLINE
    return compute_presence(
        closed=str(connection_row["state"]) != "active",
        host_pid=(
            int(connection_row["host_pid"]) if connection_row["host_pid"] is not None else None
        ),
    )


def wake_channel_hosts() -> set[str]:
    """本进程能装配出注入通道的宿主类型。

    监控台是给人看的，所以这里**不猜**：真的去问一遍适配器能不能装配出通道
    （例如 DSH 需要读得到签名密钥）。装配不出来就如实显示"没有注入通道"。
    """
    hosts: set[str] = set()
    try:
        from ..adapters.dsh_wake import DshWebWaker

        if DshWebWaker.from_environment() is not None:
            hosts.add("dsh")
    except Exception:  # noqa: BLE001 - 监控台不能因为探测失败而崩
        pass
    return hosts


class DashboardQueries:
    """监控台的全部读路径。

    每个方法自己开一条只读连接、执行、关闭：HTTP 处理线程与查询生命周期一致，
    不会把连接跨线程复用。监控台流量是人工浏览级别，不为此引入连接池。
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        presence_policy: PresencePolicy | None = None,
        now: datetime | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.presence_policy = presence_policy or PresencePolicy()
        self._now_override = now

    # -- 基础设施 ---------------------------------------------------------

    def _now(self) -> datetime:
        return self._now_override or utc_now()

    def _fetch(
        self, statements: Sequence[tuple[str, Sequence[Any]]]
    ) -> list[list[sqlite3.Row]]:
        with read_only_query(self.database_path) as connection:
            results: list[list[sqlite3.Row]] = []
            for sql, params in statements:
                try:
                    results.append(connection.execute(sql, tuple(params)).fetchall())
                except sqlite3.OperationalError as exc:
                    raise ReadOnlyDatabaseError(str(exc)) from exc
            return results

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._fetch([(sql, params)])[0]
        return rows[0] if rows else None

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """取单行单列的标量值。比"在语句数组里数下标"更不容易错位。"""
        row = self._one(sql, params)
        return None if row is None else row[0]

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        row = self._one(sql, params)
        return 0 if row is None else int(row[0])

    def schema_version(self) -> int:
        row = self._one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
        return int(row["v"]) if row is not None else 0

    # -- 健康 -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """不含任何敏感内容的存活信息。

        数据库不可读时**不抛异常**：健康检查的职责就是报告"不可用"，抛出异常会让
        连接直接断开，反而拿不到结构化诊断。健康检查是唯一不需要令牌的端点，
        因此这里的内容必须严格限定在服务自身状态。
        """
        version = 0
        reachable = True
        detail = "只读连接可用"
        try:
            version = self.schema_version()
        except (ReadOnlyDatabaseError, sqlite3.Error) as exc:
            reachable = False
            detail = str(exc)
        return {
            "service": "mcp-agent-mailbox-dashboard",
            "read_only": True,
            "database_reachable": reachable,
            "database_detail": detail,
            "schema_version": version,
        }

    # -- 总览 -------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        now = self._now()
        (
            account_rows,
            connection_rows,
            delivery_rows,
            level_rows,
            audit_rows,
        ) = self._fetch(
            [
                ("SELECT * FROM accounts WHERE deleted_at IS NULL ORDER BY created_at ASC", ()),
                ("SELECT * FROM connections WHERE is_current = 1", ()),
                (
                    "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state",
                    (),
                ),
                (
                    "SELECT capability_level, COUNT(*) AS count FROM accounts "
                    "WHERE deleted_at IS NULL GROUP BY capability_level",
                    (),
                ),
                (
                    "SELECT event_type, account_id, connection_id, conversation_id, message_id, "
                    "delivery_id, created_at FROM audit_events "
                    "ORDER BY audit_id DESC LIMIT ?",
                    (_TIMELINE_LIMIT,),
                ),
            ]
        )
        # 标量计数各自单独取：比在同一个语句数组里数下标更不容易错位。
        message_total = int(self._scalar("SELECT COUNT(*) FROM messages") or 0)
        conversation_total = int(self._scalar("SELECT COUNT(*) FROM conversations") or 0)
        unread_total = int(
            self._scalar("SELECT COALESCE(SUM(unread_count), 0) FROM conversation_participants")
            or 0
        )

        connections_by_account = {str(row["account_id"]): row for row in connection_rows}
        presence_counts: dict[str, int] = {state.value: 0 for state in PresenceState}
        can_wake = 0
        level_counts = {0: 0, 1: 0, 2: 0}
        accounts: list[dict[str, Any]] = []
        channels = wake_channel_hosts()
        for row in account_rows:
            connection = connections_by_account.get(str(row["account_id"]))
            presence = _presence_for(connection, now=now, policy=self.presence_policy)
            presence_counts[presence.value] += 1
            level = int(connection["capability_level"]) if connection is not None else int(row["capability_level"])
            level_counts[level] = level_counts.get(level, 0) + 1
            dto = S.account_row(
                row,
                presence=presence.value,
                connection=connection,
                wake_channel=str(row["host_type"]) in channels,
            )
            if dto["can_wake"]:
                can_wake += 1
            accounts.append(dto)

        delivery_counts = {str(row["state"]): int(row["count"]) for row in delivery_rows}
        for state in ("queued", "dispatched", "delivered", "failed", "dead_letter"):
            delivery_counts.setdefault(state, 0)

        return {
            "counts": {
                "accounts": len(account_rows),
                "accounts_online": presence_counts.get("connected", 0),
                "conversations": conversation_total,
                "messages": message_total,
                "unread_messages": unread_total,
                "can_wake_now": can_wake,
            },
            "presence": presence_counts,
            "deliveries": delivery_counts,
            "backlog": {
                "queued": delivery_counts["queued"],
                "failed": delivery_counts["failed"],
                "dead_letter": delivery_counts["dead_letter"],
                "pending_total": delivery_counts["queued"]
                + delivery_counts["dispatched"]
                + delivery_counts["failed"],
            },
            "capability": self._capability_summary(level_counts, accounts),
            "timeline": [
                {
                    "event_type": str(row["event_type"]),
                    "account_id": row["account_id"],
                    "connection_id": row["connection_id"],
                    "conversation_id": row["conversation_id"],
                    "message_id": row["message_id"],
                    "delivery_id": row["delivery_id"],
                    "created_at": S.timestamp(row["created_at"]),
                }
                for row in audit_rows
            ],
            "timeline_note": "时间线只显示事件类型与关联 ID，不显示任何消息正文。",
        }

    def _capability_summary(
        self, level_counts: dict[int, int], accounts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """能力与验证状态分布。

        ``verified`` 来自适配器**代码里如实登记的声明**（``adapters.capability_matrix``），
        不是数据库列，也不是从 level 推断出来的。这样界面不可能把
        "Level 2 声明"误显示成"已验证"。
        """
        from ..adapters import capability_matrix

        matrix = capability_matrix()
        verified_hosts = {
            str(row["host_type"]) for row in matrix["adapters"] if row.get("verified")
        }
        declared_level2 = {
            str(row["host_type"]) for row in matrix["adapters"] if int(row["level"]) >= 2
        }
        host_types = {account["host_type"] for account in accounts}
        return {
            "declared_levels": {str(level): count for level, count in sorted(level_counts.items())},
            "accounts_can_wake_now": sum(1 for account in accounts if account["can_wake"]),
            "hosts_declaring_level2": sorted(declared_level2),
            "hosts_verified": sorted(verified_hosts),
            "host_types_present": sorted(host for host in host_types if host),
            "verified_note": (
                "verified 表示该适配器能力已在本机真实验证；capability_level=2 只表示"
                "适配器声明了唤醒能力。两者都不是实时可达的证明。"
            ),
        }

    # -- 账号 -------------------------------------------------------------

    def accounts(
        self,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        host_type: str | None = None,
        presence: str | None = None,
    ) -> Page:
        size = _clamp_page_size(page_size)
        wanted: PresenceState | None = None
        if presence:
            try:
                wanted = PresenceState(presence.strip().lower())
            except ValueError as exc:
                raise CursorError(
                    "presence 只能是 connected / offline"
                ) from exc

        where = ["a.deleted_at IS NULL"]
        params: list[Any] = []
        if host_type:
            where.append("a.host_type = ?")
            params.append(host_type.strip().lower())
        if cursor:
            payload = _decode_cursor(cursor, expected=("created_at", "id"))
            where.append("(a.created_at, a.account_id) > (?, ?)")
            params.extend([payload["created_at"], payload["id"]])
        clause = " AND ".join(where)

        total = self._count(f"SELECT COUNT(*) FROM accounts a WHERE {clause}", params)

        rows, connection_rows = self._fetch(
            [
                (
                    f"SELECT a.* FROM accounts a WHERE {clause} "
                    "ORDER BY a.created_at ASC, a.account_id ASC LIMIT ?",
                    [*params, size + 1],
                ),
                ("SELECT * FROM connections WHERE is_current = 1", ()),
            ]
        )
        has_more = len(rows) > size
        rows = rows[:size]
        connections = {str(row["account_id"]): row for row in connection_rows}
        now = self._now()

        items: list[dict[str, Any]] = []
        channels = wake_channel_hosts()
        for row in rows:
            connection = connections.get(str(row["account_id"]))
            state = _presence_for(connection, now=now, policy=self.presence_policy)
            dto = S.account_row(
                row,
                presence=state.value,
                connection=connection,
                wake_channel=str(row["host_type"]) in channels,
            )
            if wanted is not None and state is not wanted:
                continue
            items.append(dto)

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor(
                {"created_at": str(last["created_at"]), "id": str(last["account_id"])}
            )
        return Page(items=items, total=total, next_cursor=next_cursor, has_more=bool(next_cursor))

    def account(self, account_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM accounts WHERE account_id = ? AND deleted_at IS NULL", (account_id,)
        )
        if row is None:
            return None
        now = self._now()
        current, connections, stats_row = self._fetch(
            [
                (
                    "SELECT * FROM connections WHERE account_id = ? AND is_current = 1",
                    (account_id,),
                ),
                (
                    "SELECT * FROM connections WHERE account_id = ? "
                    "ORDER BY generation DESC LIMIT 50",
                    (account_id,),
                ),
                (
                    "SELECT "
                    "(SELECT COUNT(*) FROM messages WHERE sender_account_id = ?) AS sent, "
                    "(SELECT COUNT(*) FROM messages WHERE recipient_account_id = ?) AS received, "
                    "(SELECT COUNT(*) FROM conversation_participants WHERE account_id = ?) "
                    "  AS conversations, "
                    "(SELECT COALESCE(SUM(unread_count), 0) FROM conversation_participants "
                    "  WHERE account_id = ?) AS unread, "
                    "(SELECT COUNT(*) FROM deliveries WHERE account_id = ?) AS deliveries, "
                    "(SELECT COUNT(*) FROM deliveries WHERE account_id = ? "
                    "  AND state = 'dead_letter') AS dead_letter",
                    (account_id, account_id, account_id, account_id, account_id, account_id),
                ),
            ]
        )
        connection = current[0] if current else None
        state = _presence_for(connection, now=now, policy=self.presence_policy)
        stats = S.row_to_dict(stats_row[0] if stats_row else None)
        return S.account_detail(
            row,
            presence=state.value,
            connection=connection,
            connections=list(connections),
            stats={key: int(value or 0) for key, value in stats.items()},
            wake_channel=str(row["host_type"]) in wake_channel_hosts(),
        )

    # -- 对话 -------------------------------------------------------------

    def conversations(
        self,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        account_id: str | None = None,
        unread_only: bool = False,
        blocked: bool | None = None,
    ) -> Page:
        size = _clamp_page_size(page_size)
        where: list[str] = []
        params: list[Any] = []
        join_participant = ""
        unread_expr = (
            "(SELECT COALESCE(SUM(p2.unread_count), 0) FROM conversation_participants p2 "
            " WHERE p2.conversation_id = c.conversation_id)"
        )
        if account_id:
            join_participant = "JOIN conversation_participants p ON p.conversation_id = c.conversation_id"
            where.append("p.account_id = ?")
            params.append(account_id)
        if unread_only:
            where.append(f"{unread_expr} > 0")
        if blocked is not None:
            where.append("c.blocked = ?")
            params.append(1 if blocked else 0)
        if cursor:
            payload = _decode_cursor(cursor, expected=("id", "sort_key"))
            where.append(
                "(COALESCE(c.last_message_at, c.created_at), c.conversation_id) < (?, ?)"
            )
            params.extend([payload["sort_key"], payload["id"]])
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        total = self._count(
            f"SELECT COUNT(*) FROM conversations c {join_participant}{clause}", params
        )
        rows = self._fetch(
            [
                (
                    "SELECT c.*, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) "
                    "  AS message_count, "
                    f"{unread_expr} AS unread_total "
                    f"FROM conversations c {join_participant}{clause} "
                    "ORDER BY COALESCE(c.last_message_at, c.created_at) DESC, "
                    "c.conversation_id DESC LIMIT ?",
                    [*params, size + 1],
                )
            ]
        )[0]
        has_more = len(rows) > size
        rows = rows[:size]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor(
                {
                    "sort_key": str(last["last_message_at"] or last["created_at"]),
                    "id": str(last["conversation_id"]),
                }
            )
        items = [S.conversation_row(row) for row in rows]
        if items:
            ids = [item["conversation_id"] for item in items]
            placeholders = ",".join("?" for _ in ids)
            participants, previews = self._fetch([
                (
                    "SELECT p.conversation_id, a.display_name FROM conversation_participants p "
                    "JOIN accounts a ON a.account_id = p.account_id "
                    f"WHERE p.conversation_id IN ({placeholders}) ORDER BY p.rowid",
                    ids,
                ),
                (
                    "SELECT m.conversation_id, substr(m.content, 1, 120) AS content FROM messages m "
                    f"WHERE m.conversation_id IN ({placeholders}) AND m.rowid = "
                    "(SELECT MAX(last.rowid) FROM messages last WHERE last.conversation_id = m.conversation_id)",
                    ids,
                ),
            ])
            names: dict[str, list[str]] = {}
            for participant in participants:
                names.setdefault(str(participant["conversation_id"]), []).append(str(participant["display_name"]))
            content = {str(row["conversation_id"]): str(row["content"]) for row in previews}
            for item in items:
                item["participant_names"] = names.get(item["conversation_id"], [])
                item["last_content"] = content.get(item["conversation_id"], "")
        return Page(
            items=items,
            total=total,
            next_cursor=next_cursor,
            has_more=bool(next_cursor),
        )

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        rows, participants, breakdown = self._fetch(
            [
                (
                    "SELECT c.*, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) "
                    "  AS message_count, "
                    "(SELECT COALESCE(SUM(p2.unread_count), 0) FROM conversation_participants p2 "
                    " WHERE p2.conversation_id = c.conversation_id) AS unread_total "
                    "FROM conversations c WHERE c.conversation_id = ?",
                    (conversation_id,),
                ),
                (
                    "SELECT p.*, a.display_name FROM conversation_participants p "
                    "LEFT JOIN accounts a ON a.account_id = p.account_id "
                    "WHERE p.conversation_id = ? ORDER BY p.joined_at ASC, p.account_id ASC",
                    (conversation_id,),
                ),
                (
                    "SELECT state, COUNT(*) AS count FROM deliveries WHERE conversation_id = ? "
                    "GROUP BY state",
                    (conversation_id,),
                ),
            ]
        )
        if not rows:
            return None
        return S.conversation_detail(
            rows[0],
            participants=[
                S.participant_row(row, display_name=S.row_to_dict(row).get("display_name"))
                for row in participants
            ],
            delivery_breakdown={str(row["state"]): int(row["count"]) for row in breakdown},
        )

    # -- 消息 -------------------------------------------------------------

    def messages(
        self,
        conversation_id: str,
        *,
        page_size: int | None = None,
        after_message_id: str | None = None,
        before_message_id: str | None = None,
    ) -> Page:
        """按**入队顺序**分页返回消息，附带各自的三个状态维度。

        游标用 ``rowid``（消息表的自增主键）而不是时间戳：同一毫秒内创建的多条消息
        靠它才能严格排序。响应里对外暴露的是 ``message_id``，内部换算成 rowid。
        """
        if not self._conversation_exists(conversation_id):
            raise ResourceGoneError("对话不存在或已被删除")
        size = _clamp_page_size(page_size)

        lower = 0
        if after_message_id:
            lower = self._rowid_of(conversation_id, after_message_id)
        upper: int | None = None
        if before_message_id:
            upper = self._rowid_of(conversation_id, before_message_id)

        where = ["m.conversation_id = ?", "m.rowid > ?"]
        params: list[Any] = [conversation_id, lower]
        if upper is not None:
            where.append("m.rowid < ?")
            params.append(upper)
        clause = " AND ".join(where)

        total = self._count(
            "SELECT COUNT(*) FROM messages m WHERE m.conversation_id = ?", (conversation_id,)
        )
        rows = self._fetch(
            [
                (
                    "SELECT m.*, "
                    "(SELECT d.state FROM deliveries d WHERE d.message_id = m.message_id "
                    "  AND d.account_id = m.recipient_account_id) AS delivery, "
                    "(SELECT p.state FROM message_processing p WHERE p.message_id = m.message_id "
                    "  AND p.account_id = m.recipient_account_id) AS processing, "
                    "(SELECT p.result FROM message_processing p WHERE p.message_id = m.message_id "
                    "  AND p.account_id = m.recipient_account_id) AS processing_result, "
                    "(SELECT a.display_name FROM accounts a "
                    "  WHERE a.account_id = m.sender_account_id) AS sender_display_name, "
                    "(SELECT a.display_name FROM accounts a "
                    "  WHERE a.account_id = m.recipient_account_id) AS recipient_display_name, "
                    "CASE WHEN EXISTS ("
                    "  SELECT 1 FROM conversation_participants cp "
                    "  JOIN messages lr ON lr.message_id = cp.last_read_message_id "
                    "  WHERE cp.conversation_id = m.conversation_id "
                    "    AND cp.account_id = m.recipient_account_id "
                    "    AND lr.rowid >= m.rowid"
                    ") THEN 'seen' ELSE 'unread' END AS visibility "
                    f"FROM messages m WHERE {clause} ORDER BY m.rowid ASC LIMIT ?",
                    [*params, size + 1],
                )
            ]
        )[0]
        has_more = len(rows) > size
        rows = rows[:size]

        items: list[dict[str, Any]] = []
        for row in rows:
            data = S.row_to_dict(row)
            items.append(
                S.message_row(
                    data,
                    delivery=data.get("delivery"),
                    sender_display_name=data.get("sender_display_name"),
                    recipient_display_name=data.get("recipient_display_name"),
                )
            )
        next_cursor = items[-1]["message_id"] if (has_more and items) else None
        return Page(items=items, total=total, next_cursor=next_cursor, has_more=bool(next_cursor))

    def _conversation_exists(self, conversation_id: str) -> bool:
        return (
            self._one(
                "SELECT 1 FROM conversations WHERE conversation_id = ?", (conversation_id,)
            )
            is not None
        )

    def _rowid_of(self, conversation_id: str, message_id: str) -> int:
        row = self._one(
            "SELECT rowid FROM messages WHERE message_id = ? AND conversation_id = ?",
            (message_id, conversation_id),
        )
        if row is None:
            # 游标消息不在该对话里：可能是非法游标，也可能是查询期间被删除。
            if self._conversation_exists(conversation_id):
                raise CursorError("游标消息不属于该对话，或已不存在")
            raise ResourceGoneError("对话不存在或已被删除")
        return int(row["rowid"])

    # -- 投递 -------------------------------------------------------------

    def deliveries(
        self,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
        state: str | None = None,
        account_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Page:
        size = _clamp_page_size(page_size)
        allowed = {"queued", "dispatched", "delivered", "failed", "dead_letter"}
        where: list[str] = []
        params: list[Any] = []
        if state:
            normalized = state.strip().lower()
            if normalized not in allowed:
                raise CursorError(
                    "state 只能是 queued / dispatched / delivered / failed / dead_letter"
                )
            where.append("state = ?")
            params.append(normalized)
        if account_id:
            where.append("account_id = ?")
            params.append(account_id)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if cursor:
            payload = _decode_cursor(cursor, expected=("seq",))
            where.append("delivery_seq < ?")
            params.append(int(payload["seq"]))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        total = self._count(f"SELECT COUNT(*) FROM deliveries{clause}", params)
        rows = self._fetch(
            [
                (
                    f"SELECT * FROM deliveries{clause} "
                    "ORDER BY delivery_seq DESC LIMIT ?",
                    [*params, size + 1],
                )
            ]
        )[0]
        has_more = len(rows) > size
        rows = rows[:size]
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_cursor({"seq": str(rows[-1]["delivery_seq"])})
        return Page(
            items=[S.delivery_row(row) for row in rows],
            total=total,
            next_cursor=next_cursor,
            has_more=bool(next_cursor),
        )

    # -- 诊断 -------------------------------------------------------------

    def diagnostics(self, *, database_path_display: str | None = None) -> dict[str, Any]:
        """数据库、迁移、配置与宿主能力诊断。

        - 数据库路径默认只显示**文件名**，不暴露绝对路径；
        - "Broker 是否仅为当前进程实例"与"跨进程事件通道是否实现"如实说明；
        - 能力矩阵直接来自适配器代码里的登记，不猜测。
        """
        from ..adapters import capability_matrix
        from ..config import load_settings
        from ..infrastructure.sqlite.read_only import read_only_connection

        settings = load_settings()
        from ..infrastructure.observability import audit_event_types

        table_names = (
            "accounts",
            "connections",
            "conversations",
            "conversation_participants",
            "messages",
            "deliveries",
            "message_processing",
            "adapter_checkpoints",
            "schema_migrations",
            "audit_events",
            "account_contacts",
            "rate_counters",
        )
        statements: list[tuple[str, Sequence[Any]]] = [
            ("SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC", ()),
            (
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name ASC",
                (),
            ),
        ]
        for table in table_names:
            statements.append((f"SELECT COUNT(*) AS c FROM {table}", ()))
        results = self._fetch(statements)

        migrations = [
            {
                "version": int(row["version"]),
                "name": str(row["name"]),
                "applied_at": S.timestamp(row["applied_at"]),
            }
            for row in results[0]
        ]
        tables = [str(row["name"]) for row in results[1]]
        counts = {
            table: int(results[2 + index][0]["c"]) for index, table in enumerate(table_names)
        }

        integrity = "unknown"
        journal_mode = "unknown"
        integrity_error: str | None = None
        try:
            with read_only_query(self.database_path) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        except (ReadOnlyDatabaseError, sqlite3.Error) as exc:
            integrity_error = str(exc)

        display = database_path_display or self.database_path.name
        return {
            "database": {
                "path_display": display,
                "file_size_bytes": (
                    self.database_path.stat().st_size if self.database_path.exists() else 0
                ),
                "integrity_check": integrity,
                "integrity_error": integrity_error,
                "journal_mode": journal_mode,
                "schema_version": migrations[-1]["version"] if migrations else 0,
                "tables": tables,
                "row_counts": counts,
            },
            "migrations": migrations,
            "broker": {
                "authority": "SQLite（WAL + 外键 + 显式事务）",
                "in_process_instance": True,
                "cross_process_event_channel": False,
                "cross_process_note": (
                    "多个 stdio MCP 进程通过同一个 SQLite 共享权威状态；"
                    "但实时事件通道是**进程内**的，跨进程实时投递尚未实现。"
                    "因此监控台不会把任何账号描述成『实时可达』。"
                ),
                "maintenance_loop": "由持有 Broker 的进程执行（回收托管进程已退出的连接、派发、重试）",
            },
            "capability": capability_matrix(),
            "settings": {
                "heartbeat_seconds": settings.presence.heartbeat_interval_seconds,
                "lease_seconds": settings.presence.lease_seconds,
                "grace_seconds": settings.presence.grace_seconds,
                "max_auto_turns": settings.rate_limits.max_auto_turns_per_conversation,
                "max_auto_wakes_per_hour": settings.rate_limits.max_auto_wakes_per_hour,
                "repeated_content_limit": settings.rate_limits.repeated_content_limit,
                "max_sends_per_minute": settings.rate_limits.max_sends_per_minute,
                "max_message_chars": settings.max_message_chars,
                "max_delivery_attempts": settings.delivery.max_attempts,
                "global_pause": settings.global_pause,
                "legacy_tools_enabled": settings.legacy_tools_enabled,
                "log_message_content": settings.log_message_content,
                "data_dir_display": settings.data_dir.name,
            },
            "audit_event_types": list(audit_event_types()),
            "honesty_notes": [
                "capability_level=2 只表示适配器声明了唤醒能力，不等于真实可唤醒。",
                "verified=false 表示该适配器尚未在真实宿主上完成端到端验证。",
                "delivered 只表示宿主已可靠接收，不代表任务完成。",
            ],
        }
