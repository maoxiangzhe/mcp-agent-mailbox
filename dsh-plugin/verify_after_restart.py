"""重载/重启 DSH 之后，验收"一个会话一个邮箱账号、身份由宿主注入"。

背景：本次迁移把 profile 里的 profile 根 mcp-mailbox 行（写死 MAILBOX_SESSION_ID）
换成了 `cordis:include` -> 本插件目录的 patch。插件在每个 Agent 创建时把邮箱 MCP 挂到
`agent.ctx`，并注入 `MAILBOX_SESSION_ID = agent.id`。

可在**重启后**观察到的结果（本脚本就查这些）：

  V1 本会话在邮箱里有了自己的账号，且 `native_session_id == $env:DSH_SESSION_ID`
     —— 这是"身份由宿主注入"最硬的证据：写死的旧值是 `session-1a533e71-…`，
        与当前会话 ID 不同，只有宿主注入才可能对上。
  V2 该账号有 `state=active` 且 `is_current=1` 的连接，`host_pid` 活着
     —— 说明托管进程真实存在（新插件为每个会话各起一个邮箱 MCP 子进程）。
  V3 该账号 `display_name` 不是旧的 `DSH-A`（旧账号是另一个原生会话）
  V4 旧账号（`session-1a533e71-…`）不再有 active 连接
     —— 说明 profile 根写死的那条已经被插件取代，没有残留抢占。
  V5 每个 dsh 账号的 `native_session_id` 互不相同
     —— "一个会话一个账号"的直接检查。
  V6 报告全部 active 连接及其进程存活情况，便于人工核对"关掉一个会话只离线一个"。

用法：
    # 只看当前会话
    python verify_after_restart.py
    # 指定另一个会话（对比"两个会话两个账号"）
    python verify_after_restart.py --session <另一个 DSH_SESSION_ID>
    # 不依赖环境变量，直接给会话 ID
    python verify_after_restart.py --session-id session-xxxx

注意：本机沙箱限制 `~/.board-mcp` 目录创建新文件（SQLite 的锁文件），所以只读查询
使用 `immutable=1`。该模式**会忽略未合并的 WAL**，因此脚本会在存在 `-wal` 文件时
明确警告"结果可能过期"，而不是假装准确。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sqlite3
import sys
from ctypes import wintypes
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("MAILBOX_HOME") or r"C:\Users\mxz\.board-mcp") / "mailbox.sqlite3"
LEGACY_SESSION = "session-1a533e71-e3b1-4b1f-926e-7096cddabf85"

__all__ = ["main"]


def _alive(pid: int | None) -> bool | None:
    if not pid:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel32.CloseHandle(handle)


def _connect(db: Path) -> sqlite3.Connection:
    """用普通只读连接（`mode=ro`）。

    **不要**加 `immutable=1`：该模式会**忽略未合并的 WAL**，而邮箱库正是 WAL 模式、
    服务在跑时一定有未合并的 WAL —— 读出来会是过期快照（表现为"账号明明建好了，
    脚本却说没有"）。只有 `mode=ro` 才看得到 WAL 里的最新数据。
    """
    uri = "file:" + db.resolve().as_posix() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError:
        print("⚠ 普通只读连接失败，退回 immutable（可能读到过期快照）")
        connection = sqlite3.connect(
            "file:" + db.resolve().as_posix() + "?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
    connection.row_factory = sqlite3.Row
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重启后验收每会话邮箱账号")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--session", default=None, help="要核对的会话 ID（默认取 $env:DSH_SESSION_ID）")
    parser.add_argument("--session-id", dest="session_id", default=None, help="同上（显式指定）")
    args = parser.parse_args(argv)

    db = Path(args.db)
    session = args.session_id or args.session or os.environ.get("DSH_SESSION_ID", "")
    results: list[tuple[str, bool, str]] = []

    def check(cid: str, ok: bool, note: str) -> None:
        results.append((cid, bool(ok), note))
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {note}")

    print(f"邮箱库   : {db}")
    print(f"本会话 ID: {session or '(未取到)'}")
    print(f"旧写死值 : {LEGACY_SESSION}\n")

    # 先看插件的自证落点：它直接回答"插件加载了吗、这个会话挂载了吗"，不用等日志。
    state_path = Path(__file__).resolve().parent / ".runtime-state.json"
    print("=== 插件自证落点（.runtime-state.json）===")
    if state_path.is_file():
        lines = [l for l in state_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        loaded = [l for l in lines if '"plugin.loaded"' in l]
        mounted = [l for l in lines if '"mount.ok"' in l]
        failed = [l for l in lines if '"mount.failed"' in l or '"mount.skipped"' in l]
        print(f"  插件加载记录 {len(loaded)} 条；挂载成功 {len(mounted)} 条；跳过/失败 {len(failed)} 条")
        for line in lines[-6:]:
            print(f"    {line}")
    else:
        print(f"  文件不存在：{state_path}")
        print("  → 说明插件**从未被加载**（include/patch/插件 这条链没通），或 DSH 进程没重载")
    print()

    if not db.is_file():
        print(f"找不到数据库：{db}")
        return 2
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            print(f"⚠ 存在 {side.name}：immutable 只读会忽略未合并的 WAL，以下结果可能过期。\n")

    if not session:
        print("取不到会话 ID：请在 DSH 会话的 shell 里运行，或显式传 --session-id。")
        return 2

    with _connect(db) as con:
        accounts = [dict(r) for r in con.execute(
            "SELECT account_id, display_name, native_session_id, updated_at FROM accounts"
        )]
        connections = [dict(r) for r in con.execute(
            "SELECT account_id, generation, state, is_current, host_pid, opened_at, "
            "heartbeat_at FROM connections"
        )]

    mine = [a for a in accounts if a["native_session_id"] == session]
    legacy = [a for a in accounts if a["native_session_id"] == LEGACY_SESSION]

    print("=== 邮箱里的账号 ===")
    for account in sorted(accounts, key=lambda a: a["updated_at"], reverse=True):
        mark = " <== 本会话" if account["native_session_id"] == session else ""
        print(f"  {account['display_name']:24} {account['native_session_id']}{mark}")
    print()

    check(
        "V1.本会话有账号且身份由宿主注入",
        len(mine) == 1,
        f"account(session={session}) = {[a['account_id'] for a in mine]}"
        + ("；注意：与旧写死值不同才算注入成功" if session != LEGACY_SESSION else ""),
    )

    active_for_mine = []
    if mine:
        account_id = mine[0]["account_id"]
        active_for_mine = [
            c for c in connections
            if c["account_id"] == account_id and c["state"] == "active"
        ]
        check(
            "V2.本会话账号有活跃连接且托管进程活着",
            len(active_for_mine) == 1 and _alive(active_for_mine[0]["host_pid"]) is True,
            "active 连接 = "
            + str([{k: c[k] for k in ("generation", "host_pid")} for c in active_for_mine])
            + f"，PID 存活={_alive(active_for_mine[0]['host_pid']) if active_for_mine else None}",
        )
        check(
            "V3.不是旧账号（display_name 不再继承 DSH-A）",
            mine[0]["display_name"] != "DSH-A" or session == LEGACY_SESSION,
            f"display_name = {mine[0]['display_name']!r}",
        )

    legacy_active = [
        c for c in connections
        if legacy and c["account_id"] == legacy[0]["account_id"] and c["state"] == "active"
    ]
    check(
        "V4.旧写死账号不再占着活跃连接",
        not legacy_active,
        f"旧账号 active 连接数 = {len(legacy_active)}（期望 0）",
    )

    dsh_accounts = [a for a in accounts if a["native_session_id"]]
    sessions = [a["native_session_id"] for a in dsh_accounts]
    check(
        "V5.每个账号对应不同会话",
        len(sessions) == len(set(sessions)),
        f"{len(sessions)} 个账号 / {len(set(sessions))} 个不同 native_session_id",
    )

    print("\n=== 全部 active 连接（人工核对『关掉一个会话只离线一个』）===")
    for connection in connections:
        if connection["state"] != "active":
            continue
        owner = next((a for a in accounts if a["account_id"] == connection["account_id"]), None)
        print(
            f"  {owner['display_name'] if owner else '?':24} "
            f"session={owner['native_session_id'] if owner else '?'} "
            f"host_pid={connection['host_pid']} alive={_alive(connection['host_pid'])}"
        )

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 70)
    print(f"重启后验收：{passed} PASS / {failed} FAIL（共 {len(results)}）")
    print("=" * 70)
    if failed:
        print("\n排查建议：")
        print("  1) 确认 DSH 已重载/重启（patch 改动需要重载才生效）")
        print("  2) 跑一遍装载前预检：python preflight.py")
        print("  3) 看 DSH 日志里是否有 include 或插件加载报错")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
