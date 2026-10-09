# 把某个 DSH 会话接入邮箱（DSH-A 实测记录）

本文记录**已经做过并验证过**的接入步骤，以及为什么需要这些步骤。

## 一句话结论

DSH 启动 MCP 子进程时会**丢掉所有 `DSH_*` 变量**，所以邮箱进程拿不到"我是哪个会话"。
但 DSH 的 MCP 配置**支持 `env`**（`dsh-mcp-client` 的 `Config` 里 `env: z.dict(String)`，
显式配置的 env 会合并到被清洗过的环境之上），因此正规做法是：

> 在 MCP 服务器配置里显式写 `MAILBOX_SESSION_ID` / `MAILBOX_HOST_TYPE`。

代价要说清楚：**静态配置只能服务一个会话**。一个 DSH 会话独占一个配置项；
如果你要同时接入多个 DSH 会话，需要每个会话一个配置项（或等 in-process 插件方案）。

## DSH 的配置形状（取自 dsh-mcp-client 的 schema，字段名是权威的）

```jsonc
{
  "transport": "stdio",              // 必填，固定值
  "serverName": "mailbox",           // 必填，需匹配 SERVER_NAME_PATTERN
  "command": "E:\\zcz\\modle\\MCP\\mcp-agent-mailbox\\.venv\\Scripts\\python.exe",
  "args": ["-m", "mcp_agent_mailbox.cli", "serve"],
  "env": {                           // 关键：显式 env 会保留
    "MAILBOX_HOST_TYPE": "dsh",
    "MAILBOX_HOST_INSTANCE_ID": "default",
    "MAILBOX_SESSION_ID": "<该会话的原生会话 ID>",
    "MAILBOX_CAPABILITY_LEVEL": "0"
  },
  "cwd": "E:\\zcz\\modle\\MCP\\mcp-agent-mailbox",
  "toolCallTimeoutMs": 60000,
  "failOnStartupError": false
}
```

`MAILBOX_SESSION_ID` 从该会话的 shell 里取：

```powershell
$env:DSH_SESSION_ID      # 例如 session-<示例-DSH-会话>
```

`MAILBOX_CAPABILITY_LEVEL` 必须诚实：DSH 目前**只能填 0**（tools-only，不能实时唤醒）。
填 2 会让界面显示"可唤醒"，而实际叫不醒——这正是设计文档明令禁止的。

## 注册（已验证）

配置生效前，也可以先用运维脚本把账号注册出来（本会话实测）：

```bash
.venv/Scripts/python.exe -X utf8 tools/register_dsh_session.py \
    --session-id "session-<示例-DSH-会话>" \
    --display-name "DSH-A" --workspace "E:\zcz" --capability 0
```

实测输出：

```text
  account_id     : acc_<示例账号-DSH>
  host           : dsh / default
  native_session : session-<示例-DSH-会话>
  presence       : connected（托管进程活着）
  can_wake       : 取决于邮箱能否读到 DSH 的签名密钥
```

## 收发验证（已验证）

```bash
.venv/Scripts/python.exe -X utf8 tools/verify_dsh_mailbox.py \
    --target-account acc_<示例账号-DSH> --text "给 DSH-A 的第一封测试信"
```

实测结果：消息进入 `conv_...`，投递 `queued → dispatched(notified_only) → failed`
（**没有**谎报 `delivered`），DSH-A 侧 `pending` 里能看到 1 条并可读取正文。

## 会话侧怎么用（MCP 工具）

DSH 会话装上 MCP 服务器后，模型侧工具名形如 `mcp__mailbox__whoami`：

| 目的 | 工具 |
|------|------|
| 确认自己是谁 | `whoami()` → `bound=true`、`presence=connected`、`can_wake=false` |
| 找对端 | `list_contacts()` |
| 发信 | `start_conversation(to_account_id, text)` / `send_message` / `reply_message` |
| 收信 | `mailbox_pending()` → `list_conversations(unread_only=true)` → `read_conversation(...)` |
| 回执 | `set_message_status(message_id, "completed", result=...)` |

**收信要靠主动取信**：DSH 是 Level 0，邮箱无法主动唤醒会话。邮箱的做法是给**每次工具
返回**附一个 `pending.count` 未读计数——会话在干活的任何一次工具调用里都能发现新消息。

## 已知限制（不要误解）

- DSH 是 Level 0：**不能**被实时唤醒，只能主动取信。界面/`whoami` 都如实标注。
- 静态 `env` 只能绑定一个会话；多会话需要多份配置。
- 想让 DSH 达到 Level 2，必须在 DSH 进程内写 Cordis 插件调用
  `ctx.agents.get(sessionId).followup(message)`，再由插件自建本地传输连回 Broker。
  这是**新代码**，本仓库尚未实现（见 `docs/host-capability-matrix.md`）。
- 不要让模型自己填会话 ID（`connect_mailbox` 那类做法）：那等于允许任何会话冒领
  别人的账号。身份必须来自配置或宿主注入。
