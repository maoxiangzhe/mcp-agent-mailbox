"""迁移必须可重复执行、可从空库初始化、失败要回滚。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_agent_mailbox.infrastructure.sqlite import (
    MIGRATIONS,
    Database,
    apply_migrations,
    connect,
    initialize,
    schema_version,
)
from mcp_agent_mailbox.infrastructure.sqlite import migrations as migrations_module
from mcp_agent_mailbox.infrastructure.sqlite.migrations import Migration, MigrationError

CORE_TABLES = {
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
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_registry_versions_are_strictly_increasing() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1, "第一个迁移必须从 v1 开始，便于空库初始化"


def test_empty_database_initializes_to_latest(db_path: Path) -> None:
    database = Database(db_path)
    connection = database.connection()
    assert schema_version(connection) == 0
    applied = apply_migrations(connection)
    assert applied == [migration.version for migration in MIGRATIONS]
    assert schema_version(connection) == MIGRATIONS[-1].version
    assert CORE_TABLES <= _tables(connection)
    database.close()


def test_migrations_are_idempotent(db_path: Path) -> None:
    database = initialize(db_path)
    first = schema_version(database.connection())
    assert apply_migrations(database.connection()) == []
    assert schema_version(database.connection()) == first
    database.close()


def test_wal_and_foreign_keys_are_enabled(db_path: Path) -> None:
    database = initialize(db_path)
    connection = database.connection()
    assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) > 0
    database.close()


def test_foreign_keys_reject_orphans(database) -> None:
    connection = database.connection()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO connections (connection_id, account_id, generation, state, is_current, "
            "capability_level, opened_at, lease_expires_at) "
            "VALUES ('con_x', 'acc_missing', 1, 'active', 1, 2, '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:01:00+00:00')"
        )


def test_identity_unique_constraint_blocks_duplicate_account(database) -> None:
    connection = database.connection()
    row = (
        "acc_1", "dsh", "desktop-default", "session-1", "DSH / 测试", "dsh:测试@desktop-default",
        2, 0, "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
    )
    sql = (
        "INSERT INTO accounts (account_id, host_type, host_instance_id, native_session_id, "
        "display_name, address, capability_level, blocked, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    connection.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("acc_2", *row[1:]))


def test_only_one_current_connection_per_account(database) -> None:
    connection = database.connection()
    connection.execute(
        "INSERT INTO accounts (account_id, host_type, host_instance_id, native_session_id, "
        "display_name, address, created_at, updated_at) VALUES "
        "('acc_1', 'dsh', 'i', 's', 'n', 'a', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    insert = (
        "INSERT INTO connections (connection_id, account_id, generation, state, is_current, "
        "capability_level, opened_at, lease_expires_at) VALUES (?, 'acc_1', ?, 'active', 1, 2, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00')"
    )
    connection.execute(insert, ("con_1", 1))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("con_2", 2))


def test_only_one_open_direct_conversation_per_pair(database) -> None:
    connection = database.connection()
    for account_id in ("acc_a", "acc_b"):
        connection.execute(
            "INSERT INTO accounts (account_id, host_type, host_instance_id, native_session_id, "
            "display_name, address, created_at, updated_at) VALUES "
            f"('{account_id}', 'dsh', 'i', '{account_id}', 'n', 'a', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    insert = (
        "INSERT INTO conversations (conversation_id, kind, participant_key, created_by, "
        "created_at, updated_at) VALUES (?, 'direct', 'acc_a|acc_b', 'acc_a', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(insert, ("conv_1",))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("conv_2",))
    # 关闭后允许再开一条，历史记录保留。
    connection.execute(
        "UPDATE conversations SET closed_at = '2026-01-02T00:00:00+00:00' WHERE conversation_id = 'conv_1'"
    )
    connection.execute(insert, ("conv_2",))


def test_message_idempotency_index_is_partial(database) -> None:
    """无 idempotency_key 的消息不受唯一约束限制（NULL 不参与部分索引）。"""
    connection = database.connection()
    for account_id in ("acc_a", "acc_b"):
        connection.execute(
            "INSERT INTO accounts (account_id, host_type, host_instance_id, native_session_id, "
            "display_name, address, created_at, updated_at) VALUES "
            f"('{account_id}', 'dsh', 'i', '{account_id}', 'n', 'a', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    connection.execute(
        "INSERT INTO conversations (conversation_id, kind, participant_key, created_by, "
        "created_at, updated_at) VALUES ('conv_1', 'direct', 'acc_a|acc_b', 'acc_a', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    insert = (
        "INSERT INTO messages (message_id, conversation_id, sender_account_id, "
        "recipient_account_id, content, content_hash, idempotency_key, created_at) "
        "VALUES (?, 'conv_1', 'acc_a', 'acc_b', 'hi', 'h', ?, '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(insert, ("msg_1", None))
    connection.execute(insert, ("msg_2", None))  # 允许：NULL 不冲突
    connection.execute(insert, ("msg_3", "key-1"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("msg_4", "key-1"))


def test_migration_failure_rolls_back_and_keeps_version(db_path: Path) -> None:
    """失败的迁移必须回滚，且不写入版本记录。

    通过临时插入一个会失败的迁移来验证：前一个迁移保留，坏迁移不留半张表。
    """
    database = initialize(db_path)
    connection = database.connection()
    before = schema_version(connection)

    bad = Migration(
        version=MIGRATIONS[-1].version + 1,
        name="broken",
        statements=(
            "CREATE TABLE half_baked (id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatched = (*MIGRATIONS, bad)
    original = migrations_module.MIGRATIONS
    migrations_module.MIGRATIONS = monkeypatched
    try:
        with pytest.raises(MigrationError):
            apply_migrations(connection)
    finally:
        migrations_module.MIGRATIONS = original

    assert schema_version(connection) == before
    assert "half_baked" not in _tables(connection)
    assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    database.close()


def test_registry_self_check_rejects_duplicate_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    duplicate = (MIGRATIONS[0], Migration(version=1, name="dup", statements=("SELECT 1",)))
    monkeypatch.setattr(migrations_module, "MIGRATIONS", duplicate)
    connection = connect(":memory:")
    try:
        with pytest.raises(MigrationError):
            apply_migrations(connection)
    finally:
        connection.close()
