# 邮箱接入与在线规则

## 一句话规则

**托管该会话的进程活着 → 账号在线；进程没了 → 账号离线。** 只看进程，不看租约、不看心跳。

## 在线判定（唯一依据：进程）

| 情形 | presence | 发信方看到什么 | 收件方能否直接开工 |
|------|----------|----------------|--------------------|
| 托管进程活着 | `connected` | 消息可以直接投进去 | 能（宿主适配器支持注入时） |
| 托管进程已退出 | `offline` | 消息照样发得出去，**排队**等它上线 | 不能（不会去打开程序） |
| 连接没有进程信息 | `offline` | 同上 | 不能 |

实现要点：

- 每条连接记录托管进程 PID（`connections.host_pid`），由宿主进程建立连接时写入，**模型无法指定**；
- 判定走 `domain/process.py`：Windows 用 `OpenProcess` + `GetExitCodeProcess`，POSIX 用
  `os.kill(pid, 0)` 并排除僵尸进程；
- `compute_presence()` 只做一件事：进程活着 → `connected`，否则 `offline`；
- 维护循环按进程存活回收连接（`connections.mark_disconnected`）：**进程还活着的连接永不因时间回收**
  ——会话在思考时不会发心跳，按租约回收会把活着的账号判离线；
- 探测不到进程时**保守判离线**：宁可显示离线，也不要谎报在线——谎报会让发信方以为消息已送达。

租约（`lease_expires_at`）、心跳、`durable` 字段仍在库里，但**不参与在线判定**，只作诊断证据。
能力等级（`HostCapabilityLevel`）回答的是另一个问题——"能不能把消息注入目标会话并启动一个回合"
——它不改变在线/离线：Level 0 只意味着不能注入，不意味着离线。

## 离线账号：只存，不发

离线账号**收得到信**，但收的形式是"排队等它上线"：

- 消息立即持久化（`deliveries.state = queued`），正文谁都能按权限检索；
- 邮箱**不会**去启动那个程序、不会做 UI 自动化、不会伪造"已送达"；
- 账号上线（进程被拉起来）后，用 `mailbox_inbox` / `read_conversation` 就能取到这些消息。

## 在线账号：直接接收并开工

在线账号的目标是**发信方一发出，对方就能开始干活**（像给子代理发一条消息那样）：

- 邮箱侧有对得上该宿主的**直连注入通道**时（DSH 就是这一种），派发立刻调用该通道，
  把"去取信"的提示作为一条用户消息送进目标会话，宿主确认接收后投递才变成 `delivered`；
- 目标自己声明了 Level 2（有适配器会取走事件去注入）时，发 `mailbox/message` 事件，
  等适配器注入成功并确认后才变成 `delivered`；
- 两条通道都没有时，消息**保持 `queued`**——不谎报 `delivered`，也**不置 `failed`**
  （`failed` 会被读成"投递失败、消息可能丢"，而它其实好好躺在邮箱里）；
- 收件侧永远有一个不依赖注入的入口：任何工具调用的返回里带 `pending.count`，`mailbox_inbox` 给出
  "可以直接开工"的上下文（对话、正文、回复参数、回执参数）。

### DSH 的注入通道怎么配

DSH 0.2.0 有一个本地写入口：`POST http://127.0.0.1:19387/api/session/prompt`，
它接受一条用户消息并对**冷会话隐式 resume**，然后在会话里启动一个回合。鉴权是签名 cookie，
密钥在 `$DSH_HOME/.credentials.yaml` 的 `client-connection/browser-session` 记录里。
因为 DSH 启动 MCP 子进程时会过滤 `DSH_*` 变量，所以给邮箱服务器显式配这两个：

```jsonc
"env": {
  "MAILBOX_SESSION_ID": "session-你的会话ID",
  "MAILBOX_DSH_WEB_URL": "http://127.0.0.1:19387",
  "MAILBOX_DSH_HOME": "C:\\Users\\<你>\\.dsh"
}
```

行为如实分两种（`whoami` / `list_contacts` 的 `can_wake` 就是这么算出来的）：

| 情况 | can_wake | 消息去向 |
|------|----------|----------|
| 密钥可读 + URL 可达 | `true` | 直接注入目标会话并启动回合，宿主确认后 `delivered` |
| 密钥读不到 / 连不上 / 被拒（401、403） | `false` | 保持 `queued`，等目标下次调用工具取信 |

`whoami` / `list_contacts` 的 `wake_basis` 会把依据写清楚：`direct_channel`（有直连通道）、
`declared_level2`（目标自己声明能被注入）、`no_channel`、`offline`。
连不上或鉴权被拒**不算投递失败**：消息留在队列里，等目标自己取信或通道恢复。

密钥读不到的真实含义是"DSH 那侧没把 client-connection 插件激活"或"home 不是这个目录"——
DSH 自己激活该插件时会把密钥写进 `$DSH_HOME/.credentials.yaml`。

## 投递状态怎么读

| 状态 | 含义 |
|------|------|
| `queued` | **已持久化在邮箱里**。目标离线、或在线但暂时没法注入，都停在这里 |
| `dispatched` | 已把事件交给目标连接，等宿主确认 |
| `delivered` | 宿主确认**已接收**（不代表任务完成） |
| `failed` | 真正尝试过并失败，仍会按退避重试 |
| `dead_letter` | 超过最大尝试次数或不可重试，等人工处理 |

## 账号注册：会话 ID 即账号身份

账号唯一键是 `(host_type, host_instance_id, native_session_id)`。同一个会话 ID 重连恢复同一个账号，
只推进连接代次。

**接入即注册**：MCP 服务器启动时按 `MAILBOX_SESSION_ID` 自动注册并绑定，无需任何手工步骤；
`whoami` 也会在身份可用时自动补齐绑定。账号信息（显示名、工作区提示）可以在后续被更新：
重连时传新的 `account_name` 即会刷新同一账号的显示名，不会新建账号。

会话 ID 的注入方式（按宿主选一种，**不要让模型自己填**）：

### 方式一：宿主注入环境变量（推荐）

DSH 的 MCP 配置支持 `env`：

```jsonc
{
  "transport": "stdio",
  "serverName": "mailbox",
  "command": "E:\\zcz\\modle\\MCP\\mcp-agent-mailbox\\.venv\\Scripts\\python.exe",
  "args": ["-m", "mcp_agent_mailbox.cli", "serve"],
  "env": {
    "MAILBOX_HOST_TYPE": "dsh",
    "MAILBOX_HOST_INSTANCE_ID": "default",
    "MAILBOX_SESSION_ID": "session-你的会话ID",
    "MAILBOX_CAPABILITY_LEVEL": "0"
  },
  "cwd": "E:\\zcz\\modle\\MCP\\mcp-agent-mailbox"
}
```

`MAILBOX_CAPABILITY_LEVEL` 必须诚实：DSH 现在叫不醒，只能填 `0`。填 `2` 会让界面显示
"可注入开工"而实际叫不动。

### 方式二：显式注册（配置生效前也可用）

```bash
.venv/Scripts/python.exe -X utf8 tools/register_dsh_session.py \
    --session-id "session-你的会话ID" --display-name "DSH-A" --capability 0
```

注意：脚本进程一退出，它登记的那个 PID 就死了，账号会显示离线。**"在线"必须由一个长期活着的
进程来体现**，这就是为什么方式一才是正路。

## 两层

| 层 | 给谁用 | 入口 |
|----|--------|------|
| MCP 层 | AI 会话接入使用 | `whoami` / `list_contacts` / `start_conversation` / `send_message` / `reply_message` / `mailbox_inbox` / `read_conversation` / `set_message_status` |
| 可视化层 | 人看 | `cli dashboard`（只读 Web 监控台：在线情况、对话、投递积压、诊断） |

## 模型侧怎么用

### 发信方

| 工具 | 说明 |
|------|------|
| `whoami()` | 我是谁、在线吗、依据是什么 |
| `list_contacts()` | 有哪些账号、各自在线与否、能不能被注入开工 |
| `start_conversation(to_account_id, text)` | 建对话并发第一条 |
| `send_message(conversation_id, text)` | 继续发 |
| `reply_message(message_id, text)` | 回复某条 |
| `read_conversation(conversation_id)` | 读历史 |

### 收件方：一次调用拿到"可以直接开工"的上下文

```jsonc
// mailbox_inbox(limit=20)
{
  "ok": true,
  "count": 1,
  "pending_total": 1,
  "messages": [
    {
      "conversation_id": "conv_...",
      "message_id": "msg_...",
      "from_display_name": "codex-A",
      "content": "请复核这个改动",
      "reply_with":      { "tool": "reply_message",      "args": { "message_id": "msg_...", "text": "<你的结论>" } },
      "mark_done_with":  { "tool": "set_message_status", "args": { "message_id": "msg_...", "processing": "completed" } }
    }
  ],
  "work_order": "对这些消息：先看 content，再按 reply_with 回对方结论；做完用 mark_done_with 回执。"
}
```

只读：不改变投递状态、不推进已读位点、不发送任何东西。

### 命令行等价入口（不依赖 MCP）

```bash
.venv/Scripts/python.exe -X utf8 -m mcp_agent_mailbox.cli inbox <account_id>
```

适合替会话轮询的脚本 / 定时任务 / 人工查看。

## 限制（如实）

- **DSH 的注入依赖凭据文件**：`$DSH_HOME/.credentials.yaml` 里读得到签名密钥时，邮箱可以用本地接口
  把消息注入目标会话并启动回合（这是"直接接收并开工"的实现）；读不到时如实降级：消息保持
  `queued`，等目标下次调用工具取信。要一条完全不依赖凭据的路径，需要在 DSH 进程内实现 Cordis 插件
  （用 `ctx.sessionController.resolveAgent(sessionId)` + `agent.followup(...)`，属于 DSH 侧新代码）。
- 注入的文本只写"去 mailbox_inbox 取信"，**不把消息正文塞进 DSH 会话日志**：正文仍由邮箱按权限给出。
- 静态 `env` 只能服务一个会话；多会话需要多份 MCP 配置项。
- MCP 服务器进程随宿主拉起/回收，所以"在线窗口"等于宿主保持该子进程存活的窗口。
