/**
 * DSH 每会话邮箱插件：**一个会话一个邮箱账号**。
 *
 * 这个文件为什么必须存在（而不是靠 profile 里配一个 `env:`）：
 *
 *   `@deepseek-ai/dsh-mcp-client` 的配置里**拿不到当前会话 ID**。`!!js` 表达式的求值
 *   时机是"该插件 fiber 激活时"——对 profile 根行是 profile 组合期一次，对 agent preset
 *   子行是 preset 声明激活期一次，两者都在任何 Agent 存在之前；而且 preset 的插件树是
 *   **按 preset 修订版共享**的（dsh-agent-preset-registry `lib/index.js:61` "Runtime plugin
 *   trees shared by Agents selecting one preset revision"、`:143` "the mount is not under
 *   the agent's fiber"），写死一个会话 ID 会让所有会话共用同一个账号。
 *
 *   DSH 自己给出的答案是：**在新建 Agent 时把会话 ID 写进子进程环境**。官方先例是终端
 *   插件 `dsh-api-terminal-controller/lib/index.js:1007` 的 `env: { DSH_SESSION_ID: agent.id }`。
 *   本插件照同一个姿势做，只不过挂的是邮箱 MCP 客户端。
 *
 * 身份为什么可信：`dsh-agent/lib/index.js:512` 在登记 Agent 时断言
 *     `if (id !== agent.session.id) throw ...`
 * 即 **`agent.id` 就是会话 ID**，且由宿主给出、模型无法指定。所以邮箱侧据此注册的账号
 * 天然是"一个会话一个账号"，也不需要开放 `--allow-adapter-registration`（模型连提交
 * 身份的机会都没有）。
 *
 * 生命周期：`dsh-mcp-client` 挂在 **Agent 作用域**（`agent.ctx`），其连接随 Agent 一起
 * dispose。所以关掉一个会话 → 只回收它自己的 MCP 子进程 → 只有它那个账号离线。
 *
 * 装载（官方机制，二选一）：
 *   1) `plugin_manager` 的 `install_bundle`，target = 本目录绝对路径（推荐）；
 *   2) 在 profile 的 patch 里加一行 `cordis:include`，指向本目录的 `plugin.patch.yml`。
 *      注意：`cordis:include` 的 `config.path` 是相对 **profile 目录**解析的，必须写
 *      绝对路径；而 include 进来的 patch 里以 `./` 开头的 `name` 才会锚定在 patch 文件
 *      旁（`dsh-app-boot/lib/index.js:3540`）。
 */
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

/**
 * 模块顶层落点：**只要这个文件被 Node 装载**，这一行就会出现。
 *
 * 用来区分两种失败（之前在真机上分不出来）：
 *   - 没有这条 -> Loader 根本没装载本文件（include / patch / 行 某一环没通）
 *   - 有这条、但没有 plugin.loaded -> 装载了但 `apply` 没跑或抛错
 * 写入失败绝不影响插件行为。
 */
try {
  appendFileSync(
    fileURLToPath(new URL('./.runtime-state.json', import.meta.url)),
    JSON.stringify({
      at: new Date().toISOString(),
      event: 'module.evaluated',
      pid: process.pid,
    }) + '\n',
    'utf8',
  );
} catch {
  /* 落点失败不影响插件行为 */
}

export const name = 'mailbox-per-session';

/**
 * 需要 Agent 注册表：会话创建时会发 `agent/created`，我们要在那一刻挂载。
 * 需要 loader：解析 `@deepseek-ai/dsh-mcp-client` 必须借它的 import（见下方说明）。
 *
 * ⚠️ `loader` **必须**在这里声明：cordis 的 context 是代理，读一个未注入的服务名会
 * **抛异常**（`cannot get property "loader" without inject`），`ctx?.loader?.` 这种可选链
 * 拦不住它（异常发生在 get trap 内，而不是返回 undefined）。已用真实 cordis 4.0.4 实测复现。
 */
export const inject = ['agents', 'loader'];

/**
 * 自证落点：把"插件已加载""某会话已挂载/失败"写成一条状态文件。
 *
 * 为什么需要它：插件跑在 DSH 进程里，出问题时唯一的现场就是它的日志，而日志不一定拿得到。
 * 落一个文件，就能在重启后**直接读**到"插件到底有没有被加载、每个会话的挂载结果是什么"，
 * 不必等日志、也不必猜。写入失败绝不影响插件行为。
 */
const STATE_PATH = new URL('./.runtime-state.json', import.meta.url);

async function writeState(patch) {
  try {
    appendFileSync(
      fileURLToPath(STATE_PATH),
      JSON.stringify({ at: new Date().toISOString(), ...patch }) + '\n',
      'utf8',
    );
  } catch {
    /* 落点失败不影响插件行为 */
  }
}

/**
 * `@deepseek-ai/dsh-mcp-client` 的**运行时**解析。
 *
 * 为什么不能写静态 `import`，也不能用 `createRequire`：
 *   该包**打在 DSH 的 asar 归档里**，磁盘上没有它的 `node_modules`（profile 的
 *   `node_modules` 只有 profile 自己装的 bundle）。DSH 之所以能加载它，是因为它给 root
 *   include 装了 `HostResolvedRootInclude`，把**裸包名**交给
 *   `internal.import(specifier, bareModuleBaseUrl)` 去解析
 *   （`dsh-app-boot/lib/index.js:3692-3701`）。普通 Node 解析（`createRequire` 从任何
 *   磁盘目录出发）都找不到 asar 内的包。
 *
 * 所以这里**借引擎自己的 loader 去 import**：`ctx.loader.import(spec)` 与"该插件是被谁
 * 加载的"走同一套解析。解析成功后按"命名空间插件"的形状归一化（`{apply, name}`）。
 */
let resolvedMcpClient = null;
let lastResolutionError = null;
let resolutionPromise = null;

function asPlugin(loaded) {
  if (!loaded) return null;
  if (typeof loaded.apply === 'function') return loaded;
  if (typeof loaded.default?.apply === 'function') return loaded.default;
  if (typeof loaded.default?.default?.apply === 'function') return loaded.default.default;
  return null;
}

/**
 * 解析候选顺序（每一级都记失败原因，最终一条错误信息里能看出卡在哪一层）：
 *
 *   1. `loader.import(spec)` —— 与"本插件被谁加载"同一套解析；真机上 DSH 的 root include
 *      是 `HostResolvedRootInclude`，裸包名会被交给 `internal.import(spec, bareModuleBaseUrl)`。
 *      但 `ctx.loader` 是 **Loader 实例**（`cordis-plugin-loader:603` provide 的是 `this`），
 *      它的 `import` 用的是 `this.ctx.baseUrl`（profile 目录），所以这一条**未必**能命中
 *      asar 内的包 —— 因此必须留后面的兜底，不能只靠它。
 *   2. `loader.import(url)` —— 直接把"asar 内的候选路径"当绝对 file URL 试。
 *      `bareModuleBaseUrl` 是启动器传给 `boot()` 的，插件拿不到；这里按
 *      `process.execPath / resources / app.asar / dsh / node_modules` 反推（Windows 打包形态）。
 *   3. `createRequire` 从候选目录解析 —— Electron 运行时下 asar 路径能像目录一样用。
 *   4. `process.getBuiltinModule` 不可用于第三方包，故不使用。
 */
function candidateBaseUrls() {
  const urls = [];
  const push = (value) => {
    if (typeof value === 'string' && value.trim() && !urls.includes(value)) urls.push(value);
  };
  // 打包运行时：<install>\resources\app.asar\dsh\node_modules\
  try {
    const res = process.resourcesPath;
    if (typeof res === 'string' && res.trim()) push(`${res.replace(/[\\/]+$/, '')}\\app.asar\\dsh\\node_modules\\`);
  } catch {
    /* 非 Electron 环境没有 resourcesPath */
  }
  // 从可执行文件位置反推（Electron: <install>\(DeepSeek Harness.exe)）
  try {
    const exec = process.execPath;
    if (typeof exec === 'string' && exec.includes('\\')) {
      const installRoot = exec.slice(0, exec.lastIndexOf('\\'));
      push(`${installRoot}\\resources\\app.asar\\dsh\\node_modules\\`);
    }
  } catch {
    /* ignore */
  }
  return urls;
}

function candidateRequireAnchors() {
  const anchors = [];
  const push = (value) => {
    if (typeof value === 'string' && value.trim() && !anchors.includes(value)) anchors.push(value);
  };
  push(process.env.DSH_PROFILE_DIR);
  for (const url of candidateBaseUrls()) {
    // 把 node_modules\ 去掉一层作为 require 锚点
    push(url.replace(/node_modules[\\/]?$/, ''));
  }
  return anchors;
}

async function resolveMcpClient(ctx) {
  if (resolvedMcpClient !== null) return resolvedMcpClient;
  if (resolutionPromise !== null) return resolutionPromise;

  const specifier = '@deepseek-ai/dsh-mcp-client';
  const attempts = [];
  resolutionPromise = (async () => {
    // 取 loader 用 `ctx.get(...)`：它对未注入的服务名返回 undefined 而**不抛**，
    // 而 `ctx.loader` 会因为代理的 get trap 抛异常（即便写了 `?.`）。两者都保留，
    // 且整段包在 try 里，任何异常都只变成一条可读的错误说明。
    let loader = null;
    try {
      loader = typeof ctx?.get === 'function' ? ctx.get('loader') : null;
    } catch (error) {
      attempts.push(`ctx.get('loader') 失败：${error?.message ?? error}`);
    }
    if (loader === null) {
      try {
        loader = ctx?.loader ?? null;
      } catch (error) {
        attempts.push(`ctx.loader 读取失败：${error?.message ?? error}`);
      }
    }
    const loaderImport = loader?.import;

    // 级别 1：loader.import(裸包名)
    if (typeof loaderImport === 'function') {
      try {
        const loaded = await loaderImport.call(loader, specifier);
        const plugin = asPlugin(loaded);
        if (plugin) {
          resolvedMcpClient = plugin;
          return plugin;
        }
        attempts.push(`loader.import 形状不可用：${Object.keys(loaded ?? {}).join(',')}`);
      } catch (error) {
        attempts.push(`loader.import(${specifier})：${error?.message ?? error}`);
      }

      // 级别 2：loader.import(绝对 file URL) —— 直指 asar 内的候选路径
      for (const base of candidateBaseUrls()) {
        const entry = `${base}@deepseek-ai\\dsh-mcp-client\\lib\\index.js`;
        const url = `file:///${entry.replace(/\\/g, '/').replace(/^\//, '')}`;
        try {
          const loaded = await loaderImport.call(loader, url);
          const plugin = asPlugin(loaded);
          if (plugin) {
            resolvedMcpClient = plugin;
            return plugin;
          }
          attempts.push(`loader.import(${url}) 形状不可用`);
        } catch (error) {
          attempts.push(`loader.import(${url})：${error?.message ?? error}`);
        }
      }
    } else {
      attempts.push('loader 服务不可用，无法 import');
    }

    // 级别 3：createRequire 从候选目录解析（Electron 下 asar 路径可当目录用）
    for (const anchor of candidateRequireAnchors()) {
      try {
        const require = createRequire(`${anchor.replace(/[\\/]+$/, '')}/package.json`);
        const loaded = require(specifier);
        const plugin = asPlugin(loaded);
        if (plugin) {
          resolvedMcpClient = plugin;
          return plugin;
        }
        attempts.push(`createRequire(${anchor}) 形状不可用`);
      } catch (error) {
        attempts.push(`createRequire(${anchor})：${error?.message ?? error}`);
      }
    }

    lastResolutionError = attempts.join(' | ');
    return null;
  })();

  try {
    return await resolutionPromise;
  } finally {
    resolutionPromise = null;
  }
}

/** 解析失败后允许重试（同一进程内首轮失败不应永久放弃）。 */
function forgetResolution() {
  resolvedMcpClient = null;
  lastResolutionError = null;
}

const DEFAULT_SERVER_NAME = 'mailbox';
const DEFAULT_ARGS = ['-m', 'mcp_agent_mailbox.cli', 'serve'];

/** 把配置规整成 mcp-client 的 config。 */
function buildClientConfig(config, agent) {
  const sessionId = String(agent?.id ?? '').trim();
  if (!sessionId) return null;

  const command = String(config?.command ?? '').trim();
  if (!command) return null;

  const args = Array.isArray(config?.args) && config.args.length > 0
    ? config.args.map((item) => String(item))
    : DEFAULT_ARGS.slice();

  const env = {
    PYTHONIOENCODING: 'utf-8',
    MAILBOX_HOST_TYPE: 'dsh',
    MAILBOX_HOST_INSTANCE_ID: String(config?.hostInstanceId ?? 'default'),
    // 关键一行：身份由宿主注入，取值就是本会话的原生会话 ID。
    MAILBOX_SESSION_ID: sessionId,
    MAILBOX_DISPLAY_NAME: String(config?.displayName ?? sessionId),
    // 能力等级必须诚实：DSH 目前是 tools-only，不得谎报可唤醒。
    MAILBOX_CAPABILITY_LEVEL: '0',
  };
  if (config?.mailboxHome) env.MAILBOX_HOME = String(config.mailboxHome);

  const workspace = agent?.session?.header?.cwd;
  if (typeof workspace === 'string' && workspace.trim()) {
    env.MAILBOX_WORKSPACE = workspace;
  }

  const clientConfig = {
    serverName: String(config?.serverName ?? DEFAULT_SERVER_NAME),
    transport: 'stdio',
    command,
    args,
    env,
    toolCallTimeoutMs: Number(config?.toolCallTimeoutMs ?? 60_000),
    failOnStartupError: Boolean(config?.failOnStartupError ?? false),
  };
  if (config?.cwd) clientConfig.cwd = String(config.cwd);
  return clientConfig;
}

function describe(error) {
  if (error === undefined || error === null) return 'unknown';
  if (typeof error === 'string') return error;
  return String(error?.message ?? error);
}

export async function apply(ctx, config) {
  const logger = ctx?.logger;
  const mounted = new Set();

  const log = (level, message) => {
    const sink = logger?.[level] ?? logger?.info;
    if (typeof sink === 'function') sink.call(logger, message);
  };

  /** 为一个 Agent 挂载邮箱 MCP（幂等；任何失败都只影响这一个会话）。 */
  const mountFor = async (agent) => {
    const sessionId = String(agent?.id ?? '').trim();
    if (!sessionId) {
      log('error', `${name}: 收到没有 id 的 agent，跳过`);
      writeState({ event: 'mount.skipped', reason: 'agent.id 为空' });
      return;
    }
    // 幂等：同一个会话重复触发不重复挂载（重复挂载会产生两个邮箱子进程、两个账号）。
    if (mounted.has(sessionId)) return;

    const clientConfig = buildClientConfig(config, agent);
    if (clientConfig === null) {
      log(
        'error',
        `${name}: 无法为会话 ${sessionId} 组装邮箱客户端配置` +
          '（必须配置 command，且 agent.id 必须非空）',
      );
      writeState({ event: 'mount.skipped', sessionId, reason: '配置不完整（缺 command）' });
      return;
    }

    const mcpClient = await resolveMcpClient(ctx);
    if (mcpClient === null) {
      log(
        'error',
        `${name}: 解析不到 @deepseek-ai/dsh-mcp-client（借引擎 loader 解析失败）` +
          `${lastResolutionError ? '；' + lastResolutionError : ''}`,
      );
      writeState({
        event: 'mount.failed',
        sessionId,
        reason: 'resolve-mcp-client',
        detail: lastResolutionError ?? '',
      });
      // 允许下一个会话再试一次：首次解析失败不应把整个进程永久判死。
      forgetResolution();
      return;
    }

    try {
      // 挂到 **Agent 作用域**：mcp-client 用 `scopeOf(ctx) ?? ctx.root` 作为命名空间键
      // （dsh-mcp-client/lib/index.js:812），WeakMap 按 scope 隔离，所以每个会话各自
      // 一份实例，serverName 可以同名而不冲突。
      const { ctx: mcpCtx } = agent.ctx.plugin(mcpClient, clientConfig);
      mounted.add(sessionId);
      log(
        'info',
        `${name}: 已为会话 ${sessionId} 挂载邮箱 MCP（serverName=${clientConfig.serverName}）`,
      );
      writeState({
        event: 'mount.ok',
        sessionId,
        serverName: clientConfig.serverName,
        injectedSessionId: clientConfig.env.MAILBOX_SESSION_ID,
        capabilityLevel: clientConfig.env.MAILBOX_CAPABILITY_LEVEL,
      });
      // agent/created 是串行派发（dsh-agent/lib/index.js:579 的 ctx.serial），
      // 等连接与工具同步完成再放行，保证本会话第一轮就有邮箱工具。
      if (mcpCtx?.fiber && typeof mcpCtx.fiber.await === 'function') {
        await mcpCtx.fiber.await();
      }
    } catch (error) {
      // 失败隔离：某个会话挂不上，不能让别的会话也挂不上，也不能让 Agent 创建整体失败。
      log('error', `${name}: 会话 ${sessionId} 挂载邮箱 MCP 失败：${describe(error)}`);
      writeState({
        event: 'mount.failed',
        sessionId,
        reason: 'plugin-mount',
        detail: describe(error),
      });
    }
  };

  // 插件被加载的证据（这一行一旦出现，就说明 include -> patch -> 插件这条链是通的）
  writeState({
    event: 'plugin.loaded',
    plugin: name,
    pid: process.pid,
    command: String(config?.command ?? ''),
    serverName: String(config?.serverName ?? DEFAULT_SERVER_NAME),
  });
  log('info', `${name}: 已加载（每个会话在 agent/created 时各自挂载邮箱 MCP）`);

  // 诊断：把"服务取到了没""列表里有几个会话"也落盘。
  // 否则"列表为空"和"根本没走到这一步"分不出来——这正是之前卡住的地方。
  let agentService = null;
  let existingAgents = null;
  try {
    agentService = typeof ctx?.get === 'function' ? ctx.get('agents') : null;
  } catch (error) {
    agentService = null;
    writeState({ event: 'diag.agents_service', ok: false, detail: String(error?.message ?? error) });
  }
  try {
    const listed = agentService?.list?.();
    if (Array.isArray(listed)) {
      existingAgents = listed;
      writeState({
        event: 'diag.agents_list',
        ok: true,
        pid: process.pid,
        count: listed.length,
        ids: listed.map((item) => String(item?.id ?? '?')).slice(0, 20).join(','),
      });
    } else {
      writeState({
        event: 'diag.agents_list',
        ok: false,
        detail: agentService === null || agentService === undefined
          ? 'agents 服务不可用'
          : 'agents.list() 不是数组',
      });
    }
  } catch (error) {
    writeState({ event: 'diag.agents_list', ok: false, detail: String(error?.message ?? error) });
  }

  ctx.on('agent/created', async ({ agent }) => {
    await mountFor(agent);
  });

  // 回填**已经存在**的会话：`agent/created` 只对之后新建的 Agent 触发，而插件可能是在
  // 会话已经跑起来之后才被加载（安装、改配置、修 bug 后的重载都属于这种）。不补这一步，
  // 那些会话永远不会有自己的邮箱账号。
  try {
    if (Array.isArray(existingAgents) && existingAgents.length > 0) {
      writeState({ event: 'backfill.begin', count: existingAgents.length });
      for (const agent of existingAgents) {
        await mountFor(agent);
      }
    } else {
      writeState({
        event: 'backfill.skipped',
        reason: Array.isArray(existingAgents) ? '当前没有活跃会话可回填' : '取不到会话列表',
      });
    }
  } catch (error) {
    log('error', `${name}: 回填已有会话失败（不影响后续新建会话）：${describe(error)}`);
    writeState({ event: 'backfill.failed', detail: describe(error) });
  }

  // 会话被销毁时清掉去重记录，允许同一 id 重新出现（例如会话被删除后重建）。
  ctx.on('agent/disposed', (event) => {
    const sessionId = String(event?.agent?.id ?? event?.id ?? '').trim();
    if (sessionId) mounted.delete(sessionId);
    writeState({ event: 'agent.disposed', sessionId: sessionId || '(未知)' });
  });
}
