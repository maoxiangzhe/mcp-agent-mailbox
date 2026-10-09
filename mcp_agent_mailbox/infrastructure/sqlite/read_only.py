"""严格只读的 SQLite 访问。

监控台必须在**任何**情况下都不写数据库，包括"加载页面"这种看似无害的动作。
普通连接做不到这一点：建连时会执行 ``PRAGMA journal_mode=WAL`` 之类的语句，
这些语句会申请写锁甚至创建 ``-wal``/``-shm`` 文件。因此这里单独开一条
``mode=ro`` 连接，并且只设置不产生写入的 pragma。

三重保证：

1. ``file:...?mode=ro`` —— 由 SQLite 自己拒绝任何写语句；
2. ``query_only=ON`` —— 再兜一层，连临时表写入也拒绝（``mode=ro`` 在部分版本上
   允许写 temp）；
3. ``immutable`` 默认**不**开启：数据库确实会被其他进程并发写，必须让 SQLite
   正常参与锁协议并读到最新已提交数据。

所有读路径都只走这里，因此"监控台不写业务状态"是可执行验证的事实，而不是承诺。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote

__all__ = [
    "ReadOnlyDatabaseError",
    "read_only_connection",
    "read_only_query",
    "run_readonly_queries",
]


class ReadOnlyDatabaseError(RuntimeError):
    """只读访问失败（文件缺失、被占用、schema 不兼容、数据库损坏）。"""


def _readonly_uri(path: str | Path) -> str:
    """构造 SQLite 只读 URI。

    Windows 路径要统一成斜杠并做百分号转义，否则 ``file:`` URI 解析会出错
    （反斜杠会被当成转义字符）。
    """
    resolved = Path(path).resolve()
    posix = resolved.as_posix()
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def read_only_connection(path: str | Path) -> sqlite3.Connection:
    """打开严格只读连接。调用方负责关闭。"""
    target = str(path)
    if target != ":memory:" and not Path(target).exists():
        raise ReadOnlyDatabaseError("数据库文件不存在；请先运行 migrate")
    try:
        connection = sqlite3.connect(
            _readonly_uri(target) if target != ":memory:" else ":memory:",
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
            uri=target != ":memory:",
        )
    except sqlite3.Error as exc:
        raise ReadOnlyDatabaseError(f"无法以只读方式打开数据库：{exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        try:
            # query_only 必须最先设置：它保证连接这一生都写不了任何东西。
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
    except sqlite3.Error:
        connection.close()
        raise
    return connection


@contextmanager
def read_only_query(path: str | Path) -> Iterator[sqlite3.Connection]:
    """只读查询上下文。退出时一定关闭连接。"""
    connection = read_only_connection(path)
    try:
        yield connection
    finally:
        connection.close()


def run_readonly_queries(path: str | Path, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[list[sqlite3.Row]]:
    """在**同一条只读连接**上顺序执行多条语句。

    聚合视图（总览、诊断）需要跨表一致读，用一条连接避免重复建连开销，也让所有
    计数在同一时刻下取到。语句里只会是 SELECT，任何写语句都会被 SQLite 拒绝。
    """
    with read_only_query(path) as connection:
        results: list[list[sqlite3.Row]] = []
        for sql, params in statements:
            try:
                results.append(connection.execute(sql, tuple(params)).fetchall())
            except sqlite3.OperationalError as exc:
                raise ReadOnlyDatabaseError(f"只读查询失败：{exc}") from exc
        return results
