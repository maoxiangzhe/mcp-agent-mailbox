# 从旧版（board-mcp 公告板 / 0.2.x 收件箱）升级

## 一句话

旧版把状态放在 `~/.board-mcp/boards/*.md` 和 `~/.board-mcp/notes/*.jsonl`；
0.3.0 起权威状态是 `~/.board-mcp/mailbox.sqlite3`。**旧数据不迁移也不删除**，
旧工具在一个兼容周期内继续可用。

## 需要知道的三件事

1. **数据目录不变**：仍读 `BOARD_MCP_ROOT`（也支持新的 `MAILBOX_HOME`），
   所以已有安装升级后能找到原来的目录。
2. **旧的公告板与收件箱文件不会被改写**：新版把它们当只读归档与人类可读投影。
3. **新邮箱注册名为 `mcp-agent-mailbox`**：旧名称仅在确认启动参数指向
   `mcp_agent_mailbox.cli` 时迁移，并保留原有禁用、超时、工具设置和环境变量。
   真正旧公告板以及其他同名服务器不迁移；`--legacy` 继续注册 `board` / `board-mcp`。

## 升级步骤

```bash
cd mcp-agent-mailbox
git pull
uv sync --locked

# 1) 备份旧数据（整个数据目录一起拷，最省事）
#    Windows: 复制 %USERPROFILE%\.board-mcp 到别处
#    Linux/macOS: cp -a ~/.board-mcp ~/.board-mcp.bak-$(date +%F)

# 2) 初始化新数据库（幂等，可反复执行）
uv run python -m mcp_agent_mailbox.cli migrate

# 3) 确认环境
uv run python -m mcp_agent_mailbox.cli doctor
uv run python -m mcp_agent_mailbox.cli adapters

# 4) 重新注册 MCP 服务器（默认注册新版会话寻址邮箱）
uv run python install.py --dry-run     # 先看要改什么
uv run python install.py               # 实际写入（改动前自动备份配置）

# 5) 重启各终端会话，让客户端重新拉取工具列表
```

## 注册内容的变化

| 项目 | 旧版 | 新版（默认） |
|------|------|--------------|
| 启动命令 | `python server.py` | `python -m mcp_agent_mailbox.cli serve` |
| 工具集 | 10 个协作工具（含自由填写 `agent` 的 `send_note`） | 10 个会话寻址工具 + 5 个适配器协议工具 |
| 身份 | 调用方传 `agent` 字符串 | 由连接上下文绑定；工具里没有发送者参数 |
| 权威存储 | Markdown + JSONL | SQLite（WAL） |

想继续用旧版服务器：`uv run python install.py --legacy`（会打印不安全提示）。

## 需要适配器注入会话身份

新版一个邮箱账号 = 一个原生会话，所以必须让 MCP 进程知道"我是哪个会话"：

```jsonc
// 以 Codex 为例（~/.codex/config.toml）
[mcp_servers.mcp-agent-mailbox]
type = "stdio"
command = "C:\\path\\to\\.venv\\Scripts\\python.exe"
args = ["-m", "mcp_agent_mailbox.cli", "serve"]
cwd = "C:\\path\\to\\mcp-agent-mailbox"
env_vars = ["CODEX_SESSION_ID", "CODEX_THREAD_ID"]

[mcp_servers.mcp-agent-mailbox.env]
MAILBOX_HOST_TYPE = "codex"
MAILBOX_SESSION_ID = "<仅限这个配置只服务一个会话时的原生会话 ID>"
MAILBOX_HOST_INSTANCE_ID = "default"
MAILBOX_CAPABILITY_LEVEL = "0"   # 0/1/2；只有验证过才写 2
```

`install.py --session-id <ID> --capability <0|1|2>` 可以帮你写进客户端配置。
注意：把一个固定会话 ID 写进客户端配置，只适合"这个客户端配置本来就只服务一个
会话"的场景；多个会话共用会互相冒用身份。

## 旧接口与旧数据的处理

| 旧东西 | 新版行为 |
|--------|----------|
| `~/.board-mcp/boards/*.md` | 保留在磁盘；**不**导入为新消息 |
| `~/.board-mcp/notes/*.jsonl` | 保留在磁盘（只读归档）；**不**导入为新消息 |
| `send_note` / `read_notes` / `ack_notes` | 兼容期保留，标记 legacy，见 [legacy-compat.md](legacy-compat.md) |
| `get_board` / `claim_files` / `report_done` / `check_conflict` / `release_claim` / `post_decision` / `init_bulletin` | 独立兼容模块保留，**不**与新消息数据库耦合 |

**没有自动导入**是有意的：旧的 `agent` 字符串无法可靠映射到新的
`(host_type, host_instance_id, native_session_id)` 三元组，猜一个映射会把"谁是谁"
搞错，而身份错了整套安全模型就失效。需要保留的历史信息请人工整理成新对话。

## 回滚

1. 停止所有邮箱进程；
2. `uv run python install.py --legacy`（或把客户端配置改回 `python server.py`）；
3. 需要的话用备份覆盖 `~/.board-mcp`；
4. 重启终端会话。

新版的 `mailbox.sqlite3` 可以留在原地：旧版代码不会读它，也不会破坏它。
