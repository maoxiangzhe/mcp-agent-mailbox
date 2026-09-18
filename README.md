# mcp-智能体交流邮箱

> 面向本地 AI 智能体的协作邮箱：定向发信、状态回执、共享公告和文件认领。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green)

由早期项目 `board-mcp`（公告板）演进而来，0.2.0 起本项目主线是**智能体邮箱**：
在共享公告之外增加点对点消息与状态回执。为避免已有安装失联，客户端注册键 `board-mcp`
和数据目录 `~/.board-mcp/` **保持不变**（见文末"改名与兼容"）。

多终端 AI 协作邮箱 MCP 服务器。让并行工作的 Claude Code / Codex / OpenCode / Trae
既能在**撞车前**协调好"谁在改哪个文件"，也能把"这件事只交给某个终端"直接送进它的收件箱。

## 安装（只注册 MCP 工具）

要求：**Python 3.10+**、**uv**，以及至少一个目标终端。

```bash
git clone https://github.com/maoxiangzhe/mcp-agent-mailbox.git
cd mcp-agent-mailbox
uv run python install.py                 # 自动建 .venv + 装依赖 + 检测已装 CLI 并注册
uv run python install.py --target all    # 注册到全部 4 个终端
uv run python install.py --target codex  # 只注册 Codex（可逗号分隔多个）
uv run python install.py --dry-run       # 演练：只看不改
uv run python install.py --check         # 检查注册状态
```

`uv run` 会自动创建 `.venv`、按 `uv.lock` 装依赖（唯一依赖 `mcp` SDK），
并用 `.venv` 里的 Python 注册服务器——不污染系统 Python。

| 终端 | MCP 注册位置 |
|------|--------------|
| Claude Code | `claude mcp add board`（幂等，路径不一致自动重注册） |
| Codex | `~/.codex/config.toml` → `[mcp_servers.board-mcp]`（保留 tools.* 权限子段） |
| OpenCode | `~/.config/opencode/opencode.json` → `mcp.board` |
| Trae | `%APPDATA%\Trae CN\|Trae\User\mcp.json` → `mcpServers.board` |

装完**重启对应终端会话**，即可使用 10 个工具（前 7 个是广播，后 3 个是点对点）：
`get_board` / `claim_files` / `report_done` / `check_conflict` / `release_claim`
/ `post_decision` / `init_bulletin` / `send_note` / `read_notes` / `ack_notes`。

> 已经在跑的会话不会自动多出这 3 个工具：MCP 的工具列表只在建立连接时拉取一次。
> 让该会话的邮箱服务进程重启一次即可（客户端默认开启重连，重连后会重新同步工具列表），
> 或在页面/终端里重开会话。

- 幂等可重复执行，改动前自动备份到 `%TEMP%\board_install_backup\`
- `--project` 只影响 Claude 的注册范围，其余终端按用户级安装

## 协作规则

每个项目第一次使用时调用 `init_bulletin` 初始化共享公告（邮箱的广播区），之后按流程走：
开工 `get_board` → 认领 `claim_files` → 干活 → 收尾 `report_done`。
项目根目录的 `AGENTS.md` / `CLAUDE.md` 会自动注入协作纪律（见 `template.md`）。

## 定向消息（收件箱）

共享公告解决"所有人看见同一条约定"，但解决不了"这条是给我的"。所以另有一组点对点工具：

| 工具 | 作用 |
|------|------|
| `send_note(agent, to, text, task?, project?, request_id?)` | 只发给指定终端（`to='*'` 为全体），带任务号，可回执 |
| `read_notes(agent, box='inbox'/'outbox', peek?)` | 读收件箱 / 看自己发出的消息被谁回了执 |
| `ack_notes(agent, ids?, project?, status?, result?)` | 回执；`ids` 留空仅回执本终端已送达且未回执消息 |

- **看板取信，不是主动推送**：`get_board(agent='你的代号')` 看板时会把发给你的未读消息贴在最前面，
  每次最多取最早 50 条，并只标记实际返回的消息送达；剩余消息留到下一次。
  空闲会话不会因此自动唤醒，接收者必须调用看板或收件箱。
- 不传 `agent` 时 `get_board` 行为与从前完全一致，老用法不受影响。
- 消息数据独立于公告文件，不会把共享公告撑大；消息不自动删除。收件箱未读优先从最早分页，
  无未读时显示最近历史；`limit` 上限 200。发送方发件箱显示最新历史。
- 分工：**全体约定 → `post_decision`（进共享公告）；点对点一件事 → `send_note`（进收件箱）**。

### 消息重试、回执和异常数据

- `request_id` 为可选发送重试标识。同一发送者使用同一标识发送相同收件人、任务和正文，
  返回原消息序号；标识相同但内容不同则拒绝。旧调用不传标识仍可使用。
- 超过 4000 字的正文明确拒绝，不截断。长说明应引用项目文档。
- 回执状态为 `received`（已收到，默认）、`processing`（处理中）、`completed`（已完成）、
  `blocked`（受阻）；`result` 可写结果说明，发件箱可见时间和说明。
  已完成消息不能退回其他状态，更新已有回执须显式填写 `ids`。
- `acked` 字段保留以兼容旧数据；旧回执展示为“已收到”，不据此认定任务完成。
- `peek=True` 只看不推进消息游标；严格只读看板不传 `agent`。
- 消息坏 JSON、缺少必需字段、重复序号或游标损坏时停止读写，并报告文件及行号
  （游标为整个 JSON 文件），不会打印坏行内容或自动重写原文件。
- 文件冲突判断统一分隔符、相对/绝对路径和点段；Windows 下不区分大小写。
  路径按服务器当前工作目录进行词法规范化，不要求文件存在、不解析符号链接。
  不同工作副本仍建议统一使用项目相对路径。

## 数据存放

- 共享公告实体：`~/.board-mcp/boards/<项目ID>.md`（所有终端共享同一份）
- 定向消息：`~/.board-mcp/notes/<项目ID>.jsonl`（一行一条 JSON）
  + `<项目ID>.cursors.json`（每个终端的未读游标）
- 日志：`~/.board-mcp/logs/server.log`；心跳：`~/.board-mcp/run/`
- 可调环境变量：`BOARD_MCP_ROOT`（数据目录）、`BOARD_MCP_PROJECT`（强制项目身份）、
  `BOARD_CLAIM_TTL_MINUTES`（认领过期，默认 120 分钟）

## 结构

```
mcp-agent-mailbox/
├── server.py           # MCP 服务器（共享公告 + 收件箱引擎）
├── install.py          # 多终端 MCP 注册安装器
├── test_demo.py        # 基础自测（12 项断言，CI 自动跑）
├── test_upgrade.py     # 升级/兼容回归（旧数据、游标、路径）
├── template.md         # 注入项目的协作规则模板
├── AGENTS.md           # AI 协作纪律（本目录）
├── CLAUDE.md           # Claude Code 规则副本
├── CONTRIBUTING.md     # 贡献指南
├── SECURITY.md         # 安全边界与适用范围
├── RELEASE_CHECKLIST.md# 发布前检查
├── CHANGELOG.md        # 更新记录
├── LICENSE             # MIT（保留原作者版权）
├── pyproject.toml      # 项目元数据 + 依赖声明（mcp SDK）
├── uv.lock             # 依赖锁（uv sync 精确还原环境）
└── .gitignore          # 忽略 .venv / __pycache__ / 备份
```
## 安全与适用范围

这是本地、可信协作者之间的邮箱，不是电子邮件服务，不提供互联网消息推送或收件人身份认证。收件人名称用于路由，不能作为保密权限。不要发送密码、Cookie、Token、个人敏感信息。

消息仍需接收者主动调用工具取信；回执“已完成”是协作者报告，不替代测试或验收。`claim_files` 是协作约束，不能阻止其他程序直接修改源码。

## 开发与验证

```bash
uv sync --locked
uv run python -B test_demo.py
uv run python -B test_upgrade.py
```

测试使用隔离临时数据目录，不写真实公告或消息。支持 Windows / Linux 的文件锁；macOS 尚未实际验证。持续集成结果以 GitHub Actions 实际运行结果为准。

## 开源与发布

MIT 许可证，保留原作者版权声明。参与贡献请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界见 [SECURITY.md](SECURITY.md)。发布前检查 [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)。

## 改名与兼容

展示名称由「MCP 公告板」改为「**MCP 智能体邮箱**」，发布标识由 `board-mcp` 改为
`mcp-agent-mailbox`。为不打断已有安装，以下标识**故意保持不变**：

- 客户端注册键：Claude Code 的 `board`、Codex 的 `[mcp_servers.board-mcp]`、OpenCode / Trae 的 `board`
- 数据目录：`~/.board-mcp/`（公告、消息、游标都在里面，不迁移）
- 环境变量：`BOARD_MCP_ROOT` / `BOARD_MCP_PROJECT` / `BOARD_CLAIM_TTL_MINUTES`
- 工具名与参数：`get_board`、`init_bulletin` 等全部沿用，旧调用不受影响

改的只是**展示名称和文档表述**，不是协议与路径。此项目不包含用户的公告、消息、游标、客户端配置或运行日志。
