# dsh-mailbox-plugin — DSH 每会话邮箱插件

让 **DSH 的每一个会话各自拥有一个独立的邮箱 MCP 子进程**，从而做到：

> 一个会话 ID = 一个邮箱账号；关掉一个会话，只有它那个账号离线。

对应 `mcp-agent-mailbox` 的需求：**一个会话一个账号 + 身份由宿主自动注入**。

---

## 为什么必须写插件（而不是在 profile 里配一个 `env:`）

`@deepseek-ai/dsh-mcp-client` 的配置里**拿不到当前会话 ID**，有代码级证据：

| 事实 | 证据 |
|---|---|
| `!!js` 表达式的求值时机是"该插件 fiber 激活时"，`ctx` 是该插件自己的 ctx | `cordis/lib/index.js:1345-1348`、`cordis-plugin-loader/src/index.ts:104-113`、`config/utils.ts:5-22` |
| preset 的插件树是**按 preset 修订版共享**的，不在 agent fiber 下 | `dsh-agent-preset-registry/lib/index.js:61`（"Runtime plugin trees shared by Agents selecting one preset revision"）、`:143`（"the mount is not under the agent's fiber"）、`:570`（引用计数不到 0 不回收） |
| profile 根行的插件只装载一次 → 全进程一个子进程、一个账号 | 同上 + 实测：真实库里 `host_type='dsh'` 只有一个账号，却已有 33 代连接 |

所以写在 `env:` 里的会话 ID 必然是**静态值**，会导致同一 DSH profile 的所有会话共用同一个邮箱账号（实测后果：第二个会话既注册不上自己，还顶用了第一个会话的身份）。

**DSH 自己给的答案**是在新建 Agent 时把会话 ID 写进子进程环境：

```
dsh-api-terminal-controller/lib/index.js:1007
    env: { DSH_SESSION_ID: agent.id },
```

本插件照同一个姿势做，只不过挂的是邮箱 MCP 客户端。

## 身份为什么可信

| 事实 | 证据 |
|---|---|
| Agent 登记时断言 `agent.id === agent.session.id` | `dsh-agent/lib/index.js:512` |
| Agent 创建事件串行派发，可 await（保证首轮就有工具） | `dsh-agent/lib/index.js:572-588` 的 `announce()` → `ctx.serial(..., 'agent/created', { agent, source, signal })` |
| mcp-client 按 **scope** 隔离命名空间，Agent 作用域可同名 | `dsh-mcp-client/lib/index.js:773`（`activeServerNames` 是 WeakMap）、`:812`（`scopeOf(ctx) ?? ctx.root`） |

因此 `agent.id` 由宿主给出、模型无法指定；邮箱侧据此注册的账号天然"一会话一账号"，也**不再需要** `--allow-adapter-registration`（模型连提交身份的机会都没有）。

## 依赖解析（一个踩过的坑，务必知道）

插件**不能在文件顶部写** `import '@deepseek-ai/dsh-mcp-client'`，也**不能**用
`createRequire` 从磁盘目录解析它：

> 该包**打在 DSH 的 asar 归档内部**（`app.asar`），磁盘上不存在它的 `node_modules`
> （profile 的 `node_modules` 只放 profile 自己装的 bundle）。实测：
> `createRequire('C:\Users\<user>\.dsh\profiles\desktop/package.json')('@deepseek-ai/dsh-mcp-client')`
> → `Cannot find module`。

DSH 自己能加载它，是因为它给 root include 装了 `HostResolvedRootInclude`，把裸包名交给
`internal.import(specifier, bareModuleBaseUrl)` 解析（`dsh-app-boot/lib/index.js:3692-3701`）。

所以插件采用**多级回退**解析（每一级的失败都会记下来，最终错误信息里能看出卡在哪一层）：

1. `loader.import('@deepseek-ai/dsh-mcp-client')` —— 与"本插件被谁加载"同一套解析
2. `loader.import(<asar 内候选路径的 file URL>)` —— 按 `process.resourcesPath` /
   `process.execPath` 反推 `…\resources\app.asar\dsh\node_modules\…`
3. `createRequire(<候选目录>)` —— Electron 运行时下 asar 路径可以当目录用

> 注意：`ctx.loader` 是 **Loader 实例**，不是 `HostResolvedRootInclude`。DSH 的裸包名解析
> 走的是后者的 `import`（`dsh-app-boot:3692-3701`，把 `bareModuleBaseUrl` 闭包进去），
> 而那个 URL 是**启动器**传给 `boot()` 的，插件拿不到。所以第 1 级未必命中，第 2/3 级是必需兜底。

## 文件

| 文件 | 作用 |
|---|---|
| `index.js` | 插件本体：`agent/created` 时把 mcp-client 挂到 `agent.ctx`，注入 `MAILBOX_SESSION_ID = agent.id` |
| `plugin.patch.yml` | 插件行配置（`cordis:include` 或 bundle 用） |
| `package.json` | bundle manifest（`dsh.bundle.patch`） |
| `_selftest/` | 离线自检（不需要真实 DSH） |

## 装载（二选一）

### 方式 1：官方推荐——`plugin_manager` 的 `install_bundle`

在 DSH 会话里让模型调用 `plugin_manager`，`action: install_bundle`，`target` 填本目录绝对路径：

```
E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin
```

> 注意：`install_bundle` 属于 Creator 模式的工具。若当前会话没有该工具，用方式 2。

### 方式 2：`cordis:include`

在 `$DSH_HOME/profiles/<profile>/cordis.patch.yml` 里加：

```yaml
- insert:
    - id: dsh-mailbox-per-session-include
      name: cordis:include
      config:
        # 必须写**绝对路径**：include 的 config.path 是相对 profile 目录解析的
        # （dsh-app-boot/lib/index.js:128）。
        path: E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin\plugin.patch.yml
```

`plugin.patch.yml` 里的 `name: './index.js'` 会**锚定在该 patch 文件旁**
（`dsh-app-boot/lib/index.js:3540`：`entry.name` 以 `./` / `../` 开头时按 patch 所在目录解析），
所以插件文件留在工作区即可，不必装进 profile 的 `node_modules`。

装载后按 HMR 重载；若提示 `restart-required`，重启一次 DSH。

## 配置项（`plugin.patch.yml` 的 `config`）

| 键 | 必填 | 说明 |
|---|---|---|
| `command` | ✅ | 拉起邮箱 MCP 的可执行文件（建议用 venv 里的 python） |
| `args` | | 默认 `['-m', 'mcp_agent_mailbox.cli', 'serve']` |
| `cwd` | | 子进程工作目录 |
| `mailboxHome` | | 邮箱数据目录（决定用哪份 SQLite），对应 `MAILBOX_HOME` |
| `serverName` | | 默认 `mailbox`，模型侧工具名前缀 |
| `hostInstanceId` | | 默认 `default`，对应 `MAILBOX_HOST_INSTANCE_ID` |
| `toolCallTimeoutMs` | | 默认 60000 |
| `failOnStartupError` | | 默认 false |

**能力等级固定在 `0`（tools-only）**：DSH 目前不能实时唤醒，谎报 2 会让界面显示"可唤醒"而实际叫不醒——本项目明令禁止。

## 装载前预检（先跑这个，别拿安装当试错）

```powershell
& "E:\zcz\modle\MCP\mcp-agent-mailbox\.venv\Scripts\python.exe" -X utf8 preflight.py
```

它按 DSH Loader 的实际解析规则检查 17 项：`dsh.bundle.patch` 是否声明且存在、
patch 顶层是否为数组、`insert` 行的 `id`/`name`/`config` 是否齐全、`name` 是否以 `./`
开头（否则不会锚定到 patch 文件旁）、入口文件是否 ESM、`config.command` 指向的可执行
文件是否真实存在、以及是否残留 `node_modules/` 替身包。退出码 0 才算可以安装。

## 离线自检

```powershell
$node = "E:\Ai\node.exe"   # 或任意 Node >= 20.6
cd E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin
& $node _selftest/selftest.mjs
```

自检用 `ctx.loader.import` 替身提供 mcp-client（与真机同一条解析入口），造假的 Cordis ctx
触发 `agent/created`，覆盖 20 条断言：

- 两个会话各自挂载一次，且 `MAILBOX_SESSION_ID` 等于各自的 `agent.id`
- 同一会话重复触发只挂载一次（幂等；否则会起两个子进程、两个账号）
- 能力等级恒为 `0`（不谎报唤醒能力）
- 空 `agent.id` / 缺 `command` 时安全跳过，且不误伤后续会话
- 某个会话挂载抛错被隔离，不冒泡、不阻断其他会话
- `agent/disposed` 后同一 id 可重新挂载

替身会把每次真实调用**落盘**到 `_selftest/apply-log.jsonl`，作为独立于内存的计数证据。

## 尚需在真实 DSH 上确认（本插件未做的验证）

1. `agent.ctx.plugin()` 在真实 cordis 里返回的对象形状（自检里按 `{ ctx: { fiber } }` 模拟）；
   若真实返回不同形状，`await mcpCtx.fiber.await()` 会被跳过（有 `typeof` 保护，不会崩）。
2. 慢启动 MCP 服务器下，串行 `agent/created` 的首轮工具就绪时序。
3. `agent/disposed` 事件的真实载荷字段（自检同时兼容 `event.agent.id` 与 `event.id`）。

## 与 profile 里旧配置的关系

装载本插件后，**必须**从 profile 的 patch 里删掉写死的 `MAILBOX_SESSION_ID`
（以及原来那行 profile 级 `mcp-mailbox`），否则：
旧行会在启动时抢占那个静态账号，`connect_mailbox` / 真会话都会被拒（已实测复现）。
