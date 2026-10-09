# 独立验证报告（第三方复核，未改动被验证文件）

**验证者**：另一个 DSH 会话（subagent），与作者会话并行工作。
**被验证对象**：`E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin\`（`index.js` / `package.json` / `plugin.patch.yml` / `_selftest/` / `preflight.py`）。
**快照**：`index.js` 取证起点 278 行 / 收尾 299 行，SHA256 见文末；该文件在本复核期间被作者会话改了至少 3 次，故结论一律**按结构特征**锚定，不按行号。
**运行环境**：Node `v24.18.0`（`E:\Ai\node.exe`），真实 `@deepseek-ai/cordis@4.0.4` 与真实
`@deepseek-ai/dsh-mcp-client` 的 `Config` schema，均从 `E:\zcz\deepseekHARNESS\resources\app.asar` 只读提取。

> 本目录（`_evidence/`）是**复核产物**，不是交付物；不改动 `index.js` / `package.json` /
> `plugin.patch.yml` / `README.md` / `_selftest/` / `preflight.py`。

---

## 0. 怎么复现（一条命令）

```powershell
cd E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin
node _evidence/verify/verify.mjs
```

它做三件事，全部在**真实 cordis 4.0.4** 上跑：

| 步骤 | 脚本 | 验证什么 |
|---|---|---|
| 1 | `_evidence/probe/probe-cordis.mjs` | `ctx.plugin()` 的返回形状、`agent/created` 作用域路由、scope 隔离、dispose 级联、真实 `Config` schema |
| 2 | `_evidence/probe/verify-existing.mjs` | 把**现有 `index.js`** 当插件装进真实 cordis，用真实 carrier 串行派发 `agent/created` |
| 3 | `_evidence/probe/verify-config-schema.mjs` | 现有插件**实际构造出的 config 对象**，拿去喂真实 `mcp-client` 的 `Config` |

前置：`_evidence/probe/node_modules/` 里要有从 asar 提取的 cordis（已就位；重新生成用
`_evidence/asar_materialize.py`）。解析锚点 `_evidence/verify/anchor/` 里的 stub 由
`verify.mjs` 每次自动刷新，指向插件 `resolveMcpClient()` 使用的同一个 `$DSH_PROFILE_DIR` 锚点。

---

## 1. 先行结论：当前 `index.js` 有一处**仍存在**的必崩缺陷（D1）；另有一处已被作者修掉（D2，记录在案）

> ⚠️ 作者会话在本复核期间持续改写 `index.js`。以下两条都请以**结构**为准（`inject` 是否含
> `'loader'`；`apply` 是否为 `async`），而不是以行号为准。

### 缺陷 D1 —— `ctx.loader` 没有 `inject`，`agent/created` 会抛错并冒泡到 Agent 创建 [已验证，**当前仍存在**]

**收尾复测（259 行版本，SHA256 7F69293F...50EE）**：`node _evidence/verify/verify.mjs` 依旧

```
[PASS] V0.bundle_activated: fiber state=2
Error: cannot get property "loader" without inject     (x2，两个端到端脚本各一次)
独立验证：2 个脚本失败
```

`index.js:98`：

```js
const loaderImport = ctx?.loader?.import;
```

`export const inject = ['agents']`（`index.js:42`）**不含 `loader`**。cordis 的 context 是代理，
读取未注入的服务名会**抛异常**，而且 `?.` 拦不住（异常发生在 `get` trap 里，不是 `undefined`）。

真实 cordis 4.0.4 实测（`_evidence/probe/verify-loader-seam.mjs`，5/5 PASS）：

```
[PASS] a1.ctx.loader throws without inject: cannot get property "loader" without inject
[PASS] a2.ctx?.loader?.import ALSO throws without inject: cannot get property "loader" without inject
[PASS] b.ctx.get("loader") returns the service without inject: object
[PASS] c.ctx.loader resolves when inject includes "loader": function
```

把现有 `index.js` 真装进 cordis 后，实际报错（`node _evidence/verify/verify.mjs`）：

```
Error: cannot get property "loader" without inject
    at index.js:98:29                                  <- const loaderImport = ctx?.loader?.import;
    at resolveMcpClient (index.js:91)                  <- 同步属性读取，位于任何 try 之外
    at agent/created 监听器体 (index.js:273 起)
    at Proxy.serial (.../@deepseek-ai/cordis/lib/index.js:291:25)
```

（这三个行号取自 259 行那一版；结构特征不变：`:42` 的 `inject` 不含 `'loader'`，
`:98` 直接读 `ctx.loader`。）

**为什么严重**：`:98` 在监听器自己的 `try {` **之外**（那个 try 只包住
`index.js:234` 的 `agent.ctx.plugin(...)`），所以 `await` 该监听器会把异常抛出 `ctx.serial`；
而 `agent/created` 是 `dsh-agent/lib/index.js:579` 的 `this.ctx.serial(...)`，`announce()`
的注释写明"a listener failure rejects"。也就是说**每个会话创建都会失败**——不只是邮箱挂不上。

**修法（二选一，都是一行）**：

```js
// 方案 A（推荐）：把 loader 声明为依赖。'loader' 由 cordis-plugin-loader 在构造时
// ctx.reflect.provide("loader", this, ...)（cordis-plugin-loader/lib/index.js:603），
// 早于任何会话存在，声明它是安全的。
export const inject = ['agents', 'loader'];
```

```js
// 方案 B：用不抛异常的非注入读取，并对返回值做守卫
const loader = ctx.get?.('loader');
const loaderImport = loader?.import;
if (typeof loaderImport === 'function') { ... await loaderImport.call(loader, specifier) ... }
```

另外建议把 `resolveMcpClient(ctx)` 的整个调用也放进 try（或包一层 Promise 拒绝处理），
让任何解析期异常都在 `agent/created` 监听器内部被吃掉，而不是冒泡到 Agent 创建。

### 缺陷 D2 —— `apply` 不是 `async`，却用了 `await`：模块级语法错误 [已验证，**作者已修**]

（记录在案，因为它揭示了一个流程缺口，且同类错误可能再犯。）

我当时抓到的是 278 行版本，`node --check index.js`（Node 24.18.0）：

```
E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin\index.js:285
        await mountFor(agent);
        ^^^^^

SyntaxError: Unexpected reserved word
```

`apply` 的声明在 `index.js:181` 当时是 `export function apply(ctx, config) {`（无 `async`），
而返回已有会话的"回填"段里用了 `await mountFor(agent);`（当时的 `:285`）；同一版里
`await mountFor(agent)`（当时的 `:274`）因为在 `async` 箭头里所以合法。

**为什么严重**：ESM 里 `await` 出现在非 async 函数内是**解析期错误**，整个模块无法装载；
Loader 报的是 import 失败，插件行根本不存在——D1 甚至轮不到发生。

**现状**：299 行版本已是 `export async function apply(ctx, config) {`（`index.js:181`），
`node --check index.js` → **exit 0，通过**。

**顺带一个流程缺口（未修）**：`preflight.py` 的 `P4.entry_is_esm` 只做**文本匹配**
（找 ESM 导出字样），不做语法检查，所以上面这条语法错误它照样 17/17 PASS。
建议在 `preflight.py` 里加一项硬门禁：

```python
subprocess.run(["node", "--check", entry_path], check=True)
```

0 成本，能拦住"装上去必然报 import 失败"这类问题。

### 观察 O1 —— 解析策略在本次取证期间被改了三版 [已验证]

同一份 `index.js` 在约 15 分钟内出现过：

1. 静态 `import * as mcpClient from '@deepseek-ai/dsh-mcp-client'`（14:28）
2. `createRequire($DSH_PROFILE_DIR/package.json)`（14:36）
3. `ctx.loader.import(specifier)`（14:39，当前）

三者对**运行期**是否可行的判断不同。这是当前最大的不确定性来源，建议先定一个，再谈验证。

---

## 2. 已独立验证为**正确**的部分（在真实 cordis 4.0.4 上）

这些不依赖 `index.js` 的解析策略，是骨架级结论：

| # | 结论 | 证据 |
|---|---|---|
| V0 | 插件被真实 cordis 装载后 fiber `state=2`（active）；缺 `agents` 会停在 0 | `verify-existing.mjs` V0 |
| V1 | 每个 `agent/created` 恰好产生一次 `mcp-client` 挂载 | V1，`mounts=2` |
| V2 | `MAILBOX_SESSION_ID` / `MAILBOX_DISPLAY_NAME` 恰等于该 agent 的 `id` | V2 |
| V2 | `MAILBOX_WORKSPACE` 来自 `agent.session.header.cwd`；无 `cwd` 时**不注入** | V2 |
| V3 | `MAILBOX_CAPABILITY_LEVEL` 恒为 `'0'`（不谎报 wake） | V3 |
| V3 | `transport: 'stdio'`、`serverName: 'mailbox'` | V3 |
| V4 | 两个会话拿到**两个不同的 cordis 子 fiber**，且 `scopeOf` 键不同 | V4（`uids` 不同、`ctx[kScope]` 不同）→ `dsh-mcp-client:773` 的 `activeServerNames` WeakMap 不会跨会话撞名 |
| V4 | 子 ctx 能解析到 `tools` 服务（`mcp-client` 的 `inject`） | V5 |
| V5 | 同一存活会话重复 `agent/created` 不重复挂载（幂等） | V6，`mounts` 仍为 2 |
| V6 | **处置 Agent 会级联处置挂载的 mcp-client 子 fiber**（"会话关闭即回收子进程"） | V7：`uid 5 → null` |
| V7 | 单个会话挂载抛错被隔离，后续会话照常挂载 | V8 |
| V8 | 现有插件构造的 config **通过真实 `mcp-client` Config schema 校验**（`issues: null`），`env` 原样保留，`reconnect` 补默认值 | `verify-config-schema.mjs` PASS |

### 骨架级 API 取证（真实 cordis 4.0.4，`probe-cordis.mjs` 12/12 PASS）

| 断言 | 结论 |
|---|---|
| `ctx.plugin(p, cfg)` 返回什么 | 一个**仅带 `then` 的自有属性**的 thenable；`Context.is(x.ctx) === true`；`x.ctx.fiber.uid` 可用；`await x` 等 fiber 装载完（`state=2`） |
| `x.ctx.fiber.ctx === x.ctx` | true（所以 `mcpCtx.fiber.await()` 成立；`mcpCtx` 其实是 wrapped fiber，不是纯 ctx） |
| 根级 `ctx.on('agent/created')` 能否收到带 scope carrier 的派发 | 能（`probe-cordis.mjs` "root-level listener admits..."） |
| `ctx.plugin(nsObject, cfg)` 接受 `import * as ns` 命名空间对象 | 能（真实命名空间对象 `Context.is` / `resolve` 均正常） |
| 两次挂载得到两个不同实例 | `mounts[0].ctx !== mounts[1].ctx`，fibers 也不同 |
| 处置 agent fiber 会处置子 fiber | 子 `uid` 归 `null` |
| 会话级隔离的自检替身形状（`{ ctx: { fiber } }`）是否与真实一致 | **一致**，`_selftest` 的模拟没有骗人 |

---

## 3. `preflight.py` 与 `_selftest` 复核 [已验证]

```
cd E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin
& "E:\zcz\modle\MCP\mcp-agent-mailbox\.venv\Scripts\python.exe" -X utf8 preflight.py
→ 装载前预检：17 PASS / 0 FAIL（共 17）

node --import ./_selftest/register.mjs _selftest/selftest.mjs
→ 离线自检：20 PASS / 0 FAIL（共 20）
```

`plugin.patch.yml` 用 `E:\python\python.exe`（PyYAML 6.0.3）解析并核对：

```
top-level type: list
insert rows: [('mailbox-per-session', './index.js', [args, command, cwd, failOnStartupError,
              hostInstanceId, mailboxHome, serverName, toolCallTimeoutMs])]
command exists: True     cwd exists: True     mailboxHome exists: True     entry exists: True
```

`install_bundle` 路径下 `name: './index.js'` 会被正确锚定 —— 证据：
`dsh-app-boot/lib/index.js:3537-3545` 的 `anchorInsertedPluginNames()` 把 `./`、`../`、绝对路径
的 `name` 转成 `pathToFileURL(resolve(dirname(patchFile), name))`，而 `:2009`
（`dsh-plugin-manager`）对 bundle 的 patch 也是走 `loadOverlayPatches` 这条链。
（`README.md:77-79` 把这一步归因到 `dsh-app-boot/lib/index.js:3540`，指向同一段，可接受。）

---

## 4. 我**没能**验证的部分（必须在真实 DSH 上确认）

1. **`ctx.loader.import('@deepseek-ai/dsh-mcp-client')` 到底能不能穿透 asar。**
   `cordis-plugin-loader/lib/index.js:214-227` 的 `EntryTree.import()` 对裸包名走的是
   `import(name)`（相对加载器自身位置）；loader 自身在 asar 内，所以**理论上可行**，
   但 asar 让 `fileURLToPath` 抛错（`dsh-app-boot/lib/index.js:1399-1404`），
   app-boot 的解析路由在那种情况下直接放行给 Node 默认解析。**我没有执行过真实 DSH 启动**，
   所以这条只有代码证据，没有运行证据。
2. 真实 loader 实例上 `loader.import` 的签名/返回形状（我的 stub 是 `async import(specifier)`）。
   作者注释引用 `dsh-app-boot/lib/index.js:3692-3701` 的 `HostResolvedRootInclude`——我**没有**
   逐行读那段来确认它可以被插件复用（时间/上下文所限）。
3. `MAILBOX_HOME='C:\Users\<user>\.board-mcp'` 这个静态值对所有会话是否合适（多会话共用一个
   SQLite 是否是有意为之）。**这是产品决策，不是代码缺陷。**
4. 慢启动 MCP 服务器下，串行 `agent/created` 是否真的能在首轮对话前把工具同步好。
5. 真实 `agent/disposed` 载荷（现有代码同时兼容 `event.agent.id` 与 `event.id`，稳妥）。

---

## 5. 复现 D1 / D2 的最小命令

```powershell
cd E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin
node --check index.js                                # D2 已被作者修复：exit 0
node _evidence/verify/run.mjs verify-loader-seam.mjs # D1：证明 ctx.loader 未注入会抛（5/5 PASS）
node _evidence/verify/verify.mjs                     # D1：证明现有 index.js 因此必崩（2 个脚本 FAIL）
```

`verify.mjs` / `run.mjs` 都会先装 stub、结束不残留（`preflight.py` 的 P6 仍然 PASS）。

**一句话交接**：把 `export const inject = ['agents'];`（`index.js:42`）改成
`export const inject = ['agents', 'loader'];`（或把 `index.js:98` 改成
`ctx.get?.('loader')` 并守卫返回值），D1 即消失；随后必须**再跑一次** `verify.mjs`
确认端到端 12/12 + 3 个脚本全绿。

---

## 6. 快照

```
index.js  取证起点   278 行, SHA256 CC15AC52...D9F7
index.js  299 行中间版, SHA256 7BB5D8BD96DB2847ACC7150745E649DD9BFD3E0FB3669C3C254D072E0429E0BD
          （此版 D2 已修，D1 仍在）
index.js  收尾 259 行, SHA256 7F69293F0A6FD5F6E9AD8FD0C4BE30251704C26A50FE80BD729F85FD73BF50EE
          （node --check exit 0；D1 仍在：inject 仍为 ['agents']，:98 仍为 ctx?.loader?.import）
```

作者会话在该时间点之后仍持续修改 `index.js` / `README.md`；D1 / D2 的判定请以
"`inject` 是否含 `'loader'`"、"`apply` 是否 `async`"、"`ctx.loader` 是否出现"
这三个结构特征为准。
