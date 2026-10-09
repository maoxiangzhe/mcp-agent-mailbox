"""SQLite 仓储实现。

约定：

- **所有 SQL 只出现在本模块**（以及 ``migrations.py``）。领域层、应用层、MCP 层
  一行 SQL 都没有，这样"换存储"与"审计查询范围"都是可做的。
- 仓储不做业务判断：状态迁移合法性由领域层校验，权限由应用层校验。仓储只负责
  按数据库约束持久化，并把违反约束的冲突翻译成明确的领域错误。
- 账号、消息、投递、对话的向外标识一律沿用领域层生成的值，仓储不自行拼造 ID。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime
from typing import Any, Iterator, Sequence

from ...domain.accounts import Account, Connection, ConnectionState, HostIdentity
from ...domain.conversations import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    ParticipantRole,
)
from ...domain.errors import NotFoundError, ValidationError
from ...domain.messages import (
    ContentType,
    DeliveryRecord,
    DeliveryState,
    Message,
    MessageView,
    ProcessingRecord,
    ProcessingState,
    VisibilityState,
)
from ...domain.presence import HostCapabilityLevel, PresencePolicy, PresenceState, compute_presence
from ...domain.timestamps import format_timestamp, parse_timestamp
from ...ports.repositories import ConversationSummary, MessagePage
from .contacts import RateCounterRepository, SqliteContactRepository
from .database import Database
from .presence import presence_for_accounts

__all__ = ["SqliteUnitOfWork", "SqliteUnitOfWorkFactory"]

# 数据库内部使用的状态文本（避免直接依赖枚举的 .value 写法被误改）。


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError) as exc:
        raise ValidationError("分页游标无效，请从上一页返回值原样传入") from exc
    if not isinstance(data, dict):
        raise ValidationError("分页游标无效，请从上一页返回值原样传入")
    return data


def _json_object(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"数据库中的 JSON 字段损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("数据库中的 JSON 字段不是对象")
    return data


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# 行 -> 领域对象
# ---------------------------------------------------------------------------


def _account_from_row(row: sqlite3.Row) -> Account:
    return Account(
        account_id=str(row["account_id"]),
        identity=HostIdentity(
            host_type=str(row["host_type"]),
            host_instance_id=str(row["host_instance_id"]),
            native_session_id=str(row["native_session_id"]),
        ),
        display_name=str(row["display_name"]),
        address=str(row["address"]),
        workspace_hint=_optional_text(row, "workspace_hint"),
        capability_level=HostCapabilityLevel(int(row["capability_level"])),
        blocked=bool(row["blocked"]),
        metadata=_json_object(row["metadata_json"]),
        created_at=parse_timestamp(str(row["created_at"])),
        updated_at=parse_timestamp(str(row["updated_at"])),
    )


def _connection_from_row(row: sqlite3.Row) -> Connection:
    return Connection(
        connection_id=str(row["connection_id"]),
        account_id=str(row["account_id"]),
        generation=int(row["generation"]),
        state=ConnectionState(str(row["state"])),
        is_current=bool(row["is_current"]),
        durable=bool(row["durable"]),
        host_pid=(int(row["host_pid"]) if row["host_pid"] is not None else None),
        capability_level=HostCapabilityLevel(int(row["capability_level"])),
        adapter_name=_optional_text(row, "adapter_name"),
        remote_hint=_optional_text(row, "remote_hint"),
        opened_at=parse_timestamp(str(row["opened_at"])),
        heartbeat_at=(
            parse_timestamp(str(row["heartbeat_at"])) if row["heartbeat_at"] else None
        ),
        lease_expires_at=parse_timestamp(str(row["lease_expires_at"])),
        closed_at=parse_timestamp(str(row["closed_at"])) if row["closed_at"] else None,
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        conversation_id=str(row["conversation_id"]),
        kind=ConversationKind(str(row["kind"])),
        participant_key=str(row["participant_key"]),
        created_by=str(row["created_by"]),
        created_at=parse_timestamp(str(row["created_at"])),
        updated_at=parse_timestamp(str(row["updated_at"])),
        closed_at=parse_timestamp(str(row["closed_at"])) if row["closed_at"] else None,
        blocked=bool(row["blocked"]),
        blocked_reason=_optional_text(row, "blocked_reason"),
        auto_turn_count=int(row["auto_turn_count"]),
        last_message_at=(
            parse_timestamp(str(row["last_message_at"])) if row["last_message_at"] else None
        ),
    )


def _participant_from_row(row: sqlite3.Row) -> ConversationParticipant:
    return ConversationParticipant(
        conversation_id=str(row["conversation_id"]),
        account_id=str(row["account_id"]),
        role=ParticipantRole(str(row["role"])),
        joined_at=parse_timestamp(str(row["joined_at"])),
        last_read_message_id=_optional_text(row, "last_read_message_id"),
        unread_count=int(row["unread_count"]),
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        message_id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        reply_to=_optional_text(row, "reply_to"),
        sender_account_id=str(row["sender_account_id"]),
        recipient_account_id=str(row["recipient_account_id"]),
        content_type=ContentType(str(row["content_type"])),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        idempotency_key=_optional_text(row, "idempotency_key"),
        auto_generated=bool(row["auto_generated"]),
        metadata=_json_object(row["metadata_json"]),
        created_at=parse_timestamp(str(row["created_at"])),
    )


def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        delivery_id=str(row["delivery_id"]),
        message_id=str(row["message_id"]),
        account_id=str(row["account_id"]),
        conversation_id=str(row["conversation_id"]),
        state=DeliveryState(str(row["state"])),
        attempt=int(row["attempt"]),
        content_hash=str(row["content_hash"]),
        created_at=parse_timestamp(str(row["created_at"])),
        updated_at=parse_timestamp(str(row["updated_at"])),
        acked_at=parse_timestamp(str(row["acked_at"])) if row["acked_at"] else None,
        injected_at=parse_timestamp(str(row["injected_at"])) if row["injected_at"] else None,
        notified_at=parse_timestamp(str(row["notified_at"])) if row["notified_at"] else None,
        next_attempt_at=(
            parse_timestamp(str(row["next_attempt_at"])) if row["next_attempt_at"] else None
        ),
        last_error=_optional_text(row, "last_error"),
    )


def _processing_from_row(row: sqlite3.Row) -> ProcessingRecord:
    return ProcessingRecord(
        message_id=str(row["message_id"]),
        account_id=str(row["account_id"]),
        state=ProcessingState(str(row["state"])),
        result=_optional_text(row, "result"),
        block_reason=_optional_text(row, "block_reason"),
        attempt_count=int(row["attempt_count"]),
        updated_at=parse_timestamp(str(row["updated_at"])),
    )


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------

_ACCOUNT_COLUMNS = (
    "account_id, host_type, host_instance_id, native_session_id, display_name, address, "
    "workspace_hint, capability_level, blocked, metadata_json, created_at, updated_at"
)

_CONNECTION_COLUMNS = (
    "connection_id, account_id, generation, state, is_current, durable, host_pid, "
    "capability_level, adapter_name, remote_hint, opened_at, heartbeat_at, "
    "lease_expires_at, closed_at"
)

_MESSAGE_COLUMNS = (
    "message_id, conversation_id, reply_to, sender_account_id, recipient_account_id, "
    "content_type, content, content_hash, idempotency_key, auto_generated, metadata_json, "
    "created_at"
)


class _AccountRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, account_id: str) -> Account | None:
        row = self._c.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return _account_from_row(row) if row else None

    def find_by_identity(self, identity: HostIdentity) -> Account | None:
        row = self._c.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts "
            "WHERE host_type = ? AND host_instance_id = ? AND native_session_id = ?",
            identity.key,
        ).fetchone()
        return _account_from_row(row) if row else None

    def list_accounts(
        self,
        *,
        host_type: str | None = None,
        presence: PresenceState | None = None,
        exclude_account_id: str | None = None,
        limit: int = 200,
    ) -> list[Account]:
        """列出账号。

        ``presence`` 过滤在 Python 侧完成：presence 取决于"托管进程此刻是否还活着"，
        无法用静态列表达。因此这里取候选后过滤，返回值不会超过 ``limit``。
        """
        sql = f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE deleted_at IS NULL"
        params: list[Any] = []
        if host_type:
            sql += " AND host_type = ?"
            params.append(host_type)
        if exclude_account_id:
            sql += " AND account_id <> ?"
            params.append(exclude_account_id)
        sql += " ORDER BY created_at ASC, account_id ASC LIMIT ?"
        params.append(max(limit, 1))
        rows = self._c.execute(sql, params).fetchall()
        accounts = [_account_from_row(row) for row in rows]
        if presence is None:
            return accounts
        states = presence_for_accounts(self._c, [a.account_id for a in accounts])
        return [a for a in accounts if states.get(a.account_id) is presence]

    def add(self, account: Account) -> None:
        try:
            self._c.execute(
                f"INSERT INTO accounts ({_ACCOUNT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account.account_id,
                    account.host_type,
                    account.host_instance_id,
                    account.native_session_id,
                    account.display_name,
                    account.address,
                    account.workspace_hint,
                    int(account.capability_level),
                    1 if account.blocked else 0,
                    _dump_json(account.metadata),
                    format_timestamp(account.created_at),
                    format_timestamp(account.updated_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"账号已存在或违反唯一约束：{exc}") from exc

    def update(self, account: Account) -> None:
        cursor = self._c.execute(
            "UPDATE accounts SET display_name = ?, address = ?, workspace_hint = ?, "
            "capability_level = ?, blocked = ?, metadata_json = ?, updated_at = ? "
            "WHERE account_id = ?",
            (
                account.display_name,
                account.address,
                account.workspace_hint,
                int(account.capability_level),
                1 if account.blocked else 0,
                _dump_json(account.metadata),
                format_timestamp(account.updated_at),
                account.account_id,
            ),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"账号不存在：{account.account_id}")

    def next_generation(self, account_id: str) -> int:
        row = self._c.execute(
            "SELECT COALESCE(MAX(generation), 0), "
            "(SELECT current_generation FROM accounts WHERE account_id = ?) "
            "FROM connections WHERE account_id = ?",
            (account_id, account_id),
        ).fetchone()
        if row is None:
            return 1
        return max(int(row[0]), int(row[1] or 0)) + 1

    def set_current_generation(self, account_id: str, generation: int, at: datetime) -> None:
        self._c.execute(
            "UPDATE accounts SET current_generation = ?, updated_at = ? WHERE account_id = ?",
            (generation, format_timestamp(at), account_id),
        )

    def touch(self, account_id: str, at: datetime) -> None:
        self._c.execute(
            "UPDATE accounts SET updated_at = ? WHERE account_id = ?",
            (format_timestamp(at), account_id),
        )


class _ConnectionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, connection_id: str) -> Connection | None:
        row = self._c.execute(
            f"SELECT {_CONNECTION_COLUMNS} FROM connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        return _connection_from_row(row) if row else None

    def current_for_account(self, account_id: str) -> Connection | None:
        row = self._c.execute(
            f"SELECT {_CONNECTION_COLUMNS} FROM connections "
            "WHERE account_id = ? AND is_current = 1",
            (account_id,),
        ).fetchone()
        return _connection_from_row(row) if row else None

    def list_for_account(
        self, account_id: str, *, include_closed: bool = False
    ) -> list[Connection]:
        sql = f"SELECT {_CONNECTION_COLUMNS} FROM connections WHERE account_id = ?"
        if not include_closed:
            sql += " AND state = 'active'"
        sql += " ORDER BY generation DESC"
        return [_connection_from_row(row) for row in self._c.execute(sql, (account_id,))]

    def add(self, connection: Connection) -> None:
        try:
            self._c.execute(
                f"INSERT INTO connections ({_CONNECTION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    connection.connection_id,
                    connection.account_id,
                    connection.generation,
                    connection.state.value,
                    1 if connection.is_current else 0,
                    1 if connection.durable else 0,
                    connection.host_pid,
                    int(connection.capability_level),
                    connection.adapter_name,
                    connection.remote_hint,
                    format_timestamp(connection.opened_at),
                    format_timestamp(connection.heartbeat_at) if connection.heartbeat_at else None,
                    format_timestamp(connection.lease_expires_at),
                    format_timestamp(connection.closed_at) if connection.closed_at else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"连接写入违反唯一约束：{exc}") from exc

    def update(self, connection: Connection) -> None:
        cursor = self._c.execute(
            "UPDATE connections SET state = ?, is_current = ?, durable = ?, host_pid = ?, "
            "capability_level = ?, heartbeat_at = ?, lease_expires_at = ?, closed_at = ? "
            "WHERE connection_id = ?",
            (
                connection.state.value,
                1 if connection.is_current else 0,
                1 if connection.durable else 0,
                connection.host_pid,
                int(connection.capability_level),
                format_timestamp(connection.heartbeat_at) if connection.heartbeat_at else None,
                format_timestamp(connection.lease_expires_at),
                format_timestamp(connection.closed_at) if connection.closed_at else None,
                connection.connection_id,
            ),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"连接不存在：{connection.connection_id}")

    def demote_current(self, account_id: str, *, state: ConnectionState, at: datetime) -> int:
        """降级当前主连接。

        ``is_current=0`` 必须在同一事务里完成，否则部分唯一索引会在插入新主连接时
        报冲突——这也正是我们想要的：靠数据库挡住"两个主连接"。
        """
        cursor = self._c.execute(
            "UPDATE connections SET is_current = 0, state = ?, closed_at = ? "
            "WHERE account_id = ? AND is_current = 1",
            (state.value, format_timestamp(at), account_id),
        )
        return cursor.rowcount

    def max_generation(self, account_id: str) -> int:
        row = self._c.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM connections WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_disconnected(self) -> list[str]:
        """回收"托管进程已经退出（或没有进程信息）"的活动连接。返回 connection_id 列表。

        在线严格参照进程，所以判定只有一条：

        * 有 ``host_pid``：进程还活着 -> **永不**因时间回收（会话在思考时不发心跳，
          按租约把活着的账号判离线是错的）；进程已退出 -> 回收。
        * 没有 ``host_pid``：无进程可参照，一律算离线 -> 直接回收。

        回收动作同时清掉 ``is_current``，让账号可以重新被接管。
        """
        from ...domain.process import is_process_alive

        rows = self._c.execute(
            "SELECT connection_id, host_pid FROM connections WHERE state = 'active'",
        ).fetchall()

        expired_ids: list[str] = []
        for row in rows:
            host_pid = row["host_pid"]
            if host_pid is None or not is_process_alive(int(host_pid)):
                expired_ids.append(str(row["connection_id"]))

        if not expired_ids:
            return []
        self._c.executemany(
            "UPDATE connections SET state = 'expired', is_current = 0 WHERE connection_id = ?",
            [(connection_id,) for connection_id in expired_ids],
        )
        return expired_ids


class _ConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._c.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return _conversation_from_row(row) if row else None

    def find_open_direct(self, participant_key: str) -> Conversation | None:
        row = self._c.execute(
            "SELECT * FROM conversations WHERE kind = 'direct' AND participant_key = ? "
            "AND closed_at IS NULL",
            (participant_key,),
        ).fetchone()
        return _conversation_from_row(row) if row else None

    def participants(self, conversation_id: str) -> list[ConversationParticipant]:
        rows = self._c.execute(
            "SELECT * FROM conversation_participants WHERE conversation_id = ? "
            "ORDER BY joined_at ASC, account_id ASC",
            (conversation_id,),
        ).fetchall()
        return [_participant_from_row(row) for row in rows]

    def participant(self, conversation_id: str, account_id: str) -> ConversationParticipant | None:
        row = self._c.execute(
            "SELECT * FROM conversation_participants WHERE conversation_id = ? AND account_id = ?",
            (conversation_id, account_id),
        ).fetchone()
        return _participant_from_row(row) if row else None

    def is_participant(self, conversation_id: str, account_id: str) -> bool:
        row = self._c.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND account_id = ?",
            (conversation_id, account_id),
        ).fetchone()
        return row is not None

    def list_for_account(
        self,
        account_id: str,
        *,
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[ConversationSummary], str | None]:
        """按最近活动倒序分页。

        排序键是 ``(COALESCE(last_message_at, created_at), conversation_id)``，
        用 keyset 游标而不是 OFFSET：OFFSET 在有新消息插入时会漏项或重复。
        ``created_at`` 只有秒以下精度，同一毫秒内建多个对话时排序会不稳定，
        因此游标里额外带上 conversation_id 作为并列时的次序。
        """
        params: list[Any] = [account_id]
        sql = [
            "SELECT c.*, p.unread_count AS unread_count, p.account_id AS viewer_account_id,",
            "  (SELECT m2.message_id FROM messages m2 WHERE m2.conversation_id = c.conversation_id",
            "     ORDER BY m2.rowid DESC LIMIT 1) AS last_message_id,",
            "  (SELECT m3.sender_account_id FROM messages m3 WHERE m3.conversation_id = c.conversation_id",
            "     ORDER BY m3.rowid DESC LIMIT 1) AS last_sender_id,",
            "  (SELECT m4.recipient_account_id FROM messages m4 WHERE m4.conversation_id = c.conversation_id",
            "     ORDER BY m4.rowid DESC LIMIT 1) AS last_recipient_id,",
            "  COALESCE(c.last_message_at, c.created_at) AS sort_key",
            "FROM conversations c",
            "JOIN conversation_participants p ON p.conversation_id = c.conversation_id",
            "WHERE p.account_id = ?",
        ]
        if unread_only:
            sql.append("AND p.unread_count > 0")
        if cursor:
            payload = _decode_cursor(cursor)
            sort_key = payload.get("k")
            conversation_id = payload.get("id")
            if not isinstance(sort_key, str) or not isinstance(conversation_id, str):
                raise ValidationError("分页游标无效，请从上一页返回值原样传入")
            sql.append("AND (COALESCE(c.last_message_at, c.created_at), c.conversation_id) < (?, ?)")
            params.extend([sort_key, conversation_id])
        sql.append("ORDER BY sort_key DESC, c.conversation_id DESC LIMIT ?")
        params.append(limit + 1)

        rows = self._c.execute("\n".join(sql), params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]

        items: list[ConversationSummary] = []
        for row in rows:
            conversation = _conversation_from_row(row)
            viewer = str(row["viewer_account_id"])
            counterpart = _counterpart(
                self._c,
                conversation.conversation_id,
                viewer,
                row["last_sender_id"],
                row["last_recipient_id"],
            )
            items.append(
                ConversationSummary(
                    conversation=conversation,
                    counterpart_account_id=counterpart,
                    unread_count=int(row["unread_count"]),
                    last_message_id=_optional_text(row, "last_message_id"),
                    last_message_at=conversation.last_message_at or conversation.created_at,
                )
            )
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(
                {
                    "k": format_timestamp(last.conversation.last_message_at or last.conversation.created_at),
                    "id": last.conversation.conversation_id,
                }
            )
        return items, next_cursor

    def add(self, conversation: Conversation) -> None:
        try:
            self._c.execute(
                "INSERT INTO conversations (conversation_id, kind, participant_key, created_by, "
                "created_at, updated_at, closed_at, blocked, blocked_reason, auto_turn_count, "
                "last_message_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.conversation_id,
                    conversation.kind.value,
                    conversation.participant_key,
                    conversation.created_by,
                    format_timestamp(conversation.created_at),
                    format_timestamp(conversation.updated_at),
                    format_timestamp(conversation.closed_at) if conversation.closed_at else None,
                    1 if conversation.blocked else 0,
                    conversation.blocked_reason,
                    conversation.auto_turn_count,
                    format_timestamp(conversation.last_message_at)
                    if conversation.last_message_at
                    else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"对话写入违反唯一约束：{exc}") from exc

    def update(self, conversation: Conversation) -> None:
        cursor = self._c.execute(
            "UPDATE conversations SET updated_at = ?, closed_at = ?, blocked = ?, "
            "blocked_reason = ?, auto_turn_count = ?, last_message_at = ? "
            "WHERE conversation_id = ?",
            (
                format_timestamp(conversation.updated_at),
                format_timestamp(conversation.closed_at) if conversation.closed_at else None,
                1 if conversation.blocked else 0,
                conversation.blocked_reason,
                conversation.auto_turn_count,
                format_timestamp(conversation.last_message_at)
                if conversation.last_message_at
                else None,
                conversation.conversation_id,
            ),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"对话不存在：{conversation.conversation_id}")

    def add_participant(self, participant: ConversationParticipant) -> None:
        try:
            self._c.execute(
                "INSERT INTO conversation_participants (conversation_id, account_id, role, "
                "joined_at, last_read_message_id, unread_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    participant.conversation_id,
                    participant.account_id,
                    participant.role.value,
                    format_timestamp(participant.joined_at),
                    participant.last_read_message_id,
                    participant.unread_count,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"对话成员写入违反唯一约束：{exc}") from exc

    def update_participant(self, participant: ConversationParticipant) -> None:
        cursor = self._c.execute(
            "UPDATE conversation_participants SET role = ?, last_read_message_id = ?, "
            "unread_count = ? WHERE conversation_id = ? AND account_id = ?",
            (
                participant.role.value,
                participant.last_read_message_id,
                participant.unread_count,
                participant.conversation_id,
                participant.account_id,
            ),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(
                f"对话成员不存在：{participant.conversation_id} / {participant.account_id}"
            )

    def mark_read(
        self,
        conversation_id: str,
        account_id: str,
        *,
        last_read_message_id: str | None,
        unread_count: int,
    ) -> None:
        self._c.execute(
            "UPDATE conversation_participants SET last_read_message_id = ?, unread_count = ? "
            "WHERE conversation_id = ? AND account_id = ?",
            (last_read_message_id, max(unread_count, 0), conversation_id, account_id),
        )

    def bump_unread(self, conversation_id: str, account_ids: Sequence[str], delta: int = 1) -> None:
        self._c.executemany(
            "UPDATE conversation_participants SET unread_count = MAX(0, unread_count + ?) "
            "WHERE conversation_id = ? AND account_id = ?",
            [(delta, conversation_id, account_id) for account_id in account_ids],
        )

    def bump_auto_turn(self, conversation_id: str, delta: int) -> int:
        """更新连续自动往返计数，返回新值。"""
        self._c.execute(
            "UPDATE conversations SET auto_turn_count = MAX(0, auto_turn_count + ?) "
            "WHERE conversation_id = ?",
            (delta, conversation_id),
        )
        row = self._c.execute(
            "SELECT auto_turn_count FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row["auto_turn_count"]) if row else 0


def _counterpart(
    connection: sqlite3.Connection,
    conversation_id: str,
    viewer: str,
    last_sender_id: str | None,
    last_recipient_id: str | None,
) -> str:
    """会话列表里显示的"对端"账号。

    直接对话只有两个参与者，所以取另一个即可；不要根据最后一条消息的方向推断，
    否则自己发的最后一条会让"对端"变成自己。
    """
    row = connection.execute(
        "SELECT account_id FROM conversation_participants "
        "WHERE conversation_id = ? AND account_id <> ? LIMIT 1",
        (conversation_id, viewer),
    ).fetchone()
    if row is not None:
        return str(row["account_id"])
    if last_sender_id and str(last_sender_id) != viewer:
        return str(last_sender_id)
    if last_recipient_id and str(last_recipient_id) != viewer:
        return str(last_recipient_id)
    return ""


class _MessageRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, message_id: str) -> Message | None:
        row = self._c.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return _message_from_row(row) if row else None

    def find_by_idempotency_key(
        self, *, sender_account_id: str, idempotency_key: str
    ) -> Message | None:
        row = self._c.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE sender_account_id = ? AND idempotency_key = ?",
            (sender_account_id, idempotency_key),
        ).fetchone()
        return _message_from_row(row) if row else None

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 50,
        viewer_account_id: str | None = None,
    ) -> MessagePage:
        """按时间正序分页，游标为不透明字符串。

        ``viewer_account_id`` 提供时一并返回三个状态维度（投递/可见性/处理）。
        """
        params: list[Any] = [conversation_id]
        sql = [
            "SELECT m.* FROM messages m WHERE m.conversation_id = ?",
        ]
        anchor_rowid: int | None = None
        if after_message_id == "last_read":
            # 显式"只看未读"：以本账号的已读位点为锚点，而不是猜消息 ID。
            participant = self._c.execute(
                "SELECT last_read_message_id FROM conversation_participants "
                "WHERE conversation_id = ? AND account_id = ?",
                (conversation_id, viewer_account_id),
            ).fetchone()
            if participant is not None and participant["last_read_message_id"]:
                anchor = self._c.execute(
                    "SELECT rowid FROM messages WHERE message_id = ?",
                    (str(participant["last_read_message_id"]),),
                ).fetchone()
                anchor_rowid = int(anchor["rowid"]) if anchor is not None else None
        elif after_message_id:
            anchor = self._c.execute(
                "SELECT rowid FROM messages WHERE message_id = ?",
                (after_message_id,),
            ).fetchone()
            if anchor is None:
                raise ValidationError(f"游标消息不存在：{after_message_id}")
            anchor_rowid = int(anchor["rowid"])
        if anchor_rowid is not None:
            sql.append("AND m.rowid > ?")
            params.append(anchor_rowid)
        sql.append("ORDER BY m.rowid ASC LIMIT ?")
        params.append(limit + 1)
        rows = self._c.execute(" ".join(sql), params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [_message_from_row(row) for row in rows]

        views: list[MessageView] = []
        for message in messages:
            views.append(self._view_for(message, viewer_account_id))
        next_cursor = messages[-1].message_id if (has_more and messages) else None
        return MessagePage(items=tuple(views), next_cursor=next_cursor)

    def list_pending_for_account(
        self, account_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> MessagePage:
        """按收件账号分页，过滤发生在 LIMIT 之前，不受历史或已读位点影响。"""
        params: list[Any] = [account_id, account_id]
        sql = [
            "SELECT m.* FROM messages m LEFT JOIN message_processing p "
            "ON p.message_id = m.message_id AND p.account_id = ? "
            "WHERE m.recipient_account_id = ? "
            "AND (p.state IS NULL OR p.state IN ('pending', 'running', 'blocked'))"
        ]
        if cursor:
            anchor = self._c.execute(
                "SELECT rowid FROM messages WHERE message_id = ? AND recipient_account_id = ?",
                (cursor, account_id),
            ).fetchone()
            if anchor is None:
                raise ValidationError("收件游标不存在或不属于本账号")
            sql.append("AND m.rowid > ?")
            params.append(int(anchor["rowid"]))
        size = max(1, min(limit, 100))
        sql.append("ORDER BY m.rowid ASC LIMIT ?")
        params.append(size + 1)
        rows = self._c.execute(" ".join(sql), params).fetchall()
        has_more = len(rows) > size
        messages = [_message_from_row(row) for row in rows[:size]]
        return MessagePage(
            items=tuple(self._view_for(message, account_id) for message in messages),
            next_cursor=messages[-1].message_id if has_more and messages else None,
        )

    def pending_count(self, account_id: str) -> int:
        row = self._c.execute(
            "SELECT COUNT(*) FROM messages m LEFT JOIN message_processing p "
            "ON p.message_id = m.message_id AND p.account_id = ? "
            "WHERE m.recipient_account_id = ? "
            "AND (p.state IS NULL OR p.state IN ('pending', 'running', 'blocked'))",
            (account_id, account_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def count_auto_turns(self, conversation_id: str, *, since: datetime) -> int:
        row = self._c.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND auto_generated = 1 "
            "AND created_at >= ?",
            (conversation_id, format_timestamp(since)),
        ).fetchone()
        return int(row[0]) if row else 0

    def unread_count(self, conversation_id: str, account_id: str) -> int:
        row = self._c.execute(
            "SELECT unread_count FROM conversation_participants "
            "WHERE conversation_id = ? AND account_id = ?",
            (conversation_id, account_id),
        ).fetchone()
        return int(row["unread_count"]) if row else 0

    def recent_content_hashes(self, conversation_id: str, *, limit: int = 10) -> list[str]:
        rows = self._c.execute(
            "SELECT content_hash FROM messages WHERE conversation_id = ? "
            "ORDER BY rowid DESC LIMIT ?",
            (conversation_id, max(limit, 1)),
        ).fetchall()
        return [str(row["content_hash"]) for row in rows]

    def _view_for(self, message: Message, viewer_account_id: str | None) -> MessageView:
        delivery_row = self._c.execute(
            "SELECT state FROM deliveries WHERE message_id = ? AND account_id = ?",
            (message.message_id, viewer_account_id),
        ).fetchone()
        processing_row = self._c.execute(
            "SELECT * FROM message_processing WHERE message_id = ? AND account_id = ?",
            (message.message_id, viewer_account_id),
        ).fetchone()
        visibility = VisibilityState.UNREAD
        if viewer_account_id:
            participant = self._c.execute(
                "SELECT last_read_message_id FROM conversation_participants "
                "WHERE conversation_id = ? AND account_id = ?",
                (message.conversation_id, viewer_account_id),
            ).fetchone()
            if participant is not None and participant["last_read_message_id"]:
                last_read = self._c.execute(
                    "SELECT rowid FROM messages WHERE message_id = ?",
                    (str(participant["last_read_message_id"]),),
                ).fetchone()
                own = self._c.execute(
                    "SELECT rowid FROM messages WHERE message_id = ?",
                    (message.message_id,),
                ).fetchone()
                if last_read is not None and own is not None:
                    if int(own["rowid"]) <= int(last_read["rowid"]):
                        visibility = VisibilityState.SEEN
        processing = (
            _processing_from_row(processing_row)
            if processing_row is not None
            else None
        )
        return MessageView(
            message=message,
            delivery=DeliveryState(str(delivery_row["state"])) if delivery_row else None,
            visibility=visibility,
            processing=processing.state if processing else ProcessingState.PENDING,
            processing_result=processing.result if processing else None,
        )

    def add(self, message: Message) -> None:
        try:
            self._c.execute(
                f"INSERT INTO messages ({_MESSAGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    message.conversation_id,
                    message.reply_to,
                    message.sender_account_id,
                    message.recipient_account_id,
                    message.content_type.value,
                    message.content,
                    message.content_hash,
                    message.idempotency_key,
                    1 if message.auto_generated else 0,
                    _dump_json(message.metadata),
                    format_timestamp(message.created_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"消息写入违反唯一约束：{exc}") from exc


class _DeliveryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, delivery_id: str) -> DeliveryRecord | None:
        row = self._c.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return _delivery_from_row(row) if row else None

    def find_for_message(self, message_id: str, account_id: str) -> DeliveryRecord | None:
        row = self._c.execute(
            "SELECT * FROM deliveries WHERE message_id = ? AND account_id = ?",
            (message_id, account_id),
        ).fetchone()
        return _delivery_from_row(row) if row else None

    def due_for_dispatch(self, *, now: datetime, limit: int = 100) -> list[DeliveryRecord]:
        """到期可派发：``queued`` 立即派发；``failed`` 要等退避时间到。"""
        cutoff = format_timestamp(now)
        rows = self._c.execute(
            "WITH due AS (SELECT d.*, ROW_NUMBER() OVER "
            "(PARTITION BY d.account_id ORDER BY d.delivery_seq) AS account_rank "
            "FROM deliveries d WHERE "
            "((d.state = 'queued' AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)) "
            "OR (d.state = 'failed' AND d.next_attempt_at IS NOT NULL AND d.next_attempt_at <= ?)) "
            "AND NOT EXISTS (SELECT 1 FROM message_processing p "
            "WHERE p.message_id = d.message_id AND p.account_id = d.account_id "
            "AND p.state IN ('completed', 'cancelled', 'failed'))) "
            "SELECT * FROM due ORDER BY account_rank, delivery_seq LIMIT ?",
            (cutoff, cutoff, limit),
        ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def pending_for_account(self, account_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        rows = self._c.execute(
            "SELECT d.* FROM deliveries d WHERE d.account_id = ? "
            "AND d.state IN ('queued', 'dispatched', 'failed') "
            "ORDER BY d.delivery_seq ASC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def stalled_dispatched(self, *, before: datetime, limit: int = 100) -> list[DeliveryRecord]:
        """已派发但超过 ``before`` 仍未确认的投递。

        覆盖"适配器在确认前断线"：退回重试时必须复用同一个 ``delivery_id``，
        接收方才能按它去重，不会重复注入。
        """
        rows = self._c.execute(
            "SELECT * FROM deliveries WHERE state = 'dispatched' AND updated_at < ? "
            "ORDER BY delivery_seq ASC LIMIT ?",
            (format_timestamp(before), max(limit, 1)),
        ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def count_recent_content(
        self, *, conversation_id: str, content_hash: str, since: datetime
    ) -> int:
        """同一对话在时间窗内出现过几次相同正文（循环检测）。"""
        row = self._c.execute(
            "SELECT COUNT(*) FROM deliveries WHERE conversation_id = ? AND content_hash = ? "
            "AND created_at >= ?",
            (conversation_id, content_hash, format_timestamp(since)),
        ).fetchone()
        return int(row[0]) if row else 0

    def add(self, delivery: DeliveryRecord) -> None:
        try:
            self._c.execute(
                "INSERT INTO deliveries (delivery_id, message_id, account_id, conversation_id, "
                "state, attempt, content_hash, created_at, updated_at, dispatched_at, acked_at, "
                "injected_at, notified_at, next_attempt_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.delivery_id,
                    delivery.message_id,
                    delivery.account_id,
                    delivery.conversation_id,
                    delivery.state.value,
                    delivery.attempt,
                    delivery.content_hash,
                    format_timestamp(delivery.created_at),
                    format_timestamp(delivery.updated_at),
                    format_timestamp(delivery.updated_at)
                    if delivery.state is DeliveryState.DISPATCHED
                    else None,
                    format_timestamp(delivery.acked_at) if delivery.acked_at else None,
                    format_timestamp(delivery.injected_at) if delivery.injected_at else None,
                    format_timestamp(delivery.notified_at) if delivery.notified_at else None,
                    format_timestamp(delivery.next_attempt_at) if delivery.next_attempt_at else None,
                    delivery.last_error,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # (message_id, account_id) 唯一：同一消息对同一账号只会有一条投递。
            raise ValidationError(f"投递写入违反唯一约束：{exc}") from exc

    def update(self, delivery: DeliveryRecord) -> None:
        cursor = self._c.execute(
            "UPDATE deliveries SET state = ?, attempt = ?, updated_at = ?, dispatched_at = ?, "
            "acked_at = ?, injected_at = ?, notified_at = ?, next_attempt_at = ?, last_error = ? "
            "WHERE delivery_id = ?",
            (
                delivery.state.value,
                delivery.attempt,
                format_timestamp(delivery.updated_at),
                format_timestamp(delivery.updated_at)
                if delivery.state is DeliveryState.DISPATCHED
                else None,
                format_timestamp(delivery.acked_at) if delivery.acked_at else None,
                format_timestamp(delivery.injected_at) if delivery.injected_at else None,
                format_timestamp(delivery.notified_at) if delivery.notified_at else None,
                format_timestamp(delivery.next_attempt_at) if delivery.next_attempt_at else None,
                delivery.last_error,
                delivery.delivery_id,
            ),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"投递不存在：{delivery.delivery_id}")

    def claim_for_dispatch(self, delivery_id: str, *, at: datetime) -> bool:
        """原子抢占：只有处于 ``queued`` / ``failed`` 的行能被抢占并 ``attempt+1``。

        返回 ``False`` 表示已被其他派发者抢占（或状态已变），调用方必须放弃本次派发。
        这是"至少一次"里防止重复派发的关键一步。
        """
        cursor = self._c.execute(
            "UPDATE deliveries SET state = 'dispatched', attempt = attempt + 1, "
            "updated_at = ?, dispatched_at = ?, next_attempt_at = NULL "
            "WHERE delivery_id = ? AND "
            "((state = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
            "OR (state = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?)) "
            "AND NOT EXISTS (SELECT 1 FROM message_processing p "
            "WHERE p.message_id = deliveries.message_id AND p.account_id = deliveries.account_id "
            "AND p.state IN ('completed', 'cancelled', 'failed'))",
            (format_timestamp(at), format_timestamp(at), delivery_id,
             format_timestamp(at), format_timestamp(at)),
        )
        return cursor.rowcount == 1

    def defer_offline(self, delivery_id: str, *, at: datetime, until: datetime) -> None:
        """Defer an offline target without claiming it or consuming an attempt."""
        self._c.execute(
            "UPDATE deliveries SET next_attempt_at = ?, updated_at = ?, "
            "last_error = 'offline: waiting for reconnect' "
            "WHERE delivery_id = ? AND state IN ('queued', 'failed') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (format_timestamp(until), format_timestamp(at), delivery_id, format_timestamp(at)),
        )

    def resume_offline(self, account_id: str, *, at: datetime) -> None:
        """A new binding makes deferred offline deliveries immediately eligible."""
        self._c.execute(
            "UPDATE deliveries SET next_attempt_at = ?, last_error = NULL "
            "WHERE account_id = ? AND state IN ('queued', 'failed') "
            "AND last_error = 'offline: waiting for reconnect'",
            (format_timestamp(at), account_id),
        )


class _ProcessingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def get(self, message_id: str, account_id: str) -> ProcessingRecord | None:
        row = self._c.execute(
            "SELECT * FROM message_processing WHERE message_id = ? AND account_id = ?",
            (message_id, account_id),
        ).fetchone()
        return _processing_from_row(row) if row else None

    def upsert(self, record: ProcessingRecord) -> None:
        self._c.execute(
            "INSERT INTO message_processing (message_id, account_id, state, result, block_reason, "
            "attempt_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (message_id, account_id) DO UPDATE SET state = excluded.state, "
            "result = excluded.result, block_reason = excluded.block_reason, "
            "attempt_count = message_processing.attempt_count + 1, "
            "updated_at = excluded.updated_at",
            (
                record.message_id,
                record.account_id,
                record.state.value,
                record.result,
                record.block_reason,
                record.attempt_count,
                format_timestamp(record.updated_at),
            ),
        )


# ---------------------------------------------------------------------------
# 工作单元
# ---------------------------------------------------------------------------


class _UnitOfWorkCore:
    """仓储集合 + 事务边界。读接口与写接口共享同一批实现对象。"""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._depth = 0
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._database.connection()
        return self._connection

    def __enter__(self) -> "_UnitOfWorkCore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        return None


class SqliteUnitOfWork(_UnitOfWorkCore):
    """在工作单元内暴露仓储，并管理事务。

    ``transaction()`` 可重入：应用服务组合多个用例时外层提交，内层只跟随。
    """

    def __init__(self, database: Database) -> None:
        super().__init__(database)
        connection = database.connection()
        self.accounts = _AccountRepository(connection)
        self.connections = _ConnectionRepository(connection)
        self.conversations = _ConversationRepository(connection)
        self.deliveries = _DeliveryRepository(connection)
        self.messages = _MessageRepository(connection)
        self.processing = _ProcessingRepository(connection)
        self.audit = _AuditRepository(connection)
        self.contacts = SqliteContactRepository(connection)
        self.rate_counters = RateCounterRepository(connection)

    def transaction(self) -> "_TransactionScope":
        return _TransactionScope(self)


class _TransactionScope:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self._context = None

    def __enter__(self) -> SqliteUnitOfWork:
        uow = self._uow
        if uow._depth == 0:
            uow._connection = uow._database.connection()
            self._context = uow._database.transaction()
            self._context.__enter__()
        uow._depth += 1
        return uow

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        uow = self._uow
        uow._depth -= 1
        if uow._depth == 0 and self._context is not None:
            context, self._context = self._context, None
            try:
                return context.__exit__(exc_type, exc, tb)
            finally:
                uow._connection = None
        return None


class _AuditRepository:
    """审计事件写入。只追加，不修改。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection

    def append(
        self,
        *,
        event_type: str,
        created_at: datetime,
        account_id: str | None = None,
        connection_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        delivery_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._c.execute(
            "INSERT INTO audit_events (event_type, account_id, connection_id, conversation_id, "
            "message_id, delivery_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                account_id,
                connection_id,
                conversation_id,
                message_id,
                delivery_id,
                _dump_json(detail or {}),
                format_timestamp(created_at),
            ),
        )

    def recent(self, *, limit: int = 100, event_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: list[Any] = []
        if event_type:
            sql += " WHERE event_type = ?"
            params.append(event_type)
        sql += " ORDER BY audit_id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._c.execute(sql, params)]


class SqliteUnitOfWorkFactory:
    """每请求一个工作单元。Broker 持有它，服务按需创建。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    def __call__(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self._database)
