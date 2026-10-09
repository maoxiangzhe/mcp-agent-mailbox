"""SQLite 连接与事务管理。

设计要点（对应设计文档 §12）：

- **WAL**：允许多个 MCP 端点／适配器并发读、单写者写，读不阻塞写；
- **外键**：SQLite 默认关闭外键，必须每条连接显式打开，否则孤儿数据不会报错；
- **显式事务**：写事务统一用 ``BEGIN IMMEDIATE``，避免"读事务升级为写事务"时
  出现 ``SQLITE_BUSY`` 死锁；``busy_timeout`` 让并发写在锁竞争时排队而不是立刻失败；
- **权威唯一性交给数据库约束**：账号唯一键、路由幂等键、同一对账号只允许一个未关闭
  的直接对话、每个账号最多一个当前主连接，全部用唯一索引表达，而不是靠应用层先查后写
  （先查后写在并发下必然出现重复）。

本模块不包含任何业务规则，只负责连接、pragma 与事务边界。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["Database", "DatabaseError", "connect"]

# 并发写等待锁的时间。超过就抛 OperationalError，由调用方决定重试。
BUSY_TIMEOUT_MS = 5000


class DatabaseError(RuntimeError):
    """数据库层错误（路径、pragma、事务状态异常）。"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开一个配置好的连接。

    调用方负责关闭。所有 pragma 都是**每连接**生效的，因此不能只设一次。
    """
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        target,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # 自己管事务，不用 sqlite3 的隐式事务
        check_same_thread=False,  # 连接可能被 MCP 请求线程复用，锁由本模块负责
    )
    connection.row_factory = sqlite3.Row
    _apply_pragmas(connection, target)
    return connection


def _apply_pragmas(connection: sqlite3.Connection, target: str) -> None:
    cursor = connection.cursor()
    try:
        # WAL 只对文件数据库有意义；内存库保持默认 journal。
        if target != ":memory:":
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA trusted_schema=OFF")
    finally:
        cursor.close()


class Database:
    """一个数据库文件的连接持有者。

    线程安全策略：每个线程各自持有一个连接（``threading.local``），避免 sqlite3
    连接被多线程交叉使用导致的未定义行为；同一线程内的并发通过实例锁串行化事务。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._tx_lock = threading.RLock()
        self._closed = False

    # -- 连接管理 ---------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """当前线程的连接，按需创建。"""
        if self._closed:
            raise DatabaseError("数据库已关闭")
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = connect(self.path)
            self._local.connection = existing
        return existing

    def close(self) -> None:
        """关闭当前线程的连接（其他线程的连接各自关闭）。"""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None
        self._closed = True

    # -- 事务 -------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：``BEGIN IMMEDIATE`` ... ``COMMIT`` / ``ROLLBACK``。

        嵌套调用会复用外层事务（应用服务可能组合多个用例），内层不提交。
        """
        connection = self.connection()
        with self._tx_lock:
            if connection.in_transaction:
                yield connection
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读路径：不需要显式事务（单条语句本身是原子的），但提供统一入口。"""
        yield self.connection()

    # -- 维护 -------------------------------------------------------------

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        """把 WAL 合并回主库。诊断命令与测试收尾用。"""
        if self.path == ":memory:":
            return
        cursor = self.connection().cursor()
        try:
            cursor.execute(f"PRAGMA wal_checkpoint({mode})")
        finally:
            cursor.close()

    def integrity_check(self) -> str:
        """返回 ``ok`` 或损坏说明。"""
        cursor = self.connection().cursor()
        try:
            row = cursor.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row else "unknown"
        finally:
            cursor.close()
