# Broker 与诊断

## 分层与职责

```text
domain          纯领域模型与状态机（不依赖 SQLite / MCP SDK / 任何宿主）
ports           协议：仓储、时钟、事件通道、宿主适配器
application     用例编排：账号、会话、投递、在线状态
infrastructure  SQLite 仓储与迁移、结构化日志、事件总线
adapters        宿主适配器（能力等级与降级策略）
daemon          Broker 与生命周期
mcp             MCP 端点：连接上下文、工具注册、stdio 服务器
cli             运维命令
```

依赖方向单向向内：domain 不知道 SQLite，application 不知道 MCP SDK，MCP 层一行
SQL 都没有（所有 SQL 集中在 `infrastructure/sqlite/`）。

## Broker 做什么

Broker 是唯一权威状态中心：

- 持有 SQLite 与应用服务；
- 后台维护循环：**先**回收"托管进程已退出"的连接（否则会朝死连接派发）→ 补投"派发后
  未确认"的投递 → 派发到期投递 → 清理过期计数窗口；
- 作为事件总线的发布者：事件只发给目标账号**当前主连接**，旧代次连接收不到。
  队列有界，溢出时丢最旧并计数——权威事实已在库里，重投由投递重试补上。

维护循环默认每 1 秒跑一轮；测试直接调用 `broker.maintenance_once()`，不依赖线程。

## 投递生命周期

```text
        消息与投递在同一事务入队
message.created ──► deliveries.state = queued
                        │
                        ├─ 目标离线 ────────────► 保持 queued（不消耗尝试次数）
                        │
                        └─ 目标健康
                             ├─ Level 2 ──► claim（原子抢占，attempt+1）
                             │               └─► dispatched ──► 适配器确认 ──► delivered
                             │                                └─ 失败 ──► failed（指数退避）
                             │
                             └─ Level 0/1 ─► 只发通知事件
                                             投递**不置** dispatched/delivered
                                             （通知过 ≠ 送达过），按更长间隔重试
```

关键点：

- **至少一次**：重试复用同一个 `delivery_id`，接收方按它去重；
- **不可重复派发**：`claim_for_dispatch` 原子抢占，抢占失败即放弃；
- **确认前断线**：`requeue_stalled` 把长期未确认的投递退回重试（复用同一 ID）；
- **超过上限**：进入 `dead_letter` 并广播 `delivery/cancelled`，不再自动重试。

## 配置项

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `MAILBOX_HOME` / `BOARD_MCP_ROOT` | `~/.board-mcp` | 数据目录 |
| `MAILBOX_HEARTBEAT_SECONDS` | 20 | 心跳间隔 |
| `MAILBOX_LEASE_SECONDS` | 60 | 租约期限 |
| `MAILBOX_GRACE_SECONDS` | 10 | 断线宽限 |
| `MAILBOX_MAX_AUTO_TURNS` | 8 | 单对话连续自动往返硬上限 |
| `MAILBOX_MAX_AUTO_WAKES_PER_HOUR` | 120 | 每小时每账号自动唤醒上限 |
| `MAILBOX_REPEATED_CONTENT_LIMIT` | 3 | 相同内容重复多少次判循环 |
| `MAILBOX_REPEATED_CONTENT_WINDOW_SECONDS` | 300 | 循环检测时间窗 |
| `MAILBOX_MAX_SENDS_PER_MINUTE` | 30 | 单账号发送速率 |
| `MAILBOX_MAX_MESSAGES_PER_CONVERSATION_PER_MINUTE` | 60 | 单对话速率（双方合计） |
| `MAILBOX_MAX_DELIVERY_ATTEMPTS` | 5 | 超过即死信 |
| `MAILBOX_DELIVERY_BACKOFF_MS` / `_MAX_BACKOFF_MS` | 2000 / 300000 | 指数退避 |
| `MAILBOX_MAX_MESSAGE_CHARS` | 4000 | 正文上限（超限拒绝，不截断） |
| `MAILBOX_GLOBAL_PAUSE` | false | 全局暂停自动唤醒 |
| `MAILBOX_LEGACY_TOOLS` | false | 是否允许旧版不安全工具 |
| `MAILBOX_LOG_MESSAGE_CONTENT` | false | 是否记录正文（默认脱敏） |

参数全部集中在 `config.Settings`，不允许散落为魔法数字。

## 诊断

```bash
uv run python -m mcp_agent_mailbox.cli doctor        # 综合诊断（JSON）
uv run python -m mcp_agent_mailbox.cli accounts      # 账号 + 在线状态
uv run python -m mcp_agent_mailbox.cli deliveries acc_xxx
```

`doctor` 输出包含：`integrity_check`、账号数与在线状态分布、待投递样本数、
死信数量、维护循环统计、生效参数。

### 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `whoami` 显示 `bound=false` | 适配器没注入 `MAILBOX_SESSION_ID` | 在客户端配置里注入会话身份后重启服务器 |
| `can_wake=false` | 邮箱读不到该宿主的注入凭据（例如 DSH 的 `.credentials.yaml`） | 属于如实降级：消息会排队等对方取信；DSH 重启重建凭据文件后即可注入 |
| 消息一直 `queued` | 目标离线，或在线但没有注入通道 | 等目标上线；或让目标主动 `read_conversation` / `mailbox_inbox` 取信 |
| 投递反复 `failed` 最后死信 | 注入通道可用但一直失败（HTTP 拒绝、连接出错） | 看 `deliveries` 的 `last_error`；修好后用新消息重试 |
| 对话变成 `blocked` | 自动互聊达上限或检测到重复内容 | `blocked_reason` 有原因；人工确认后恢复 |
| 出现 `identity_mismatch` | 连接被同账号新连接取代，或已回收 | 重新调用工具会重新绑定 |
| 日志里看不到正文 | 默认脱敏，这是刻意的 | 需要时显式开 `MAILBOX_LOG_MESSAGE_CONTENT`（有泄露风险） |

## 日志与可观测

结构化 JSON 日志写在数据目录下的 `logs/`。事件类型清单见
`infrastructure.observability.audit_event_types()`，覆盖账号注册、连接开关、在线
变化、消息创建、投递派发/确认/重试/死信、唤醒请求、处理状态、循环阻塞等。

规则：**日志不写凭据、Token 或完整正文**；超长文本只留长度与哈希前缀；
事件用 ID 关联（`account_id` / `conversation_id` / `message_id` / `delivery_id`）。

## 已知限制（未实现项）

- **跨进程 stdio 代理未实现**：当前 stdio MCP 服务器在自身进程内创建 Broker。
  同一 `MAILBOX_HOME` 的多个进程通过同一个 SQLite 文件共享权威状态（已由
  `tests/test_mcp_tools.py::test_stdio_roundtrip_over_real_protocol` 覆盖），
  但实时事件通道是**进程内**的：一个进程里派发的事件不会推给另一个进程的连接。
  跨进程实时投递需要新增一个常驻 Broker + 本地套接字协议，属于后续工作。
- 适配器的实时唤醒只有 Codex 路径（且未在真实宿主验证）。
- 没有消息过期清理任务：消息与投递不会自动删除（`dead_letter` 需要人工处理）。
