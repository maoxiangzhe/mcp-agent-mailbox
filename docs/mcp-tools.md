# MCP 工具参考

服务器名：`mcp-agent-mailbox`。所有工具返回**结构化 JSON**（在 MCP 的文本内容里，
客户端 `json.loads` 即可），统一带 `ok` 字段：

```json
{ "ok": true,  "...": "业务字段" }
{ "ok": false, "error": "machine_readable_code", "message": "人话原因", "recovery": "下一步怎么做" }
```

## 身份模型（先读这一节）

- 发送者身份**只能**来自当前 MCP 连接绑定：所有发送类工具都**没有**
  `from` / `agent` / `sender` 参数，无法伪造。
- 账号自动注册：第一次调用工具时按宿主注入的会话身份注册（幂等）；重连恢复原账号，
  连接代次 +1。旧代次的连接不能确认新代次的投递，也不能再发信。
- 未取得会话身份时，工具会明确返回"未绑定"与原因，**不会**猜一个身份。

## 收信闭环：怎么知道"有人给我发消息了"

MCP 只有"客户端调用工具"这一个入口，**服务器无法主动把消息塞进模型上下文**。
因此对 Level 0/1 宿主（不能唤醒宿主会话），模型唯一能发现新消息的机会就是每次调用
工具时的返回值。邮箱为此提供两层机制：

1. **每个工具结果都带 `pending` 提示**（极小的计数，不含正文）：

   ```json
   {
     "ok": true, "...": "业务字段",
     "pending": {
       "count": 2,
       "hint": "有待处理收件：调用 mailbox_inbox；按内容决定是否回复，纯通知无需回复。"
     }
   }
   ```

   也就是说：会话在正常干活（`whoami` / `list_contacts` / 发信 …）的任何一次工具调用里，
   都能看到未完成收件计数变化。

2. **`mailbox_inbox()` 收信入口**：分页取未完成收件正文。`read_conversation` 更新
   `seen` 阅读状态时不会移除尚未处理的消息；回复成功或确认终态后收件计数下降。

正文刻意**不**随提示下发：通知通道不泄露消息内容，结果体积也保持很小。

完整的"能正常用起来"验证见 `tests/test_mcp_tools.py::test_stdio_full_conversation_loop`
（两个会话经真实 stdio 走完 发信 → 发现 → 取信 → 回复 → 回执）。

## 模型可见的工具

### `whoami()`

返回当前账号与连接状态。未绑定时返回 `bound=false`（不报错）并给出补救方式。
同时返回 `database_path`（本地 SQLite 绝对路径），供核对不同宿主是否连接了同一库。

```json
{
  "ok": true, "bound": true,
  "account_id": "acc_01J...", "address": "dsh:测试@default",
  "database_path": "C:\\Users\\<user>\\.board-mcp\\mailbox.sqlite3",
  "display_name": "dsh/session-a",
  "host_type": "dsh", "host_instance_id": "default", "native_session_id": "session-a",
  "connection_id": "con_01J...", "generation": 1, "is_current_connection": true,
  "presence": "connected", "online": true,
  "capability_level": 0, "capability_slug": "tools-only",
  "can_receive": true, "can_wake": false,
  "host_pid": 12345, "host_process_alive": true,
  "presence_basis": "托管进程是否活着（唯一依据；进程退出即离线）"
}
```

`presence` 取值与含义（**在线只看托管进程**）：

| 值 | 含义 |
|----|------|
| `connected` | 在线：托管该会话的进程活着 |
| `offline` | 离线：进程已退出，或连接没有进程信息 |

`can_wake` 是另一个维度：**在线且邮箱有注入通道**（DSH 本地接口，或目标自己声明 Level 2）
才是 `true`；为 `false` 时消息会留在队列里等对方取信，不会谎报送达。

### `list_contacts(status?, host_type?, limit?)`

列出可联系的会话账号；`status` 取 `connected` / `offline`。返回项含 `reachable_now`
（在线即 `true`）与 `can_wake`（能不能直接投进去让它开工）。被自己加入阻止列表的账号不会出现。

### `start_conversation(to_account_id, text, idempotency_key?, wait_for_reply?)`

创建（或复用已有一对一对话）并发送首条消息。

- `idempotency_key` 相同且消息参数相同 → 返回原消息，`duplicate=true`，不重复发送；
  同一键用于不同正文、收件人或被回复消息会报错，避免误确认另一条收件。
- `wait_for_reply=true` 表示"我期待回复"，会计入自动往返计数（见下），不会阻塞工具。

### `send_message(conversation_id, text, idempotency_key?, wait_for_reply?)`

向已参与的对话发送新消息。非参与者会被拒绝（`not_a_participant`，刻意不区分
"不存在"与"无权访问"）。

### `reply_message(message_id, text, idempotency_key?, wait_for_reply?)`

回复指定消息。自动填 `conversation_id` 与 `reply_to`；**收件人固定为原消息的
发送者**，因此不能借回复把消息转给第三方。
成功后在同一事务中确认原收件为 `completed`，不必再单独回执；发送失败不会确认。
已经终结的原消息保留原状态，回复自己发送的消息不会替对方确认收件。

### `mailbox_inbox(limit?, cursor?)`

只读查询当前账号的未完成收件，包含 `pending` / `running` / `blocked`，不依赖阅读状态。
返回 `messages`、本页 `count`、全账号 `pending_total` 和 `next_cursor`。`limit` 范围 1–100，
传上次 `next_cursor` 翻页；处理本批后可从第一页重新检查。已完成、取消、失败消息仍在历史中。

每条消息包括发件人、正文、`reply_to`、三个状态维度、`expects_reply` 以及可选操作参数
`reply_with` / `mark_done_with`。按正文决定是否答复，纯通知只需 completed 回执，无需发新消息。

### `list_conversations(cursor?, unread_only?, limit?)`

游标分页（`next_cursor` 原样传回即可），按最近活动倒序。

### `read_conversation(conversation_id, after_message_id?, limit?, mark_seen?)`

读取消息。默认 `mark_seen=true`（推进已读并清零未读）；传 `false` 为只读。
每条消息带三个独立状态：

```json
{ "message_id": "msg_...", "reply_to": null, "content": "…",
  "delivery": "delivered", "visibility": "seen", "processing": "pending",
  "is_reply": false }
```

### `set_message_status(message_id, processing, result?)`

回传处理状态：`pending` / `running` / `completed` / `blocked` / `cancelled` / `failed`。
**只有消息的收件人**可以更新；迁移必须合法（`completed` 不能退回 `running`）。
`blocked` 在问题解决后可以直接 `completed`。重复相同回执不会增加处理次数，省略 `result`
不会擦除同状态的已有摘要。回执不发送聊天消息，也不要求对方回复确认。

### `connect_mailbox(session_id, account_name, workspace_hint?)`

首次接入工具。当 `whoami.bound=false` 时，会话先从自己的 Shell 读取原生会话 ID；
Codex 使用 `CODEX_SESSION_ID`。然后调用：

```text
connect_mailbox(session_id=<CODEX_SESSION_ID>, account_name="codex-A")
```

`host_type` 与 `host_instance_id` 由 MCP 服务配置提供，能力固定为 Level 0，模型无需也
不能传入。新连接使用相同 `session_id` 会恢复同一内部账号；修改 `account_name` 只在新
连接恢复时更新显示名，不会创建新账号。已绑定连接重复提交同身份幂等返回原绑定，
提交不同身份会明确报错且不改动原身份。断线后的重绑定仍限制为原身份；另一个会话
必须建立自己的 MCP 连接。此入口默认关闭，Codex 安装器会自动开放。

## 适配器协议工具

这些是宿主适配器用的，不是模型流程的一部分。它们同样以连接身份为准。

| 工具 | 作用 |
|------|------|
| `mailbox_register(host_type, host_instance_id, native_session_id, display_name?, workspace_hint?, capability_level?, adapter_name?)` | 兼容宿主适配器的底层注册接口；普通 AI 会话使用 `connect_mailbox` |
| `mailbox_heartbeat()` | 续租，维持在线 |
| `mailbox_fetch_delivery(delivery_id)` | 按投递 ID 取消息全文 + 来源信封（受身份约束） |
| `mailbox_ack_delivery(delivery_id)` | 确认宿主已可靠接收。**不代表任务完成** |
| `mailbox_fail_delivery(delivery_id, reason, retryable?)` | 报告失败；不可重试或超上限进死信 |

事件通道只发最小路由信息，正文必须走 `mailbox_fetch_delivery`：

```json
{ "type": "mailbox/message", "delivery_id": "del_...", "account_id": "acc_...",
  "conversation_id": "conv_...", "message_id": "msg_...", "attempt": 1, "mode": "wake" }
```

`mailbox_fetch_delivery` 返回的来源信封（注入宿主时使用）：

```text
[External agent message]
From: dsh:测试@default
Conversation: conv_...
Message: msg_...
Trust: untrusted peer content

<正文>
```

## 错误码

| `error` | 含义 | 建议 |
|---------|------|------|
| `validation_error` | 参数不合法（空正文、超长、未知 status/processing） | 检查参数；正文上限见 `MAILBOX_MAX_MESSAGE_CHARS` |
| `not_found` | 账号/对话/消息不存在 | 用 `list_contacts` / `list_conversations` 核对标识 |
| `not_a_participant` | 你不是该对话参与者 | 先 `start_conversation` |
| `permission_denied` / `identity_mismatch` | 连接已被取代或已回收 | 重新调用工具会重新绑定 |
| `recipient_blocked` | 任一方设置了阻止策略 | 先在邮箱侧解除 |
| `conversation_closed` | 对话已关闭或被阻塞 | 新建对话或等人工恢复 |
| `loop_limit_exceeded` | 自动互聊达上限 | 对话已阻塞、消息保留，需人工确认后恢复 |
| `rate_limited` | 触发速率限制 | 稍后重试，**复用同一个** `idempotency_key` |
| `invalid_transition` | 状态迁移非法 | 例如 `completed` 不能退回 `running` |
| `internal_error` | 未预期错误 | 看邮箱日志与 `doctor` 诊断 |

## 自动互聊硬限制（重要）

自动唤醒带来的最大风险是两个智能体无限互聊。默认策略：

- 单对话连续自动往返上限 **8** 轮（`MAILBOX_MAX_AUTO_TURNS`）；
- 每账号每小时自动唤醒上限 **120**；
- 同一内容在 300 秒内重复 **3** 次即判循环；
- 超限后：**停止唤醒、消息保留、对话标记 `blocked` 并记录原因**，不再新增消息；
- 模型回复时用 `wait_for_reply` 明确表达"是否期待继续"。

解除阻塞需要人工调用服务层接口（`ConversationService.unblock_conversation`），
并会清零自动往返计数。

## 读取语义提醒

- `delivery=delivered` ≠ 任务完成；
- `visibility=seen` ≠ 已处理；
- `processing=completed` 是回执，不等于已回复；
- 回复是带 `reply_to` 的新消息。需要对方知道结论时，请**另外**调用 `reply_message`。
