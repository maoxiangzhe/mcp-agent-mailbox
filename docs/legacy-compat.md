# 旧接口兼容（legacy）

## 定位

旧版接口（`server.py` 提供的 10 个工具）在一个兼容周期内保留，目的是**不打断已有
安装**。它们被明确标记为 legacy，并且：

- **不**影响新接口的安全模型：新工具不读取也不信任旧接口的状态；
- **不**破坏新身份约束：旧接口不参与新消息库的读写；
- 自由填写 `agent` 的旧行为会给出**弃用警告**；
- 旧接口的存储（Markdown 公告板、JSONL 收件箱）与新消息库**完全分离**。

## 两组旧接口

### 1. 公告板与文件认领（独立兼容模块）

`init_bulletin` / `get_board` / `claim_files` / `report_done` / `check_conflict` /
`release_claim` / `post_decision`

这些继续由 `server.py` 提供，仍然读写 `~/.board-mcp/boards/*.md`，
**不**与新消息数据库耦合。它们只做"共享公告 + 文件占用协调"，与邮箱身份无关。

### 2. 收件箱（不安全，明确标记）

`send_note(agent, to, text, ...)` / `read_notes` / `ack_notes`

| 能力 | 状态 |
|------|------|
| 定向消息、未读游标、status 回执、`request_id` 防重复 | 保留可用 |
| 调用方自由填写 `agent` 作为发送者 | 保留，但**不安全**：无法验证身份 |
| 读写 `~/.board-mcp/notes/*.jsonl` | 保留（只读归档语义：新系统不改写它） |

**为什么旧接口不安全**：`agent` 是调用方传来的字符串，任何客户端都能填别人的名字。
新系统不允许这样：发送者来自连接上下文。因此迁移期建议所有新流程使用
`start_conversation` / `send_message` / `reply_message`。

## 弃用警告

`server.py` 的旧工具在当前版本会打印一次性弃用提示（`legacy.tool_used` 审计事件），
说明：

- 旧接口只用于兼容，不再演进；
- 自由填写发送者不受信任；
- 新部署请使用会话寻址邮箱工具。

## 如何关闭旧接口

```bash
# 只注册新版会话寻址邮箱（不注册旧服务器）
uv run python install.py            # 默认行为，就是新版

# 旧服务器仅在显式指定时才注册
uv run python install.py --legacy
```

新进程的 `MAILBOX_LEGACY_TOOLS`（默认 `false`）用于控制 legacy 工具是否暴露。

## 兼容期结束后

计划（**尚未执行**，时间点待定）：

1. 从 `install.py` 移除 `--legacy`；
2. 把 `server.py` 的公告板部分移到 `legacy/` 子目录，作为独立可选模块；
3. 收件箱接口移除；`notes/*.jsonl` 转成只读归档工具（仅供人工查看）。

在此之前，旧接口不会被删除，也不会改变已有的数据文件格式。
