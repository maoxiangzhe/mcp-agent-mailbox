"""数据库迁移。

规则（设计文档 §12 与任务要求）：

- 版本**单调递增**，只追加不修改；已发布的迁移永远不得改写；
- 每个迁移单独在一个 ``BEGIN IMMEDIATE`` 事务里执行，**失败即回滚**，
  绝不留半张表；
- 支持从空数据库初始化（v1 建全部核心表）；
- 能检测当前版本，且重复调用是幂等的；
- 有自动化测试（``tests/test_migrations.py``）。

迁移 SQL 全部写成常量，便于测试对"最终 schema"做断言。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ...domain.timestamps import format_timestamp, utc_now

__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "schema_version",
    "pending_versions",
]


class MigrationError(RuntimeError):
    """迁移失败（SQL 错误、版本表损坏、版本回退）。"""


@dataclass(frozen=True, slots=True)
class Migration:
    """一个不可变的迁移步骤。"""

    version: int
    name: str
    statements: tuple[str, ...]


# ---------------------------------------------------------------------------
# v1：核心表
# ---------------------------------------------------------------------------

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE accounts (
        account_id          TEXT PRIMARY KEY,
        host_type           TEXT NOT NULL,
        host_instance_id    TEXT NOT NULL,
        native_session_id   TEXT NOT NULL,
        display_name        TEXT NOT NULL,
        address             TEXT NOT NULL DEFAULT '',
        workspace_hint      TEXT,
        capability_level    INTEGER NOT NULL DEFAULT 0
                            CHECK (capability_level BETWEEN 0 AND 2),
        blocked             INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
        -- 投影列：由 connections 推导，允许重建，不作为权威事实。
        -- 刻意不存 current_presence：presence 依赖"当前时刻 vs 租约"，存下来必然过期，
        -- 一律在读路径按租约重算（见 domain.presence.compute_presence）。
        current_generation  INTEGER NOT NULL DEFAULT 0 CHECK (current_generation >= 0),
        metadata_json       TEXT NOT NULL DEFAULT '{}',
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        deleted_at          TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_accounts_identity
      ON accounts (host_type, host_instance_id, native_session_id)
    """,
    "CREATE INDEX ix_accounts_host_type ON accounts (host_type, current_generation)",
    "CREATE INDEX ix_accounts_address ON accounts (address)",
    """
    CREATE TABLE connections (
        connection_id       TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        generation          INTEGER NOT NULL CHECK (generation > 0),
        state               TEXT NOT NULL
                            CHECK (state IN ('active', 'closed', 'superseded', 'expired')),
        -- 每个账号最多一个当前主连接（部分唯一索引保证）。
        is_current          INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
        capability_level    INTEGER NOT NULL DEFAULT 0
                            CHECK (capability_level BETWEEN 0 AND 2),
        adapter_name        TEXT,
        remote_hint         TEXT,
        opened_at           TEXT NOT NULL,
        heartbeat_at        TEXT,
        lease_expires_at    TEXT NOT NULL,
        closed_at           TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_connections_current
      ON connections (account_id) WHERE is_current = 1
    """,
    "CREATE UNIQUE INDEX ux_connections_generation ON connections (account_id, generation)",
    "CREATE INDEX ix_connections_lease ON connections (state, lease_expires_at)",
    """
    CREATE TABLE conversations (
        conversation_id     TEXT PRIMARY KEY,
        kind                TEXT NOT NULL CHECK (kind IN ('direct')),
        -- 排序后的 "账号A|账号B"；与顺序无关，供唯一约束使用。
        participant_key     TEXT NOT NULL,
        created_by          TEXT NOT NULL REFERENCES accounts (account_id),
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        closed_at           TEXT,
        blocked             INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
        blocked_reason      TEXT,
        auto_turn_count     INTEGER NOT NULL DEFAULT 0 CHECK (auto_turn_count >= 0),
        last_message_at     TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_conversations_open_direct
      ON conversations (kind, participant_key)
      WHERE closed_at IS NULL
    """,
    """
    CREATE TABLE conversation_participants (
        conversation_id       TEXT NOT NULL
                              REFERENCES conversations (conversation_id) ON DELETE CASCADE,
        account_id            TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        role                  TEXT NOT NULL CHECK (role IN ('initiator', 'peer')),
        joined_at             TEXT NOT NULL,
        last_read_message_id  TEXT,
        unread_count          INTEGER NOT NULL DEFAULT 0 CHECK (unread_count >= 0),
        PRIMARY KEY (conversation_id, account_id)
    )
    """,
    "CREATE INDEX ix_participants_account ON conversation_participants (account_id)",
    """
    CREATE TABLE messages (
        -- rowid 就是入队顺序：同一毫秒内创建的消息靠它严格排序，比时间戳可靠。
        message_id            TEXT PRIMARY KEY,
        conversation_id       TEXT NOT NULL
                              REFERENCES conversations (conversation_id) ON DELETE CASCADE,
        reply_to              TEXT REFERENCES messages (message_id) ON DELETE SET NULL,
        sender_account_id     TEXT NOT NULL REFERENCES accounts (account_id),
        recipient_account_id  TEXT NOT NULL REFERENCES accounts (account_id),
        content_type          TEXT NOT NULL DEFAULT 'text/plain',
        content               TEXT NOT NULL,
        content_hash          TEXT NOT NULL,
        idempotency_key       TEXT,
        auto_generated        INTEGER NOT NULL DEFAULT 0 CHECK (auto_generated IN (0, 1)),
        metadata_json         TEXT NOT NULL DEFAULT '{}',
        created_at            TEXT NOT NULL,
        CHECK (sender_account_id <> recipient_account_id)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_messages_idempotency
      ON messages (sender_account_id, idempotency_key)
      WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE INDEX ix_messages_conversation
      ON messages (conversation_id, created_at, message_id)
    """,
    "CREATE INDEX ix_messages_recipient ON messages (recipient_account_id, created_at)",
    """
    CREATE TABLE deliveries (
        -- 单调递增的入队序号：至少一次投递的补投必须严格按入队顺序，
        -- 而 created_at 在同一毫秒内会并列、delivery_id 是随机的，都不能用来排序。
        delivery_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id         TEXT NOT NULL UNIQUE,
        message_id          TEXT NOT NULL REFERENCES messages (message_id) ON DELETE CASCADE,
        account_id          TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        conversation_id     TEXT NOT NULL
                            REFERENCES conversations (conversation_id) ON DELETE CASCADE,
        state               TEXT NOT NULL
                            CHECK (state IN ('queued', 'dispatched', 'delivered',
                                             'failed', 'dead_letter')),
        attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        -- 冗余保存正文哈希：循环检测要在"消息上下文"里看重复内容，而这里能一次查完。
        content_hash        TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        dispatched_at       TEXT,
        acked_at            TEXT,
        injected_at         TEXT,
        -- 最近一次"只通知不唤醒"（Level 1 通道）的时间；用于避免重复打扰。
        notified_at         TEXT,
        next_attempt_at     TEXT,
        last_error          TEXT,
        UNIQUE (message_id, account_id)
    )
    """,
    "CREATE INDEX ix_deliveries_due ON deliveries (state, next_attempt_at)",
    "CREATE INDEX ix_deliveries_account ON deliveries (account_id, state)",
    "CREATE INDEX ix_deliveries_message ON deliveries (message_id)",
    """
    CREATE TABLE message_processing (
        message_id          TEXT NOT NULL REFERENCES messages (message_id) ON DELETE CASCADE,
        account_id          TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        state               TEXT NOT NULL
                            CHECK (state IN ('pending', 'running', 'completed',
                                             'blocked', 'cancelled', 'failed')),
        result              TEXT,
        block_reason        TEXT,
        attempt_count       INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
        updated_at          TEXT NOT NULL,
        PRIMARY KEY (message_id, account_id)
    )
    """,
    """
    CREATE TABLE adapter_checkpoints (
        account_id          TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        adapter_name        TEXT NOT NULL,
        -- 适配器自己的进度（例如已处理的 delivery_id），保证重启后不重复注入。
        checkpoint_json     TEXT NOT NULL DEFAULT '{}',
        updated_at          TEXT NOT NULL,
        PRIMARY KEY (account_id, adapter_name)
    )
    """,
    """
    CREATE TABLE audit_events (
        audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type          TEXT NOT NULL,
        account_id          TEXT,
        connection_id       TEXT,
        conversation_id     TEXT,
        message_id          TEXT,
        delivery_id         TEXT,
        detail_json         TEXT NOT NULL DEFAULT '{}',
        created_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_audit_type_time ON audit_events (event_type, created_at)",
    "CREATE INDEX ix_audit_conversation ON audit_events (conversation_id, created_at)",
    """
    CREATE TABLE account_contacts (
        owner_account_id    TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        contact_account_id  TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
        policy              TEXT NOT NULL CHECK (policy IN ('allow', 'block')),
        note                TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        PRIMARY KEY (owner_account_id, contact_account_id),
        CHECK (owner_account_id <> contact_account_id)
    )
    """,
    "CREATE INDEX ix_contacts_policy ON account_contacts (owner_account_id, policy)",
    """
    CREATE TABLE rate_counters (
        scope_key     TEXT NOT NULL,
        window_start  TEXT NOT NULL,
        count         INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (scope_key, window_start)
    )
    """,
)


# ---------------------------------------------------------------------------
# v2：持久可达连接
# ---------------------------------------------------------------------------

# 背景：v1 只按租约判在线。对 Level 0 客户端（只有 MCP 工具入口、没有任何实时事件
# 通道）这行不通——它没有心跳可发，注册后 60 秒就掉线，对端发信时目标永远离线，
# 系统不可用。修法是承认这类连接"进程活着即可寻址"：
#     durable=1 -> 不按租约过期；presence 仍只报 connected（绝不报 realtime）。
# 只加列，不动 v1（v1 可能已经在真实库上跑过了）。
_V2_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE connections ADD COLUMN durable INTEGER NOT NULL DEFAULT 0 "
    "CHECK (durable IN (0, 1))",
    # 已有的 Level 0 活动连接按新语义回填，避免升级后旧账号依旧判离线。
    "UPDATE connections SET durable = 1 WHERE capability_level = 0 AND state = 'active'",
)


# ---------------------------------------------------------------------------
# v3：按宿主进程判定在线
# ---------------------------------------------------------------------------

# 产品规则：账号在线严格参照进程——托管该会话的进程活着就算在线，进程退出就算离线。
# 所以连接要记住"是谁在托管我"（PID）。租约退居兜底：只有拿不到进程信息的适配器才用它。
_V3_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE connections ADD COLUMN host_pid INTEGER",
    "CREATE INDEX ix_connections_host_pid ON connections (host_pid)",
)


# ---------------------------------------------------------------------------
# v4：进程信息成为唯一权威，取消"durable 永久在线"的兜底
# ---------------------------------------------------------------------------

# v2 的 durable 是在只有租约模型时加的兜底：没有实时通道的客户端不该按租约掉线。
# 现在在线由**托管进程是否活着**决定（v3 的 host_pid），durable 反而会让"没有进程信息
# 的连接"永久显示在线——那是在谎报。所以把存量回填成 0：没有进程信息就按租约判定，
# 该离线就离线。
_V4_STATEMENTS: tuple[str, ...] = (
    "UPDATE connections SET durable = 0 WHERE durable = 1",
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="core_schema", statements=_V1_STATEMENTS),
    Migration(version=2, name="durable_connections", statements=_V2_STATEMENTS),
    Migration(version=3, name="process_based_presence", statements=_V3_STATEMENTS),
    Migration(version=4, name="process_authoritative", statements=_V4_STATEMENTS),
)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _ensure_version_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            checksum    TEXT NOT NULL DEFAULT ''
        )
        """
    )


def schema_version(connection: sqlite3.Connection) -> int:
    """当前已应用的最高版本；空数据库返回 0。"""
    _ensure_version_table(connection)
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def pending_versions(connection: sqlite3.Connection) -> list[int]:
    """尚未应用的迁移版本，升序。"""
    current = schema_version(connection)
    return [m.version for m in MIGRATIONS if m.version > current]


def apply_migrations(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> list[int]:
    """应用所有待执行迁移，返回实际执行的版本列表。

    每个迁移一个事务：失败时该迁移回滚，先前成功的迁移保留，版本表保持一致。
    因此重复调用不会重复执行，也不会留下半成品 schema。
    """
    _validate_registry()
    applied: list[int] = []
    _ensure_version_table(connection)
    current = schema_version(connection)

    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        _apply_one(connection, migration, now=now)
        applied.append(migration.version)
    return applied


def _apply_one(
    connection: sqlite3.Connection, migration: Migration, *, now: datetime | None
) -> None:
    if connection.in_transaction:
        raise MigrationError("迁移开始前连接不应处于事务中")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
            "VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                format_timestamp(now or utc_now()),
                _checksum(migration),
            ),
        )
    except BaseException as exc:
        connection.execute("ROLLBACK")
        raise MigrationError(
            f"迁移 v{migration.version}（{migration.name}）失败，已回滚：{exc}"
        ) from exc
    else:
        connection.execute("COMMIT")


def _checksum(migration: Migration) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(f"{migration.version}:{migration.name}".encode("utf-8"))
    for statement in migration.statements:
        digest.update(statement.encode("utf-8"))
    return digest.hexdigest()[:32]


def _validate_registry() -> None:
    """注册表自检：版本必须严格递增且不重复。

    这条检查放在运行时（而不是只在测试里）是因为版本号错乱会静默跳过迁移，
    属于会毁数据的一类错误，必须在每次启动时挡住。
    """
    seen: set[int] = set()
    previous = 0
    for migration in MIGRATIONS:
        if migration.version in seen:
            raise MigrationError(f"迁移版本重复：v{migration.version}")
        if migration.version <= previous:
            raise MigrationError(
                f"迁移版本必须严格递增：v{migration.version} 出现在 v{previous} 之后"
            )
        if not migration.statements:
            raise MigrationError(f"迁移 v{migration.version} 没有任何语句")
        seen.add(migration.version)
        previous = migration.version
