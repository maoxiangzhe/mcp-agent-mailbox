"""只读看一眼本机邮箱里"谁在线"，用来人工核对在线判定。

在线严格参照进程：托管该会话的进程活着 -> 在线；进程没了 / 没有进程信息 -> 离线。
租约、心跳、durable、能力等级都**不参与**这个判定（它们最多只影响"能不能直接注入
让它开工"）。

用法： .venv/Scripts/python.exe -X utf8 tools/inspect_live_state.py [数据库路径]
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_agent_mailbox.domain.presence import (  # noqa: E402
    HostCapabilityLevel,
    compute_presence,
)
from mcp_agent_mailbox.domain.process import is_process_alive  # noqa: E402

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".board-mcp" / "mailbox.sqlite3"

connection = sqlite3.connect(str(DB))
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT a.account_id, a.display_name, a.host_type, a.host_instance_id, a.native_session_id, "
    "       c.is_current, c.state, c.capability_level, c.host_pid "
    "FROM accounts a LEFT JOIN connections c ON c.account_id = a.account_id AND c.is_current = 1"
    " WHERE a.deleted_at IS NULL"
).fetchall()

print(f"database = {DB}")
print("\n=== 账号在线情况（只看托管进程）===")
for row in rows:
    host_pid = row["host_pid"]
    state = compute_presence(
        host_pid=(int(host_pid) if host_pid is not None else None),
        closed=str(row["state"]) != "active",
    ).value
    level = HostCapabilityLevel(int(row["capability_level"] or 0))
    if host_pid is None:
        note = "没有进程信息 => 离线"
    elif is_process_alive(int(host_pid)):
        note = f"进程 {host_pid} 活着 => 在线"
    else:
        note = f"进程 {host_pid} 已退出 => 离线"
    wake = "可注入开工" if state == "connected" and level.can_wake_session else "不能注入"
    print(
        f"  {row['display_name']:<12} host={row['host_type']:<6} "
        f"session={row['native_session_id']:<44} presence={state:<10} "
        f"level={int(level)}（{wake}）  {note}"
    )

print("\n=== 说明 ===")
print("  在线 = 托管该会话的进程活着（唯一依据）。")
print("  离线账号照样收得到信：消息持久化排队，等它上线再收（不会谎报已送达）。")
print("  能力等级（Level 0/1/2）只回答'能不能把消息注入目标会话并让它开始一个回合'，")
print("  它不改变在线/离线：Level 0 只是不能注入，不是在离线。")
connection.close()
