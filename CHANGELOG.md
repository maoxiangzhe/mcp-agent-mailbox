# 更新记录

## 0.3.1 — 2026-10-04

### 新增：只读 Web 监控台

一条命令启动本机回环控制台，用来观察账号/连接/在线状态、对话与消息（三个独立状态）、
投递积压与失败原因、数据库与适配器能力诊断。

```bash
uv run python -m mcp_agent_mailbox.cli dashboard --host 127.0.0.1 --port 8765
```

- 新增 `mcp_agent_mailbox/dashboard/`：`server.py`（HTTP、鉴权、路由、安全头、静态资源）、
  `queries.py`（只读查询门面）、`serializers.py`（稳定 JSON DTO）、`static/`（原生 HTML/CSS/JS）。
- 新增 `infrastructure/sqlite/read_only.py`：严格只读连接（`mode=ro` + `query_only=ON`），
  写语句由 SQLite 直接拒绝。
- 新增 CLI 子命令 `dashboard`（`--host` / `--port` / `--verbose`）：默认 `127.0.0.1:8765`，
  非回环地址打印安全警告，端口冲突明确报错，停止时释放线程与连接。
- 安全：启动生成高熵临时令牌（不落盘）、`Authorization: Bearer`、严格 CSP、
  `nosniff` / `no-referrer` / `DENY`、无宽泛 CORS、错误响应脱敏、前端全部文本渲染。
- 只读保证：`/api/*` 写方法一律 405；`total` 来自真实聚合并有 page size 上限与游标；
  浏览前后业务表逐行一致。
- 测试：`tests/test_dashboard_api.py`（真实 HTTP）、`tests/test_dashboard_static.py`
  （前端契约与 a11y）、`tests/test_dashboard_cli.py`（CLI 接线），以及端到端验收脚本
  `tools/dashboard_smoke.py`（真实子进程 + 真实 HTTP + 数据库前后比对）。

### 修复

- Codex Desktop 不会把聊天级 `CODEX_SESSION_ID` 直接交给全局 stdio MCP。安装器现在
  明确开放模型工具 `connect_mailbox(session_id, account_name, workspace_hint?)`：各会话从
  自己的 Shell 读取 `CODEX_SESSION_ID` 后完成首次注册，并可设置 `codex-A` / `codex-B`
  等显示名；宿主与能力参数不再暴露给模型。`mailbox_register` 仅保留适配器兼容。
- 投递状态机补齐 `failed -> delivered`：先失败后重试成功的投递现在能被确认，
  否则任何"先失败后成功"的投递都会卡在 `failed`。
- 处理状态机补齐 `pending -> blocked`：适配器可直接回传"受阻"，不必伪造 `running`。
- 监控台启动横幅改为即时 `flush`：stdout 被重定向到管道/文件时（脚本、CI）不再挂住。
- HTTP 客户端提前断开（`ConnectionResetError`）不再打印堆栈；未预期异常统一转成
  结构化 500，而不是直接断开连接。

## 0.3.0 — 2026-10-04

**重写为会话寻址的 MCP 智能体邮箱。** 权威存储由 Markdown/JSONL 迁移到 SQLite
（WAL + 外键 + 显式事务）。一个邮箱账号对应一个**原生会话**，而不是整个智能体软件。

### 新增

- **领域层**：账号/连接/对话/消息/在线状态与三个独立状态机（delivery / visibility /
  processing）。领域层不依赖 SQLite、MCP SDK 或任何宿主。
- **SQLite 权威存储**：12 张表、单调递增迁移、事务内执行、失败回滚、空库初始化，
  并有自动化测试覆盖（含"坏迁移必须回滚且不留半张表"）。
- **账号与连接模型**：稳定唯一键 `(host_type, host_instance_id, native_session_id)`；
  注册幂等；重连恢复原账号并推进连接代次；只有当前主连接能确认投递。
- **在线状态**：`realtime` / `connected` / `stale` / `offline`，由连接健康度 +
  已验证的能力等级推导。参数可配置（心跳 20s / 租约 60s / 宽限 10s）。
- **可靠投递**：消息创建与首次投递入队同一事务；至少一次投递；`delivery_id` 去重；
  指数退避重试；超过上限进死信并广播 `delivery/cancelled`；离线保持排队，
  重连后按入队顺序补投。
- **8 个会话寻址 MCP 工具**：`whoami` / `list_contacts` / `start_conversation` /
  `send_message` / `reply_message` / `list_conversations` / `read_conversation` /
  `set_message_status`。**没有任何工具接受发送者参数**。
- **5 个适配器协议工具**：`mailbox_register` / `mailbox_heartbeat` /
  `mailbox_fetch_delivery` / `mailbox_ack_delivery` / `mailbox_fail_delivery`。
- **收信闭环**：MCP 无法主动推送，因此每个工具结果都附未读计数提示
  （`pending.count`），并新增 `mailbox_pending` 收信入口。模型在正常干活的过程中就能
  发现新消息，然后 `read_conversation` 取全文。正文不随提示下发。
- **宿主适配器**：DSH（**Level 0**，如实降级）、Codex（**Level 2**，公开 CLI
  `queue` / `exec resume`，标注**未在真实宿主验证**）。不做 UI 自动化，
  不改写宿主内部会话文件或数据库。
- **安全机制**：正文长度上限、单账号/单对话速率限制、允许/阻止联系人、
  自动往返硬上限（默认 8）、重复内容循环检测、全局与单对话暂停、日志脱敏、
  注入时的外部来源信封。
- **CLI**：`serve` / `migrate` / `doctor` / `accounts` / `conversations` /
  `deliveries` / `tools` / `adapters` / `install`。
- **文档**：宿主能力矩阵、MCP 工具参考、数据库与迁移、Broker 与诊断、
  从旧版升级、旧接口兼容说明。

### 变更

- `pyproject.toml`：版本 0.3.0；新增 `dev` 可选依赖（pytest）；声明包与 CLI 入口。
- `install.py`：默认注册新版 `python -m mcp_agent_mailbox.cli serve`，
  并支持 `--session-id` / `--capability` / `--data-dir` / `--legacy`；
  Codex 写 `env` 子段注入会话身份；OpenCode / Trae 支持 env 字段。
- `README.md` / `SECURITY.md`：按新架构重写。

### 保留（兼容期）

- 旧接口继续可用并标记 legacy：`init_bulletin` / `get_board` / `claim_files` /
  `report_done` / `check_conflict` / `release_claim` / `post_decision` /
  `send_note` / `read_notes` / `ack_notes`；公告板与文件认领作为独立兼容模块保留，
  不与新消息数据库耦合。
- 数据目录仍优先读旧版 `BOARD_MCP_ROOT`（也支持 `MAILBOX_HOME`），已有安装不换目录。
- 客户端注册键（Claude 的 `board`、Codex 的 `[mcp_servers.board-mcp]`）保持不变。
- 旧数据文件**不迁移也不删除**：旧的 `agent` 字符串无法可靠映射到新的会话三元组，
  猜映射会导致身份错误。

### 未验证 / 未实现（如实声明）

- DSH 的实时注入与唤醒：**未实现**。DSH 的 inbox 是进程内投影，且 MCP 子进程拿不到
  `DSH_SESSION_ID`；达到 Level 2 需要在 DSH 进程内实现 Cordis 插件（设计已写出，
  代码未实现）。
- Codex 的真实端到端唤醒：代码按公开 CLI 契约实现并有契约测试（用注入的假 CLI 验证
  调用形状），但**未在真实 Codex 上验证**。
- 跨进程实时事件通道：多进程通过同一个 SQLite 共享权威状态（已有测试覆盖），
  但实时事件通道是进程内的；跨进程实时投递需要新增常驻 Broker + 本地套接字协议。
- 消息 TTL 与自动归档：未实现。

### 修复

- 迁移失败现在会整体回滚该迁移，且不写入版本记录（原本可能留下半张表）。
- 状态机补齐两条合法迁移：`dispatched -> dead_letter`、
  `pending -> completed/cancelled`（适配器可直接回传终态，不必伪造中间态）。
- 自动互聊达上限时，"阻塞对话 + 记录原因"现在会**先提交再报错**，
  不再被异常回滚连带撤销。
- 消息与投递的排序改用 `rowid` / 自增 `delivery_seq`：同一毫秒内创建的对象也能
  严格按入队顺序补投与分页。

## 0.2.0 — 2026-09-17

展示名称由「MCP 公告板」改为「MCP 智能体邮箱」，发布标识 mcp-agent-mailbox。保留 board-mcp 注册键及数据目录兼容。

- 增加定向消息、收发件箱和状态回执。
- 最早未读分页，只推进实际返回消息的送达游标。
- 不因历史上限删除消息；支持 request_id 防重复发送。
- 超长消息明确拒绝；损坏消息和游标保护原文件。
- 统一路径比较，补充隔离测试及 MCP 协议重连验证。
- 增加贡献、安全和发布文档，扩展 CI 到 Windows/Linux。

原始项目版权及 MIT 许可证保留。

- 启动清理仅删除过期心跳，不根据可复用的PID终止进程。
