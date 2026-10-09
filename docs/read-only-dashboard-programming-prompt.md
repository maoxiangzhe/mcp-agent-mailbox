# MCP 智能体邮箱只读监控台：编程提示词

你是一名拥有 10 年经验的资深 Python、Web 与系统工程师。请在现有仓库中实现 MCP 智能体邮箱的只读 Web 监控台。

项目目录：

```text
E:\zcz\modle\MCP\mcp-agent-mailbox
```

开始前必须完整阅读：

1. `AGENTS.md`
2. `docs/read-only-dashboard-design.md`
3. `docs/session-addressed-agent-mailbox-design.md`
4. `docs/migrations.md`
5. `docs/broker.md`
6. `docs/host-capability-matrix.md`
7. `mcp_agent_mailbox/cli.py`
8. 现有领域、应用、仓储、Broker 和测试代码

## 任务目标

实现一个本机运行的只读监控台，展示：

- 账号和当前在线/连接状态；
- 对话和参与者；
- 消息正文、回复关系；
- delivery、visibility、processing 三套独立状态；
- 投递积压、失败、重试和死信；
- SQLite、Broker、配置和宿主能力诊断。

设计文档 `docs/read-only-dashboard-design.md` 是本任务的权威需求。若它与旧 README 或注释矛盾，以当前代码事实和该设计文档为准，并在最终报告中说明差异，不要自行扩大需求。

## 绝对约束

1. 只读：不得实现发送、回复、标记已读、回执、重试、删除、注册、续租、唤醒或配置修改。
2. 页面加载和 API 查询不得改变任何业务表、未读位点、投递状态或处理状态。
3. 不得让浏览器直接读取 SQLite，不得把日志、Markdown 或渲染表格当作权威状态。
4. 不改变现有 MCP 工具协议、身份模型和消息状态机。
5. 不把 Level 2 接口存在误报为真实唤醒已经验证。
6. 不新增 Node.js 构建链，不使用 CDN、远程字体或外部脚本。
7. 优先使用 Python 标准库和现有依赖；若认为必须新增依赖，先停止并解释必要性、维护成本和替代方案。
8. 不覆盖用户已有修改，不使用 `git reset --hard`、`git checkout --` 等破坏性命令。
9. 所有消息正文必须按纯文本渲染，禁止通过 `innerHTML` 注入。
10. API 总数必须使用真实聚合查询，不能用当前分页长度冒充全局总数。

## 实现要求

### 后端

- 新建边界清晰的 `mcp_agent_mailbox/dashboard/` 包；
- 提供查询门面、DTO 序列化、HTTP 路由和静态资源；
- CLI 新增 `dashboard` 子命令；
- 默认监听 `127.0.0.1`，默认端口建议 `8765`；
- 启动时生成临时高熵令牌，并输出本地访问 URL；
- API 使用 Bearer Token；不把令牌写入持久化文件或普通日志；
- 拒绝跨域读取并设置 CSP、`nosniff`、`no-referrer`；
- `/api/*` 仅接受 GET 以及必要的 HEAD/OPTIONS，写方法返回 405；
- 所有列表采用稳定排序、有限 page size 和游标；
- 错误响应脱敏，不返回堆栈、SQL、绝对路径或正文；
- 正确释放 HTTP 服务、后台线程和数据库连接。

至少实现：

```text
GET /api/health
GET /api/overview
GET /api/accounts
GET /api/accounts/{id}
GET /api/conversations
GET /api/conversations/{id}
GET /api/conversations/{id}/messages
GET /api/deliveries
GET /api/diagnostics
```

### 前端

- 使用语义化 HTML、CSS 和原生 JavaScript；
- 视觉方向：深色石墨“本地通信枢纽”，琥珀信号色，避免紫色渐变模板；
- 包含总览、账号、对话、投递、诊断五个主区域；
- 对话采用主从布局，消息正文按需分页加载；
- 状态不能只靠颜色，必须同时有明确文字；
- 明确显示 `connected`、`can_wake`、`verified` 和降级原因；
- 提供手动刷新和默认关闭的 5/15/30 秒自动刷新；
- 展示最后成功刷新时间，失败时不得把旧数据伪装成最新结果；
- 支持键盘操作、可见焦点、`prefers-reduced-motion`；
- 在 320、768、1024、1440 像素宽度下可用且无页面级横向滚动。

## 工作方式

1. 先检查 Git 状态、现有代码和仓储接口，不要假设设计文档中的接口已经存在。
2. 列出准备新增和修改的文件，以及每个文件的职责。
3. 先写失败测试，再实现最小代码使测试通过。
4. 每次只处理一个清晰边界：查询层、API、鉴权与安全头、页面、CLI、文档。
5. 复用领域模型和仓储事务语义；不要复制一套业务逻辑到 dashboard。
6. 如果现有仓储缺少只读聚合，增加窄而明确的查询接口及测试，不顺手重构无关模块。
7. 实现后检查数据库前后状态，证明浏览行为没有写入。
8. 运行新增测试，再运行完整回归：

```bash
uv run python run_tests.py
```

9. 不要仅凭静态页面或单元测试宣称“产品可用”；至少真实启动本地服务器，通过 HTTP 完成鉴权、列表、详情、分页和写方法拒绝的冒烟验证。

## 必测场景

- 空数据库；
- 多账号、多连接和租约过期；
- 超过一页的对话、消息、投递；
- queued、dispatched、delivered、failed、dead_letter；
- unread/seen 与 processing 状态独立显示；
- reply_to 跳转；
- Level 0、Level 1、声明 Level 2 但 `verified=false`；
- 无令牌、错误令牌、写方法；
- 消息中包含 `<script>`、HTML、长文本和 Unicode；
- 数据库忙、资源在刷新间消失、非法游标；
- 320 像素窄屏和键盘导航；
- 启停两次无端口、线程或连接泄漏。

## 禁止的虚假完成

- 不得用硬编码演示数据冒充数据库结果；
- 不得把 SQLite 文件存在当作 Broker 在线；
- 不得把 `capability_level=2` 当作 `verified=true`；
- 不得把 `delivered` 当作任务完成；
- 不得把当前页计数当作全局总数；
- 不得在测试中直接调用内部函数来冒充真实 HTTP 验收；
- 不得只报告“页面能打开”，必须分别报告数据正确性、安全边界和回归结果。

## 完成报告格式

完成后按以下顺序报告：

1. 实现了什么；
2. 新增或修改的文件；
3. API 和页面如何保持只读；
4. 安全机制；
5. 实际运行的测试命令与原始结果摘要；
6. 真实 HTTP 冒烟验证结果；
7. 尚未实现、未验证或受宿主限制的能力；
8. Git 工作树状态和是否存在与本任务无关的改动。

只有在上述验收完成且不存在未说明失败时，才能称本轮实现完成。
