# MCP 智能体邮箱

> **会话寻址**的多智能体邮箱：一个邮箱账号对应一个**原生会话**，而不是整个智能体软件。
> 让 DSH、Codex、Claude Code、OpenCode 里各自独立的会话之间可靠地互发消息。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green)

```text
DSH
├─ 会话 A -> 邮箱账号 A  ─┐
└─ 会话 B -> 邮箱账号 B  ─┼─  同一个 Broker + 一份 SQLite（唯一权威状态）
Codex                    │
└─ 任务 C -> 邮箱账号 C  ─┘
```

## 30 秒理解设计

| 问题 | 做法 |
|------|------|
| 谁是谁 | 账号唯一键 `(host_type, host_instance_id, native_session_id)`，重连恢复原账号 |
| 谁能发信 | 发送者从**连接上下文**解析；工具里根本没有 `from` / `agent` 参数 |
| 在线是什么 | `connected`（托管连接进程活着）/ `offline`（无有效托管进程）；`can_wake` 单独表示是否有注入通道 |
| 会不会丢 | 消息与投递入队同一事务；至少一次投递；目标离线保持排队，回来按序补投 |
| 会不会重复 | 发送幂等键 + `delivery_id` 去重 |
| 会不会互相刷屏 | 自动往返上限、速率限制、重复内容循环检测、全局/单对话暂停 |
| 收信安全吗 | 跨智能体消息是**不可信数据**，不授予任何权限提升；注入带来源信封 |

完整设计见 [`docs/session-addressed-agent-mailbox-design.md`](docs/session-addressed-agent-mailbox-design.md)。

## 安装

要求：**Python 3.10+**、**uv**，以及至少一个目标终端。

```bash
git clone https://github.com/maoxiangzhe/mcp-agent-mailbox.git
cd mcp-agent-mailbox
uv sync --locked          # 建 .venv 并装依赖（MCP SDK；Python 3.10 另需轻量 tomli）

uv run python -m mcp_agent_mailbox.cli migrate     # 初始化/升级数据库（幂等）
uv run python -m mcp_agent_mailbox.cli doctor      # 诊断
uv run python -m mcp_agent_mailbox.cli tools       # 列出会暴露的 MCP 工具
uv run python -m mcp_agent_mailbox.cli adapters    # 宿主能力矩阵
uv run python -m mcp_agent_mailbox.cli dashboard   # 启动只读 Web 监控台（会打印带令牌的地址）
```

### 只读 Web 监控台

```bash
uv run python -m mcp_agent_mailbox.cli dashboard --host 127.0.0.1 --port 8765
```

本机回环上的**只读**控制台，用来观察账号/连接/在线状态、对话与消息（含三个独立状态）、
投递积压与失败原因、数据库与适配器能力诊断。默认 `127.0.0.1:8765`，启动时生成临时
令牌并通过带令牌的 URL 打开；非回环地址会打印安全警告；端口冲突明确报错。

它是现有权威状态的投影，**没有**任何写操作：所有查询走 `mode=ro` + `query_only` 的
SQLite 连接，`/api/*` 的写方法一律 405，浏览前后业务表内容逐行一致（有端到端验收脚本
`tools/dashboard_smoke.py` 证明）。详见 [docs/dashboard.md](docs/dashboard.md).

注册进客户端（默认注册**会话寻址邮箱**；旧版公告板用 `--legacy`）：

```bash
uv run python install.py --dry-run                 # 演练：只看不改
uv run python install.py --target codex            # 只注册 Codex
uv run python install.py --target all              # 4 个终端全注册
uv run python install.py --check                   # 检查注册状态
```

默认客户端注册名统一为 `mcp-agent-mailbox`。旧 `board` / `board-mcp` 只有明确指向
`-m mcp_agent_mailbox.cli` 时才迁移；其他服务器保持原样。禁用状态、超时、额外环境变量
及工具设置保留，重复安装不重置这些选择。`--legacy` 才使用旧公告板名称。

### 会话身份必须由适配器注入（关键）

邮箱账号 = 一个原生会话，所以 MCP 服务器必须知道"我是哪个会话"。适配器要在启动
MCP 进程时注入：

| 环境变量 | 含义 | 必填 |
|----------|------|------|
| `MAILBOX_SESSION_ID` | 宿主原生会话/任务/线程 ID | 是（否则工具会如实报告"未绑定"） |
| `MAILBOX_HOST_TYPE` | `dsh` / `codex` / `claude` / `opencode` … | 是 |
| `MAILBOX_HOST_INSTANCE_ID` | 宿主实例标识（同一机器同一数据目录需稳定） | 建议 |
| `MAILBOX_CAPABILITY_LEVEL` | 适配器**已验证**的能力等级 0/1/2 | 建议 |
| `MAILBOX_HOME` | 数据目录（兼容旧版 `BOARD_MCP_ROOT`） | 可选 |

**在拿到会话 ID 之前，邮箱不会替你猜一个身份**：工具会返回明确的不可用原因，
而不是偷偷注册一个假账号。

> Codex Desktop 当前不会把聊天级 `CODEX_SESSION_ID` 直接传给全局 stdio MCP 进程。
> `install.py --target codex` 因此会以 `--allow-adapter-registration` 启动邮箱。会话首次看到
> `whoami.bound=false` 时，从自己的 Shell 读取 `CODEX_SESSION_ID`，调用
> `connect_mailbox(session_id=<自己的 CODEX_SESSION_ID>, account_name="codex-A")`；
> 随后再用 `whoami` 确认绑定。同一 MCP 连接不能切换到另一个会话；每个会话需要独立
> 连接。若宿主共享一个全局连接，必须先实现按会话隔离的适配器，不能通过轮流注册冒用身份。

重启 MCP 后，宿主再次提供相同三元身份会恢复原 `account_id` 并推进连接代次，历史与
离线积压保留。显式注册模式需再次调用 `connect_mailbox`，不会根据显示名、最后使用的账号
或其他对话的会话 ID 自动挑选身份。客户端聊天仍存在与邮箱连接在线是两件事：连接进程退出
不会删除原账号，也不表示聊天已经消失。

> DSH 特别注意：DSH 启动 MCP 子进程时会过滤掉所有 `DSH_*` 环境变量，因此
> **不能**指望自动拿到 `DSH_SESSION_ID`；需要在 profile 里为该 MCP 服务器显式
> 配置 `env:`（静态值无法区分会话）。DSH 的实时唤醒需要 in-process 插件路径，
> 详见 [宿主能力矩阵](docs/host-capability-matrix.md)。

## MCP 工具（模型可见）

| 工具 | 作用 |
|------|------|
| `whoami()` | 当前账号、宿主会话、能力等级、连接状态 |
| `connect_mailbox(session_id, account_name, workspace_hint?)` | 首次接入：用当前会话 ID 注册或恢复邮箱账号 |
| `list_contacts(status?, host_type?)` | 列出可联系账号，如实标注可否实时唤醒 |
| `start_conversation(to_account_id, text, idempotency_key?, wait_for_reply?)` | 建对话并发首条消息 |
| `send_message(conversation_id, text, idempotency_key?, wait_for_reply?)` | 向已有对话发消息 |
| `reply_message(message_id, text, idempotency_key?, wait_for_reply?)` | 回复指定消息（带 `reply_to`，成功后自动确认原收件） |
| `mailbox_inbox(limit?, cursor?)` | 分页取未完成收件，返回 `next_cursor` 和全局 `pending_total` |
| `list_conversations(cursor?, unread_only?, limit?)` | 游标分页列对话 |
| `read_conversation(conversation_id, after_message_id?, limit?, mark_seen?)` | 读消息；`mark_seen=False` 为只读 |
| `set_message_status(message_id, processing, result?)` | 回传处理状态 |

另有**适配器协议 + 收信**工具：`mailbox_register`、`mailbox_heartbeat`、
`mailbox_fetch_delivery`、`mailbox_ack_delivery`、
`mailbox_fail_delivery`。完整参考见 [docs/mcp-tools.md](docs/mcp-tools.md)。

### 会话怎么发现"有人给我发消息了"

MCP 只有"客户端调用工具"一个入口，服务器**无法**主动把消息塞进模型上下文。
所以邮箱在每个工具结果里附一个极小的未完成收件提示：

```json
{ "ok": true, "...": "业务字段",
  "pending": { "count": 1, "hint": "有待处理收件：调用 mailbox_inbox；按内容决定是否回复，纯通知无需回复。" } }
```

会话在正常干活（`whoami` / `list_contacts` / 发信 …）的任何一次工具调用里都能看到
收件计数变化，然后 `mailbox_inbox` 取全文。`read_conversation` 用于查看对话历史；
它只改变阅读状态，未完成收件仍留在 inbox。

正文**不**随提示下发：通知通道不泄露消息内容。

### 上手三步

```text
1) whoami                          # 确认 account_id、presence 与 can_wake（在线不等于能自动唤醒）
2) list_contacts                   # 找到对端 account_id
3) start_conversation(to_account_id=..., text="…")   # 发信；之后 mailbox_inbox 取信
```

### 三个状态维度必须分开读

```text
delivery:   queued -> dispatched -> delivered | failed | dead_letter
visibility: unread -> seen
processing: pending -> running -> completed | blocked | cancelled | failed
```

**`delivered` 只表示目标宿主已可靠接收，不代表模型已阅读或任务完成。**
回复是带 `reply_to` 的新消息；`processing=completed` 是处理回执，不发送新消息。
需要答复时调用 `reply_message`，成功后与回复写入同一事务确认原收件已处理；
纯通知调用 `set_message_status(..., processing="completed")` 即可，无需发送确认消息。
`wait_for_reply=True` 表示发件人期待答复，不会让工具阻塞等待；inbox 的 `expects_reply`
保留这个意图，仍需结合正文决定如何回复。
inbox 包含 `pending`、`running`、`blocked` 收件，排除 `completed`、`cancelled`、`failed`
终态；终态消息仍可在对话历史查看。分页时传上一页 `next_cursor`，确认处理后从第一页
重新检查即可；`pending_total` 始终统计全账号未完成收件。

## 安全边界

- 跨智能体消息是**不可信输入**：不授予文件写入、命令执行、网络访问、账号访问、
  向第三方发信，也不放宽沙箱与审批策略。
- 唤醒后的回合继承目标会话**既有**权限与审批策略。
- 会话自注册默认关闭；Codex 安装器会明确开启 `connect_mailbox`，让会话用自己
  Shell 中的 `CODEX_SESSION_ID` 完成首次注册。`mailbox_register` 仅保留适配器兼容。
- 日志默认脱敏，不记录凭据、Token 或完整正文。

细节见 [SECURITY.md](SECURITY.md)。

## 数据与兼容

- 权威存储：SQLite（WAL + 外键 + 显式事务），默认 `~/.board-mcp/mailbox.sqlite3`。
  沿用旧版的 `BOARD_MCP_ROOT` 是为了让已有安装不换目录就能升级。
- Markdown/JSONL 只作为兼容输入或人类可读投影，**不再**是权威状态。
- 旧接口 `get_board` / `claim_files` / `report_done` / `check_conflict` /
  `release_claim` / `post_decision` / `init_bulletin` / `send_note` / `read_notes` /
  `ack_notes` 在一个兼容周期内保留，标记为 legacy，见
  [docs/legacy-compat.md](docs/legacy-compat.md)。

## 开发与验证

```bash
uv run pytest                    # 新系统测试（单元 + 集成 + 契约，含真实 stdio 往返）
uv run python run_tests.py       # 一次跑完新测试 + 旧版兼容回归
uv run python -m mcp_agent_mailbox.cli doctor
```

测试要求（见 [CONTRIBUTING.md](CONTRIBUTING.md)）：全部使用隔离临时目录，
**绝不**写真实的 `~/.dsh`、`~/.codex` 或真实邮箱数据库。

## 文档

| 文档 | 内容 |
|------|------|
| [设计文档](docs/session-addressed-agent-mailbox-design.md) | 产品与架构依据 |
| [只读监控台](docs/dashboard.md) | 启动、API、安全模型、只读保证与验收 |
| [宿主能力矩阵](docs/host-capability-matrix.md) | 各宿主达到的等级与证据（含未验证项） |
| [MCP 工具参考](docs/mcp-tools.md) | 参数、返回值、错误码 |
| [数据库与迁移](docs/migrations.md) | 表结构、迁移规则、运维 |
| [Broker 与诊断](docs/broker.md) | 架构、循环、诊断命令、故障处理 |
| [从旧版升级](docs/migration-from-board-mcp.md) | 升级步骤与回滚 |
| [旧接口兼容](docs/legacy-compat.md) | 兼容范围与不安全的旧行为 |
| [CHANGELOG](CHANGELOG.md) | 版本记录 |

## 开源与发布

MIT 许可证，保留原作者版权声明。参与贡献请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，
安全边界见 [SECURITY.md](SECURITY.md)，发布前检查 [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)。

本仓库**不包含**用户的公告、消息、游标、客户端配置或运行日志。
