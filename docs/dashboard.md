# 只读 Web 监控台

设计依据：[`docs/read-only-dashboard-design.md`](read-only-dashboard-design.md)。

一句话：**它是现有权威状态的只读投影，不是第二套邮箱客户端。** 页面上没有、API 里也
不存在任何写操作。

## 启动

```bash
uv run python -m mcp_agent_mailbox.cli migrate      # 首次需要先建库
uv run python -m mcp_agent_mailbox.cli dashboard
uv run python -m mcp_agent_mailbox.cli dashboard --host 127.0.0.1 --port 8765
```

启动时会打印带临时令牌的访问地址，例如：

```text
MCP 智能体邮箱 · 只读监控台
  数据库（只读）：mailbox.sqlite3
  访问地址：http://127.0.0.1:8765/?token=<临时令牌>
  令牌：abcd…wxyz（只在本次启动有效，不写入任何文件）
  安全：仅本机回环、Bearer 鉴权、严格 CSP、无任何写操作
  按 Ctrl+C 停止。
```

- 默认且建议绑定 `127.0.0.1`；
- **非回环地址会打印明显安全警告**（并说明风险），不阻止你，但会反复提醒；
- 端口被占用时**明确报错并提示 `--port`**，绝不静默换端口（静默换端口会让你访问到
  错误的地址）；
- Ctrl+C 停止会释放 HTTP 线程与数据库连接。

## 为什么它不可能写库

三重保证，任何一层单独都足够：

1. **独立的只读连接**：所有查询走 `infrastructure/sqlite/read_only.py`，用
   `file:...?mode=ro` 打开，并立即 `PRAGMA query_only=ON`。SQLite 自己会拒绝任何写语句
   （测试里直接断言 `UPDATE` / `DELETE` / `CREATE TABLE` 都会抛 `OperationalError`）。
2. **路由表里只有查询**：`dashboard/` 包不导入 `daemon` / `application` / `migrations`，
   有测试用 AST 扫描这条依赖禁令。
3. **REST 层没有写方法**：`POST` / `PUT` / `PATCH` / `DELETE` 对 `/api/*` 一律 405。

另外，监控台**不**持有 Broker、不触发维护循环、不推进已读位点、不改处理状态。
`tools/dashboard_smoke.py` 会在浏览全部页面与 API 前后，对全部业务表逐行哈希比对，
证明内容完全一致（见下文"验收"）。

> 注意：只读连接**不**使用 `immutable=1`。数据库确实会被其它进程并发写，必须让 SQLite
> 正常参与锁协议才能读到最新已提交数据。

## 安全模型

消息正文是敏感数据，即使只监听本机也必须鉴权。

| 措施 | 实现 |
|------|------|
| 临时令牌 | 启动时 `secrets.token_urlsafe(32)` 生成，只存在内存 |
| 鉴权 | 除 `/api/health` 外全部要求 `Authorization: Bearer <token>`，否则 401 |
| 令牌不落盘 | 不写项目、不写数据库、不写日志文件；日志里只出现脱敏形态（`abcd…wxyz`） |
| 令牌不进页面 | 静态资源里不含令牌（有测试断言） |
| 前端存储 | 只用 `sessionStorage`（窗口关闭即失效），**不用** `localStorage` |
| 地址栏清理 | 页面读取令牌后立刻 `replaceState` 抹掉它，避免进入历史与 Referer |
| CSP | `default-src 'none'`，脚本/样式/字体/连接全部 `'self'`，`frame-ancestors 'none'`，`base-uri 'none'` |
| 其他头 | `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`、`Cache-Control: no-store` |
| CORS | 不返回 `Access-Control-Allow-Origin`，默认拒绝跨域读取 |
| 错误脱敏 | 错误响应只有稳定机器码 + 中文提示，不含堆栈、SQL、绝对路径或消息正文 |
| XSS | 前端所有文本经 `textContent` / `createElement` 渲染；测试断言不存在 `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write` |
| 无外部依赖 | 不使用 CDN、远程字体或远程脚本（测试断言） |

`/api/health` 刻意无需令牌：它只返回服务自身状态（是否可读、schema 版本），不含任何
业务数据，且未授权时也不透露实体是否存在。

## API

统一响应：

```json
{ "ok": true, "data": {}, "meta": { "generated_at": "ISO-8601", "next_cursor": null, "has_more": false, "total": 0, "count": 0, "page_size_limit": 200 } }
```

| 方法与路径 | 用途 |
|---|---|
| `GET /api/health` | 存活与只读状态（**无需令牌**） |
| `GET /api/overview` | 计数、积压、死信、在线分布、能力分布、最近事件时间线 |
| `GET /api/accounts` | 分页账号：`page_size`、`cursor`、`host_type`、`presence` |
| `GET /api/accounts/{id}` | 账号详情：连接历史、托管进程 PID、注入依据、统计 |
| `GET /api/conversations` | 分页对话：`page_size`、`cursor`、`account_id`、`unread_only`、`blocked` |
| `GET /api/conversations/{id}` | 对话元数据、参与者、投递状态分布 |
| `GET /api/conversations/{id}/messages` | 消息分页：`page_size`、`after_message_id`、`before_message_id` |
| `GET /api/deliveries` | 投递分页：`page_size`、`cursor`、`state`、`account_id`、`conversation_id` |
| `GET /api/diagnostics` | 数据库/迁移/Broker/能力/配置诊断 |

约定：

- 只允许 `GET` / `HEAD` / `OPTIONS`；写方法一律 **405**（带 `allowed` 列表）；
- **`total` 来自独立聚合查询**，不是当前页长度；
- 所有列表稳定排序（账号按创建时间+ID，对话按活动时间+ID，投递按自增序号，消息按
  入队 rowid）并带不透明游标；`page_size` 上限 200；
- 不存在资源 **404**，非法参数/游标 **400**，数据库忙 **503**（脱敏）；
- 查询期间资源被删除时返回 **404** 并提示刷新，不会 500。

## 状态表达（不允许混淆）

页面与 API 都把三个维度**分开**返回，并各带一句人类可读说明：

```text
delivery:   queued -> dispatched -> delivered | failed | dead_letter
visibility: unread -> seen
processing: pending -> running -> completed | blocked | cancelled | failed
```

| 说法 | 真实含义 |
|------|----------|
| `delivery=delivered` | 宿主已**可靠接收**；**不代表**模型已阅读或任务完成 |
| `visibility=seen` | 调用者推进了可见性；不代表已处理 |
| `processing=completed` | 处理回执；不等于已回复（回复是带 `reply_to` 的新消息） |

宿主能力也分开表达：

| 字段 | 含义 |
|------|------|
| `capability_level` | 适配器**声明**的等级（0 tools-only / 1 notify / 2 wake） |
| `connected` | 当前是否有健康连接 |
| `can_wake` | 声明等级 ≥ 2 **且**连接健康（能力"存在"） |
| `verified` | 是否已在**真实宿主**上端到端验证 |

`verified` 直接来自适配器代码里的登记（`adapters.capability_matrix()`），**不是**从
`capability_level` 推断的。因此界面永远不会把"Level 2 声明"显示成"已验证"。
当前 DSH 是 Level 0，Codex 是 Level 2 但 `verified=false`，监控台会如实显示未验证项。

## 诊断页会显示的"未实现/未验证"事实

- 数据库路径只显示**文件名**，不显示绝对路径；
- SQLite 完整性检查、WAL 日志模式、schema 版本、每张表行数；
- 迁移列表（并说明监控台**不会**执行迁移）；
- Broker 仅当前进程实例；**跨进程实时事件通道未实现**（原文说明）；
- 每个适配器的声明等级、`verified`、证据、未验证项、禁止事项；
- 在线状态只有 `connected` / `offline` 两态，并且会把**注入依据**
  （`wake_basis`：`direct_channel` / `declared_level2` / `no_channel` / `offline`）写清楚；
- 当前生效的非敏感配置（心跳/租约/上限/速率/暂停开关/是否记录正文）；
  心跳与租约**只作诊断展示**，不参与在线判定。

## 页面

两个入口：**对话 / 监控**。默认进入对话，列表显示参与者名称和最近消息摘要；选中后直接显示消息正文。

监控内的**页面设置 / 通讯概况 / 账号与连接 / 当前对话与消息详情 / 对话筛选 / 投递记录 / 详细参数与诊断**全部默认折叠。原生会话 ID、PID、连接证据、能力等级、投递/可见性/处理状态及详细配置都在这里查看。监控保留只读行为，页面设置仅控制浏览器刷新间隔，不修改邮箱配置。

- 首次加载只请求对话列表；展开监控项后才加载相应明细；
- 对话采用主从布局（左列表、右详情），消息按需分页；`reply_to` 可跳转并高亮被回复消息；
- 主对话页保留发件人、收件人、时间、正文和引用跳转；三个状态与说明集中在监控的消息详情；
- 失败原因默认折叠；长 ID 截断但完整值放在 `title` 里；
- 手动刷新 + 5/15/30 秒自动刷新（**默认关闭**），并显示"最后成功刷新"时间；
- 请求失败时明确标注**当前显示的是旧数据**，不会把旧数据伪装成刷新成功；
- 空 / 加载 / 鉴权失败 / 数据库忙 / 资源消失各有独立文案；
- 旧请求结果按序号丢弃，筛选防抖，避免结果乱序；
- 键盘可操作（方向键在导航间移动、`Home`/`End`、跳转链接、可见焦点环）；
- 状态不只用颜色：每种状态都带图标与文字；
- 尊重 `prefers-reduced-motion`；320 / 768 / 1024 / 1440 宽度均不产生页面级横向滚动
  （宽表格在自身容器内横向滚动）。

视觉方向：石墨深色底、琥珀信号色、青绿正常、红橙故障；等宽字体承担数据展示，
字体栈不使用 Arial / Inter / Roboto 一类通用默认字体。

## 验收与测试

```bash
uv run pytest tests/test_dashboard_api.py tests/test_dashboard_static.py tests/test_dashboard_cli.py
uv run python -X utf8 tools/dashboard_smoke.py     # 真实子进程 + 真实 HTTP 的端到端验收
uv run python run_tests.py                          # 全量回归（含旧版兼容）
```

`tools/dashboard_smoke.py` 会：

1. 建一份覆盖各种状态的种子库；
2. 对全部业务表做逐行哈希快照；
3. 用**真实子进程**启动 `cli dashboard`，从启动横幅里取令牌，再用 `urllib` 发真实
   HTTP 请求走完全部 API、鉴权、405、分页、安全头与静态资源；
4. 停止服务后重新快照并逐表比对，证明监控台**没有写入任何业务状态**。

## 已知边界

- 单机单用户：与邮箱其它部分一致，不做多租户与跨机访问。
- 没有实时推送：页面靠手动/定时刷新轮询（SSE/WebSocket 未实现）。
- 没有"仅某账号可见"的过滤：可见范围等于操作系统账号的读写范围。
- 非回环监听只是"允许"，不提供 TLS；令牌会出现在 URL 里，仅适合本机或完全可信的内网。
