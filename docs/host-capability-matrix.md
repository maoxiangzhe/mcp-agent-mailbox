# 宿主能力矩阵

> 本文档严格区分三类状态，**不得混用**：
>
> - **已完成并验证**：本机实测通过，有自动化测试或可复现证据。
> - **已实现但未完成宿主验证**：代码与契约测试已完成，但未在真实宿主上跑通端到端。
> - **设计中的未来能力**：只有设计，没有实现。
>
> `verified=False` 的能力**不得**在文档、`whoami` 或对外说明里被描述成"已支持"。

## 等级定义（设计文档 §9.3）

| 等级 | 名称 | 含义 |
|------|------|------|
| Level 0 | `tools-only` | 能发信、能主动查信；**不能**实时接收 |
| Level 1 | `notify` | 能实时显示通知；**不能**自动启动模型回合 |
| Level 2 | `wake` | 能把消息注入指定原生会话并启动模型回合 |

`whoami` 与 `list_contacts` 都按事实标注：`presence` 只有 `connected`（托管进程活着）
与 `offline` 两态；`can_wake` 表示"邮箱有对得上该宿主的注入通道，能把消息送进去并启动回合"。

## 矩阵

| 宿主 | 达到等级 | 已验证 | 实时接收 | 唤醒 | 投递方式 |
|------|----------|--------|----------|------|----------|
| DSH | **Level 2** `wake`（有凭据时）/ Level 0（读不到密钥时） | DSH-A 现场收发与批量唤醒已实测；静态矩阵声明尚为 `verified=False` | 否 | 是（本地 HTTP + 签名 cookie） | 邮箱侧直连 `POST /api/session/prompt` 注入；无凭据时排队等目标取信 |
| Codex | 原生 MCP 收发与 CLI queue 唤醒 | codex-A“为什么”的 CLI queue → 原生 MCP 取信/回复已实测；exec resume/WS 未现场验收 | 否 | 是（本机 queue 通道） | `CodexQueueWaker` 调用公开 CLI；其他路径单独验证 |
| Claude Code | Level 0 `tools-only` | 否 | 否 | 否 | 与 DSH 同理，需适配器注入会话身份 |
| OpenCode | Level 0 `tools-only` | 否 | 否 | 否 | 同上 |
| 通用（无适配器） | Level 0 `tools-only` | 是（本仓库测试覆盖） | 否 | 否 | 只能主动取信 |

用命令查看当前实现的实际声明：

```bash
uv run python -m mcp_agent_mailbox.cli adapters
```

---

## DSH

**结论：有凭据时 Level 2（可注入），读不到密钥时如实降级为 Level 0。**

已经确认的事实（对照运行中的 DSH 0.2.0 实现核实）：

1. DSH 确实有会话 inbox：它是会话日志中 `agent/inbox/spliced` 事件的**进程内投影**，
   包含 `next-turn` / `next-step` 两个队列；进程内写入走 `Agent`
   （`send` / `followup` / `steer` / `inject`），`followup` 会 `wakeDriver()` 真的起一个回合。
2. DSH 启动 MCP 子进程时会过滤掉所有 `DSH_` 前缀的环境变量，因此 MCP 服务器
   **拿不到**调用会话的 `DSH_SESSION_ID`；除非 profile 里为该 MCP 服务器静态写死
   `env:`，而静态值无法区分是哪个会话。
3. DSH 只做 MCP **客户端**，不提供 MCP 服务器；MCP 客户端不声明 sampling/elicitation，
   所以服务端无法反向让 DSH 起回合。
4. Web GUI 的 `POST /api/session/prompt` 是本项目当前使用的外部写入口。
   这是内部契约，DSH 升级后需复测；已在 DSH-A 实测，不能据此保证所有版本或配置。

5. 鉴权是**签名 cookie**：
   `dsh-auth-<base64url(sha256(authority))> = v1.<base64url(JSON payload)>.<base64url(HMAC-SHA256(secret, body))>`，
   密钥是 `$DSH_HOME/.credentials.yaml` 里 `client-connection/browser-session` 记录的
   `payload.secret`（base64url，32 字节）。

因此 `DshAdapter`：

- `probe()` 按**事实**报等级：读得到密钥 + URL 可达 → Level 2；否则 Level 0 并给出原因；
- `inject()` 返回 `unsupported`（DSH 不会主动回调邮箱；真正的注入走
  `adapters/dsh_wake.py` 的 `DshWebWaker`，由邮箱侧直连调用）；
- 读不到密钥时如实降级：投递保持 `queued`，等目标自己取信，**绝不谎报 delivered**；
- 绝不直接改写 `session.v4.jsonl.zstd` / `session_projcache`，绝不使用 UI 自动化。

**禁止事项**（写进代码与文档，防止后人"走捷径"）：

- 直接改写 `session.v4.jsonl.zstd`
- 直接改写 `session_projcache`
- UI 点击 / 键盘模拟

### 达到 Level 2 的两条路径

**已实现（邮箱侧直连）**：`adapters/dsh_wake.py` 自签 cookie 调
`POST <DSH_WEB_URL>/api/session/prompt`，把提示注入目标会话。要求
`$DSH_HOME/.credentials.yaml` 可读（DSH 重启后会重建它）。

2026-10-08 现场证据：DSH-A 收到同一批三条邮件，批量通知仅唤醒一次，按内容完成
一条回复及两条通知回执。记录见 `.local-run/communication-live-result.json`；当时运行的
DSH MCP 仍返回旧工具结构，因此新版分页和回执行为另外由隔离的真实 stdio 测试验证。

**备选（DSH 侧新代码）**：在 DSH 进程内实现 **Cordis 插件**，不依赖凭据文件：

1. 插件在进程内调用 `ctx.sessionController.resolveAgent(sessionId)` 拿到（必要时 resume）
   Agent，再 `agent.followup(createUserMessage({content, source}))` 完成注入与唤醒；
2. 插件自行暴露一个**本地传输**（例如注册本地 HTTP 路由）与邮箱 Broker 通信；
3. 注入必须带来源信封，且不得提升目标会话的权限或放宽审批策略；
4. 完成后需要通过端到端验收（DSH 会话 A 发信 → 会话 B 空闲被唤醒 → B 回复 →
   A 被唤醒）才能把 `verified` 改为 True。

```bash
uv run python -c "from mcp_agent_mailbox.adapters.dsh import level2_guidance; import json; print(json.dumps(level2_guidance(), ensure_ascii=False, indent=2))"
```

### 当前可用的 DSH 用法

在 profile 里为该 MCP 服务器显式注入会话身份（以及可选的
`MAILBOX_DSH_WEB_URL` / `MAILBOX_DSH_HOME`）后，DSH 会话可以正常：

- 注册账号（`whoami` 显示 `bound=true`、`presence=connected`）；
- 发信、收信、回复、回传处理状态；
- 有凭据时被**直接注入并开工**；没有凭据时由该会话主动 `read_conversation` 取信。

---

## Codex

**结论：实际原生 MCP 收发已在 codex-A“为什么”验证；不同唤醒适配器须分别验收。**

2026-10-08 已确认 `codex-A` 对应现有聊天“为什么”，其原生 ID 为
`01a109dd-69d0-7ff1-8c8b-defb0ddc7802`。原连接进程退出只使邮箱连接过期，聊天与账号仍在。
恢复同一账号后，目标聊天实际调用 `mailbox_inbox`、`connect_mailbox`、`reply_message`。
本次实际自动投递使用 `CodexQueueWaker` 的 `codex queue --thread <id> --message <notice>`，
触发现有目标聊天回合；不是 `exec resume`、AppServer WebSocket 或应用工具代发。
不能把这项证据描述成每一个 Codex 聊天都已加载邮箱 MCP；主工作聊天的原生工具目录
是否加载仍须单独验证。

CLI queue 路径已有上述现场证据，旧 CLI 适配器的静态声明仍为 `verified=False`，
`exec resume` 的冷会话回退尚未完成现场验收。此外 `codex_wake.py` 已实现显式配置的 AppServer
WebSocket 路径，需要已存在的本地端点和可选 `websockets` 依赖，不会启动另一 AppServer
或放宽目标聊天审批。它的协议契约测试也不能替代真实宿主验证。

使用 Codex 的**公开 CLI 子命令**，不碰内部数据库、不做 UI 自动化：

| 命令 | 用途 |
|------|------|
| `codex agents` | 列出本地 app-server 上的会话（发现可寻址 thread） |
| `codex queue --thread <id> --message <text>` | 向已有会话排队一条消息 |
| `codex exec resume <id> <prompt>` | 非交互地把消息交给已有会话并启动一个回合 |

投递逻辑：先 `queue`（会话在跑时最自然），失败再退回 `exec resume`（冷会话）。
两条路径都失败返回 `failed`，由邮箱按指数退避重试；**不会**谎报 delivered。

### 为什么 `verified=False`

CLI 适配器的静态声明尚未更新，现场证据覆盖 queue 唤醒和原生 MCP 收发，没有证明
下面每条 CLI 回退路径都在同一宿主上完成了验证。因此：

- 契约测试用**注入的假 CLI**验证调用形状（参数、顺序、失败回退、信封内容、
  不带任何沙箱/审批放宽标志），这部分是真实覆盖；
- `adapters` 静态声明仍按其实现标记未验证，文档另列现场证据，避免混为同一项验证；
- 只有在真实 Codex 上跑通"发信 → 目标会话被唤醒 → 回复 → 原会话收到"之后，
  才可以把 `verified` 改成 `True`。

### 关于"实时接收"

`can_receive_realtime=False`：Codex 不会主动回调邮箱，投递由邮箱侧发起。因此
账号在线状态仍由连接健康度决定，**不能**理解为"Codex 会主动把消息推给你"。

**禁止事项**：

- 修改 Codex 内部 sqlite / rollout 文件
- UI 自动化

---

## Claude Code / OpenCode / Trae

当前没有专门适配器：它们通过 `install.py` 注册 MCP 服务器，能力等级取决于客户端
能否注入会话身份环境变量。

- 能注入 → Level 0（可发信、可主动取信）；
- 不能注入 → `whoami` 如实报告"未绑定"，发送类工具不可用。

---

## 如何新增一个 Level 2 适配器

1. 实现 `ports.host_adapter.HostAdapter` 契约；
2. `probe()` **只读**探测，不写宿主任何状态；
3. 在 `adapters/base.py` 的 `capability_matrix()` 里如实登记等级与证据；
4. 通过 `tests/test_adapters.py` 的契约测试（去重、信封、不提升权限、状态回传）；
5. 在真实宿主上完成端到端验收后，才把 `verified` 改为 `True` 并更新本文档。
