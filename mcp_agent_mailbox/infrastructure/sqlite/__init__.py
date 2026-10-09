"""SQLite 权威存储。

对外只暴露四件事：打开数据库、应用迁移、查询 schema 版本、创建工作单元。
"""

from __future__ import annotations

from pathlib import Path

from .database import BUSY_TIMEOUT_MS, Database, DatabaseError, connect
from .migrations import MIGRATIONS, Migration, MigrationError, apply_migrations, schema_version
from .repositories import SqliteUnitOfWork, SqliteUnitOfWorkFactory

__all__ = [
    "BUSY_TIMEOUT_MS",
    "MIGRATIONS",
    "Database",
    "DatabaseError",
    "Migration",
    "MigrationError",
    "SqliteUnitOfWork",
    "SqliteUnitOfWorkFactory",
    "apply_migrations",
    "connect",
    "initialize",
    "schema_version",
]


def initialize(path: str | Path) -> Database:
    """打开数据库并确保 schema 是最新的。

    Broker 启动与 CLI 迁移命令都走这里；重复调用是幂等的。
    """
    database = Database(path)
    apply_migrations(database.connection())
    return database
