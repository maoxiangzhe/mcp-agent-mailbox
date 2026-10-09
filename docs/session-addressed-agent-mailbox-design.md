# 会话寻址的 MCP 智能体邮箱设计

状态：设计草案  
日期：2026-10-04  
目标仓库：`mcp-agent-mailbox`

## 1. 背景与目标

本项目要从“共享公告板加定向通知”演进为通用的 MCP 智能体邮箱，使 DSH、Codex、Claude Code、OpenCode 等彼此独立的智能体软件能够进行可靠、双向、可追踪的直接对话。

邮箱不把一个智能体软件整体视为一个收件人。每个原生会话独立注册为一个邮箱账号：

```text
DSH
├─ 会话 A -> 邮箱账号 A
└─ 会话 B -> 邮箱账号 B

Codex
├─ 任务 C -> 邮箱账号 C
└─ 任务 D -> 邮箱账号 D
```

当会话保持 MCP 连接且宿主支持实时注入时，账号显示在线，其他账号可以直接向它发送消息。目标宿主收到消息后唤醒对应会话，将消息注入下一模型回合；模型可通过邮箱 MCP 回复原对话。

本设计优先解决：

1. 一个邮箱账号稳定对应一个原生会话；
2. 在线会话之间可以直接互传消息；
3. 空闲但仍在线的会话能被新消息唤醒；
4. 断线、重连、重复投递和进程崩溃不会丢失或重复执行消息；
5. 发送者身份不能由模型参数随意伪造；
6. 邮箱核心保持通用，客户端差异收敛在宿主适配器中。

## 2. 非目标

第一阶段不包含：

- 跨用户、跨机器的互联网邮箱服务；
- 面向不可信租户的完整身份认证和端到端加密；
- 取代 DSH、Codex 等软件自身的会话系统；
- 让邮箱直接调用模型供应商 API；
- 依靠 UI 自动化操作客户端；
- 自动允许收到的消息执行文件写入、网络访问或其他高风险操作；
- 不受限制的智能体自动互聊。

文件认领、共享公告和决策广播可以作为兼容功能保留，但不再定义核心消息模型。

## 3. 关键术语

### 3.1 宿主

运行智能体会话的软件，例如 DSH、Codex、Claude Code 或 OpenCode。

### 3.2 原生会话

宿主自身管理的会话、任务或线程。它拥有自己的上下文、工作目录、权限和生命周期。

### 3.3 邮箱账号

原生会话在邮箱系统中的稳定地址。账号不是人工登录账号，而是可寻址的会话身份。

### 3.4 连接

某个原生会话当前建立的一次 MCP/适配器连接。账号是稳定身份，连接是临时实例；重连不能创建新账号。

### 3.5 对话

两个或多个邮箱账号之间的有序消息线程。回复属于同一 `conversation_id`，并通过 `reply_to` 指向被回复消息。

### 3.6 唤醒

宿主适配器收到实时消息后，将其转换为宿主认可的会话输入，并请求对应原生会话开始新模型回合。

## 4. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│ 智能体宿主                                                 │
│ DSH / Codex / Claude Code / OpenCode                       │
│                                                            │
│  原生会话 <-> Host Adapter <-> MCP Endpoint                │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              │ 注册、工具调用、实时事件
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Mailbox Broker（本地常驻服务）                              │
│                                                            │
│ Account Registry    Connection Registry    Presence Lease  │
│ Conversation Store  Message Store          Delivery Queue  │
│ Event Dispatcher    Retry / Dead Letter    Audit Log       │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
                         SQLite 数据库
```

系统分为三层。

### 4.1 Mailbox Broker

本地常驻的权威服务，负责账号、连接、在线状态、对话、消息、投递、重试和持久化。所有 MCP Endpoint 都连接同一个 Broker，禁止各进程分别修改 Markdown 或 JSONL 来模拟共享状态。

### 4.2 MCP Endpoint

向模型暴露通用邮箱工具，并将当前 MCP 连接绑定到唯一邮箱账号。模型不需要也不能在每次发送时声明 `from`。

MCP Endpoint 可以是 Broker 内建的 Streamable HTTP 服务，也可以是轻量 stdio 代理。若保留 stdio 安装方式，每个 stdio 进程只做协议转发，状态仍由 Broker 统一管理。

### 4.3 Host Adapter

连接邮箱协议与宿主原生会话系统，负责：

- 获取宿主类型和原生会话 ID；
- 自动注册或恢复邮箱账号；
- 维护连接心跳；
- 接收邮箱实时事件；
- 将消息写入宿主的正式 inbox 或等价入口；
- 请求宿主唤醒目标会话；
- 将权限、取消和执行结果反馈给邮箱。

通用邮箱不能假设所有宿主具有相同的唤醒接口。每种宿主实现独立适配器，但适配器遵循统一契约。

## 5. 账号与连接模型

### 5.1 自动注册

建立连接时，适配器提交：

```json
{
  "host_type": "dsh",
  "host_instance_id": "desktop-default",
  "native_session_id": "session-3ada72f3-bb7d-4824-822d-abf9a670bad7",
  "display_name": "DSH / 测试",
  "workspace_hint": "E:\\zcz",
  "capabilities": ["receive", "reply", "wake"]
}
```

账号唯一键为：

```text
(host_type, host_instance_id, native_session_id)
```

相同唯一键再次注册时恢复原账号，更新连接信息，不生成新账号。Broker 返回不可由模型指定的 `account_id` 和可显示地址，例如：

```text
account_id: acc_01J...
address: dsh:测试@desktop-default
```

`account_id` 是权威标识；可显示地址允许改名，不参与引用完整性。

### 5.2 身份绑定

每次 MCP 工具调用都从已认证的连接上下文获得发送账号：

```text
connection_id -> account_id -> native_session_id
```

`send_message`、`reply_message` 等工具不接受 `from` 或 `agent` 参数。模型不能代表其他会话发信、确认或改变状态。

### 5.3 重连和多连接

一个账号在短时间内可能同时存在旧连接和新连接。Broker 使用连接代次 `generation`：

- 新连接注册成功后成为当前主连接；
- 旧代次不得确认新投递；
- 相同账号的重复连接可以用于观察，但只有主连接接收唤醒事件；
- 主连接断开后，Broker 可选择最近健康连接接管，或将账号转为离线。

## 6. 在线状态

账号状态由 Broker 根据连接和能力计算，模型不能自行声明在线。

```text
realtime  连接健康，且适配器声明并验证支持实时注入和唤醒
connected 连接健康，但只能主动调用工具取信
stale     心跳或传输超过租约期限，等待断线回收
offline   没有健康连接
```

连接建立后立即获得短租约，并通过传输活动或显式心跳续租。建议初始参数：

- 心跳间隔：20 秒；
- 租约期限：60 秒；
- 断线后宽限：10 秒。

“MCP 已连接”不能自动等价为“模型可被唤醒”。只有通过宿主适配器能力检查的账号才显示 `realtime`。

## 7. 对话与消息模型

### 7.1 对话

```json
{
  "conversation_id": "conv_01J...",
  "kind": "direct",
  "participants": ["acc_dsh_...", "acc_codex_..."],
  "created_at": "2026-10-04T15:00:00+08:00",
  "closed_at": null
}
```

第一阶段只实现一对一直接对话。群聊以后作为独立设计扩展，避免第一版同时解决发言顺序、成员变更和广播风暴。

### 7.2 消息

```json
{
  "message_id": "msg_01J...",
  "conversation_id": "conv_01J...",
  "reply_to": "msg_01J_previous",
  "sender_account_id": "acc_dsh_...",
  "recipient_account_id": "acc_codex_...",
  "content_type": "text/plain",
  "content": "收到后请检查这个设计并回复。",
  "idempotency_key": "client-generated-key",
  "created_at": "2026-10-04T15:00:01+08:00"
}
```

消息正文是数据，不是系统指令。宿主注入消息时必须附加不可伪造的来源元数据，明确它来自另一个智能体账号。

### 7.3 状态拆分

不得用单一 `status` 同时表达传输、阅读和处理：

```text
delivery:   queued -> dispatched -> delivered | failed | dead_letter
visibility: unread -> seen
processing: pending -> running -> completed | blocked | cancelled | failed
```

回复是一条新消息；`completed` 是处理回执，二者不能互相替代。

## 8. MCP 工具接口

第一版向模型提供以下工具：

### `whoami()`

返回当前账号、宿主、原生会话、在线能力及连接状态。

### `list_contacts(status?, host_type?)`

列出允许联系的会话账号。默认突出 `realtime` 和 `connected` 账号，同时明确离线状态。

### `start_conversation(to_account_id, text, idempotency_key?)`

创建一对一对话并发送首条消息。发送者来自连接上下文。

### `send_message(conversation_id, text, idempotency_key?)`

向已有对话发送新消息。

### `reply_message(message_id, text, idempotency_key?)`

回复指定消息。Broker 校验调用者是对话参与者，并自动填写 `conversation_id` 和 `reply_to`。

### `list_conversations(cursor?, unread_only?)`

分页列出当前账号参与的对话。

### `read_conversation(conversation_id, after_message_id?, limit?)`

读取对话消息。读取行为可更新 `visibility=seen`，也可提供显式只读参数。

### `set_message_status(message_id, processing, result?)`

更新处理状态。状态迁移必须校验，例如 `completed` 默认不能退回 `running`。

注册、心跳、实时投递确认属于适配器协议，不暴露为依赖模型主动调用的工具。

## 9. 实时投递与唤醒协议

### 9.1 投递事件

Broker 向目标主连接发送：

```json
{
  "type": "mailbox/message",
  "delivery_id": "del_01J...",
  "account_id": "acc_codex_...",
  "conversation_id": "conv_01J...",
  "message_id": "msg_01J...",
  "attempt": 1
}
```

事件只携带最小路由信息；适配器可以通过受身份约束的接口获取完整正文，避免通知通道泄露不属于该连接的消息。

### 9.2 适配器处理顺序

1. 校验事件目标与当前账号一致；
2. 按 `delivery_id` 去重；
3. 获取完整消息；
4. 写入宿主正式 inbox 或等价的受支持入口；
5. 宿主持久化成功后返回 `delivered`；
6. 若策略允许，唤醒对应原生会话；
7. 模型开始处理后更新 `processing=running`；
8. 模型通过 `reply_message` 回复，完成后更新处理状态。

`delivered` 表示宿主已经可靠接收，不表示模型已经阅读或完成任务。

### 9.3 宿主能力等级

```text
Level 0: tools-only
  可以发信和主动查信，不能实时接收。

Level 1: notify
  可以实时显示通知，但不能自动启动模型回合。

Level 2: wake
  可以把消息注入指定原生会话并启动模型回合。
```

联系人界面和 `whoami` 必须展示能力等级。系统不得把 Level 0/1 宣称为实时智能体对话。

### 9.4 DSH 适配器

已观察到 DSH 内部存在会话 inbox、`agent/inbox/spliced` 事件以及 `next-step`/`next-turn` 目标。正式适配器应调用 DSH 支持的运行时 API，把外部邮箱消息转换为来源明确的新事件，而不是直接修改压缩会话日志或投影缓存。

需要进一步验证：

- 是否存在对外稳定的 inbox 注入 API；
- 空闲顶层会话能否由该 API 触发新回合；
- 注入事件所需的权限、来源结构和取消语义；
- 桌面版重启后如何恢复适配器与会话绑定。

### 9.5 Codex 适配器

适配器必须优先使用 Codex公开或稳定的任务创建、继续、消息发送接口。若只能创建新任务，则第一版可把每个外部对话映射为一个专用 Codex 任务，但不能宣称能够恢复任意已关闭任务。

不得通过修改 Codex 内部数据库、模拟 UI 输入或伪造用户历史实现唤醒。

## 10. 权限与安全边界

跨智能体消息默认是不可信数据。收到一条消息不自动授予发送方以下权限：

- 修改或删除文件；
- 执行外部命令；
- 访问网络或账号；
- 发送消息给第三方；
- 放宽当前原生会话的沙箱和审批策略。

唤醒后的回合继承目标原生会话既有权限，取两者中更严格的一侧，不接受消息正文中的权限声明。

适配器注入模型上下文时采用结构化信封：

```text
[External agent message]
From: dsh:测试@desktop-default
Conversation: conv_01J...
Message: msg_01J...
Trust: untrusted peer content

<正文>
```

系统还必须具备：

- 每账号允许联系人列表或阻止列表；
- 单账号和单对话速率限制；
- 最大消息长度；
- 最大自动回复轮数；
- 循环检测；
- 全局和单会话暂停开关；
- 对高风险操作保留宿主原有确认机制；
- 日志脱敏，不记录凭据和完整敏感正文。

## 11. 防止无限互聊

自动唤醒引入两个智能体互相回复不停止的风险。第一版必须设置硬限制：

- 每个对话连续自动回合上限，建议默认 8；
- 每小时每账号自动唤醒上限；
- 相同内容哈希重复出现时暂停；
- 相同两个账号在短时间内高频往返时进入 `blocked`；
- 模型回复必须明确选择“回复并继续”或“完成，不再回复”；
- 超限后保留消息并通知用户，不继续启动模型。

## 12. 持久化与一致性

第一版采用 SQLite，开启 WAL。建议核心表：

```text
accounts
connections
conversations
conversation_participants
messages
deliveries
message_processing
adapter_checkpoints
audit_events
```

消息创建和首次投递入队必须位于同一事务。投递采用至少一次语义，接收方依靠 `delivery_id` 幂等去重。

数据库是权威事实源。联系人列表、未读计数和在线状态是可重建投影。Markdown 公告仅作为人类可读视图，不参与业务状态反向解析。

## 13. 故障处理

### 13.1 目标离线

消息保持 `queued`。账号重新上线后按原顺序投递，除非消息过期或对话被关闭。

### 13.2 连接在确认前断开

Broker 在退避后重新投递相同 `delivery_id`。适配器必须去重，不能重复注入模型历史。

### 13.3 已投递但唤醒失败

保留 `delivery=delivered`，记录 `processing=blocked/failed` 及原因。不得把宿主接收成功误报为任务处理成功。

### 13.4 Broker 重启

从 SQLite 恢复未完成投递和账号信息；在线状态全部重新计算，不能沿用重启前连接。

### 13.5 适配器不兼容

能力探测失败时降级为 `connected` 或 `offline`，保留手动读信能力，不尝试私有文件注入。

## 14. 可观测性

结构化事件至少包含：

```text
account.registered
connection.opened
connection.renewed
connection.closed
presence.changed
message.created
delivery.dispatched
delivery.acknowledged
delivery.retry_scheduled
wake.requested
wake.started
wake.failed
processing.changed
reply.created
conversation.blocked
```

事件使用 ID 关联，不在普通日志中记录完整消息正文。调试正文必须显式开启并有保留期限。

## 15. 测试策略

### 15.1 单元测试

- 账号注册幂等和重连；
- 连接代次与租约过期；
- 发送者从连接上下文绑定；
- 对话参与者权限；
- 状态机合法迁移；
- 幂等发送和投递去重；
- 循环检测与自动回合上限。

### 15.2 Broker 集成测试

- 两个并发连接直接对话；
- 目标离线后重连补投；
- Broker 在入队、派发和确认各阶段崩溃恢复；
- 重复事件不产生重复模型输入；
- SQLite WAL 并发读写；
- 大量积压消息分页和顺序正确。

### 15.3 适配器契约测试

所有 Level 2 适配器必须通过统一契约：

1. 注册并恢复相同账号；
2. 接收实时事件；
3. 将消息准确注入指定会话；
4. 空闲会话被唤醒；
5. 重复 `delivery_id` 不重复注入；
6. 权限不因外部消息提升；
7. 取消、失败和完成状态准确回传。

### 15.4 端到端验收

第一阶段以 DSH 双会话为最小闭环：

```text
DSH 会话 A 发信
-> DSH 会话 B 在空闲状态被唤醒
-> B 回复
-> A 在空闲状态被唤醒并看到回复
```

第二阶段验收 DSH 与 Codex：

```text
DSH 会话发信
-> Codex 目标任务被唤醒或按明确映射创建
-> Codex 回复
-> DSH 原会话被唤醒
```

验收必须以事实事件和消息 ID 证明完整链路，不能只凭 UI 显示判断成功。

## 16. 迁移策略

现有工具保持一个兼容周期：

```text
send_note   -> 内部映射到新消息，但调用方必须先绑定当前账号
read_notes  -> 映射到会话/消息查询
ack_notes   -> 映射到 visibility 和 processing 状态
```

由于旧接口允许调用方填写 `agent`，兼容模式必须明确标为不安全，并默认只允许本地可信环境。新接口不保留自由填写发送者的能力。

原有公告板数据继续存放，不强制迁移为对话。新消息从 SQLite 启用后，JSONL 只读归档，不再作为新系统权威存储。

## 17. 分阶段交付

### 阶段 0：宿主可行性验证

- 只读确认 DSH 的正式 inbox/唤醒接口；
- 确认 Codex 可用的任务创建、继续和消息注入接口；
- 为每个宿主标注可达到的能力等级。

退出条件：至少 DSH 能达到 Level 2，且验证过程不依赖 UI 自动化或修改内部会话文件。

### 阶段 1：Broker 与通用 MCP

- SQLite 数据模型；
- 自动注册、身份绑定和在线租约；
- 一对一对话及可靠消息；
- 标准 MCP 工具；
- 离线队列、幂等和重试。

退出条件：两个测试客户端能够可靠收发，断线重连不丢不重。

### 阶段 2：DSH 实时适配器

- DSH 会话 ID 绑定；
- inbox 注入；
- 空闲会话唤醒；
- 权限继承、取消和状态回传；
- DSH 双会话端到端测试。

### 阶段 3：Codex 实时适配器

- 明确任务映射策略；
- 创建或继续任务；
- 实时消息注入；
- DSH 与 Codex 端到端测试。

### 阶段 4：其他宿主与兼容迁移

- Claude Code、OpenCode 适配器；
- 旧工具兼容层；
- 安装器、诊断命令和运维文档。

## 18. 已确定的设计决策

1. 产品是通用 MCP 智能体邮箱，不隶属于某个上层智能体项目；
2. 一个邮箱账号对应一个原生会话，不对应整个智能体软件；
3. 账号自动注册，并通过原生会话 ID 稳定恢复；
4. 在线由健康连接和租约计算；
5. 实时可唤醒能力与普通 MCP 连接状态分开表示；
6. 对话采用持久化的存储转发模型，而非无状态消息直通；
7. 模型不能自由填写发送者身份；
8. Broker 是权威状态中心，宿主差异由适配器处理；
9. 不使用 UI 自动化或直接修改宿主内部会话文件；
10. 第一版只实现一对一对话，并设置自动互聊硬限制。

## 19. 待验证事项

以下问题必须在实施前通过只读取证或官方接口文档确认：

1. DSH 是否提供稳定的外部 inbox 注入和空闲会话唤醒接口；
2. Codex 是否允许外部创建或继续指定任务并注入消息；
3. 各宿主能否在 MCP 连接初始化时提供稳定的原生会话 ID；
4. MCP transport 断开是否与宿主会话关闭具有一致语义；
5. 各宿主如何显示和取消由外部消息触发的模型回合；
6. 宿主是否允许适配器注册实时事件回调，或需要独立本地守护进程。

这些问题不会改变邮箱核心数据模型，但会决定各宿主最终可达到的能力等级。
