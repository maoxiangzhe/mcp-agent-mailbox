/**
 * 离线自检：不需要真实 DSH 运行时就验证 `index.js` 的核心不变量。
 *
 * 做法：`index.js` 是**运行时**解析 `@deepseek-ai/dsh-mcp-client`（从 `DSH_PROFILE_DIR`
 * 或 `DSH_HOME` 解析）。自检把环境变量指向 `_selftest/anchor`，那里放了一个可观测替身，
 * 于是解析路径与真机**同构**（都走 createRequire 从 profile 目录解析），而不是把替身塞进
 * 插件自己的 node_modules。
 *
 * 断言：
 *   S1 每个会话各自挂载一次（两个会话 -> 两次 apply，两个不同 agent.ctx）
 *   S2 注入的 MAILBOX_SESSION_ID 恰好等于该会话的 agent.id（身份来源正确）
 *   S3 MAILBOX_CAPABILITY_LEVEL 恒为 0（不谎报 wake 能力——DSH 是 tools-only）
 *   S4 同一会话重复触发只挂载一次（幂等；否则会产生两个子进程、两个账号）
 *   S5 缺少 command 或 agent.id 时安全跳过，且**不影响别的会话**
 *   S6 某个会话挂载抛错时被隔离，不冒泡、不阻断其余会话
 *   S7 agent/disposed 后同一 id 可再次挂载
 *   S8 替身落盘的调用记录（独立于内存的计数证据）
 *
 * 运行：node _selftest/selftest.mjs
 */
import { readFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ANCHOR = fileURLToPath(new URL('./anchor', import.meta.url));
const LOG_PATH = fileURLToPath(new URL('./anchor/apply-log.jsonl', import.meta.url));

// 插件现在通过 `ctx.loader.import(...)` 解析 mcp-client（真机穿透 asar），
// 自检用 makeCtx() 提供的 loader 替身，所以不需要环境变量。
const { apply } = await import('../index.js');
const { resetLog } = await import('./stub-mcp-client.mjs');

const results = [];
function check(id, ok, note) {
  results.push([id, Boolean(ok), note]);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${id}: ${note}`);
}

check('S0.begin', true, `开始离线自检（替身经 loader.import 解析，锚点 = ${ANCHOR}）`);

/** 造一个假 ctx：只实现 index.js 真正用到的 on / logger / loader / agents。 */
function makeCtx({ existingAgents = [] } = {}) {
  const handlers = new Map();
  return {
    logger: {
      info: () => {},
      warn: () => {},
      error: () => {},
    },
    // 忠实模拟引擎：插件通过 `ctx.loader.import('@deepseek-ai/dsh-mcp-client')` 解析
    // 那个包（真机上穿透 asar；自检里返回可观测替身）。
    loader: {
      import(specifier) {
        if (specifier !== '@deepseek-ai/dsh-mcp-client') {
          return Promise.reject(new Error(`unexpected specifier: ${specifier}`));
        }
        return import('./anchor/node_modules/@deepseek-ai/dsh-mcp-client/index.mjs');
      },
    },
    // 模拟 AgentRegistry.list()：插件加载时用它回填**已存在**的会话。
    agents: {
      list: () => existingAgents.slice(),
    },
    // 插件通过 `ctx.get(name)` 取服务（这种写法对未注册的服务返回 undefined 而**不抛**，
    // 而 `ctx.loader` 会因为代理的 get trap 抛异常）。替身必须提供 get，否则回填拿不到列表。
    get(name) {
      if (name === 'loader') return this.loader;
      if (name === 'agents') return this.agents;
      return undefined;
    },
    on(event, handler) {
      if (!handlers.has(event)) handlers.set(event, []);
      handlers.get(event).push(handler);
    },
    emit(event, payload) {
      return Promise.all((handlers.get(event) ?? []).map((h) => h(payload)));
    },
  };
}

/**
 * 造一个假 agent：`agent.ctx.plugin` **忠实模拟 cordis 的行为**——真正调用
 * `plugin.apply(子ctx, config)`（cordis/lib/index.js:1618-1621 的 `plugin()` 会
 * `this.resolve(plugin)` 后执行 apply）。这样替身的落盘记录才是对真实调用路径的
 * 独立观测，而不是只观测"参数长什么样"。
 */
function makeAgent(id, { throwOnPlugin = false, cwd = undefined } = {}) {
  const mounts = [];
  const agent = {
    id,
    session: cwd === undefined ? { header: {} } : { header: { cwd } },
    ctx: {
      __label: `agent:${id}`,
      plugin(plugin, config) {
        if (throwOnPlugin) throw new Error('boom: plugin() failed');
        mounts.push({ plugin, config });
        const childCtx = { __label: `agent:${id}`, fiber: { await: async () => {} } };
        const applyFn = typeof plugin === 'function' ? plugin : plugin?.apply;
        if (typeof applyFn === 'function') applyFn(childCtx, config);
        return { ctx: childCtx };
      },
    },
  };
  return { agent, mounts };
}

const CONFIG = {
  command: 'C:\\fake\\python.exe',
  args: ['-m', 'mcp_agent_mailbox.cli', 'serve'],
  cwd: 'C:\\fake\\repo',
  serverName: 'mailbox',
  hostInstanceId: 'default',
  mailboxHome: 'C:\\Users\\fake\\.board-mcp',
};

async function main() {
  // 启动时清掉上次的落盘日志：`_evidence` 里的独立验证脚本会把记录写进同一个文件，
  // 不清空的话下面的"总数"断言会把别人的记录算进来（曾经因此假失败）。
  try {
    rmSync(LOG_PATH, { force: true });
  } catch {
    /* 文件不存在也无所谓 */
  }
  try {
    resetLog();
  } catch {
    /* stub 未导出 resetLog 时忽略；S8 会用文件内容判定 */
  }
  const ctx = makeCtx();
  apply(ctx, CONFIG);

  // ---- S1/S2/S3：两个会话各自挂载，身份与配置正确 ----
  const a = makeAgent('session-AAA', { cwd: 'E:\\work\\a' });
  const b = makeAgent('session-BBB');
  await ctx.emit('agent/created', { agent: a.agent, source: 'startup' });
  await ctx.emit('agent/created', { agent: b.agent, source: 'startup' });

  check('S1.two_mounts', a.mounts.length === 1 && b.mounts.length === 1,
    `A 挂载 ${a.mounts.length} 次、B 挂载 ${b.mounts.length} 次（期望各 1）`);
  check('S1.distinct_ctx', a.mounts[0]?.config !== b.mounts[0]?.config,
    '两个会话拿到各自的 config 对象');

  const envA = a.mounts[0]?.config?.env ?? {};
  const envB = b.mounts[0]?.config?.env ?? {};
  check('S2.session_id_is_agent_id',
    envA.MAILBOX_SESSION_ID === 'session-AAA' && envB.MAILBOX_SESSION_ID === 'session-BBB',
    `MAILBOX_SESSION_ID = ${envA.MAILBOX_SESSION_ID ?? ''} / ${envB.MAILBOX_SESSION_ID ?? ''}（期望分别等于各自 agent.id）`);
  check('S2.no_identity_bypass', envA.MAILBOX_SESSION_ID !== envB.MAILBOX_SESSION_ID,
    '两个会话的邮箱身份不同（一线程一账号的前提）');
  check('S3.capability_honest',
    envA.MAILBOX_CAPABILITY_LEVEL === '0' && envB.MAILBOX_CAPABILITY_LEVEL === '0',
    `MAILBOX_CAPABILITY_LEVEL = ${envA.MAILBOX_CAPABILITY_LEVEL} / ${envB.MAILBOX_CAPABILITY_LEVEL}（期望 0，不得谎报 wake）`);
  check('S3.workspace_from_session',
    envA.MAILBOX_WORKSPACE === 'E:\\work\\a' && envB.MAILBOX_WORKSPACE === undefined,
    `会话头有 cwd 才注入 MAILBOX_WORKSPACE：A=${envA.MAILBOX_WORKSPACE ?? '(无)'} B=${envB.MAILBOX_WORKSPACE ?? '(无)'}`);
  check('S3.stdio_transport',
    a.mounts[0]?.config?.transport === 'stdio' && a.mounts[0]?.config?.command === CONFIG.command,
    `transport=${a.mounts[0]?.config?.transport} command=${a.mounts[0]?.config?.command}`);

  // ---- S4：幂等 ----
  await ctx.emit('agent/created', { agent: a.agent, source: 'resume' });
  await ctx.emit('agent/created', { agent: a.agent, source: 'resume' });
  check('S4.idempotent', a.mounts.length === 1,
    `同一会话重复触发后 A 仍只挂载 ${a.mounts.length} 次（期望 1）`);

  // ---- S5：配置不全时安全跳过，且不误伤别人 ----
  // 5a. 同一个（配置正常的）ctx 上：空 id 的会话被跳过，不影响后续会话挂载
  const ctxMixed = makeCtx();
  apply(ctxMixed, CONFIG);
  let emptyIdPluginCalled = false;
  const emptyId = {
    id: '',
    session: { header: {} },
    ctx: { plugin() { emptyIdPluginCalled = true; throw new Error('不该被调用'); } },
  };
  await ctxMixed.emit('agent/created', { agent: emptyId, source: 'startup' });
  const afterEmpty = makeAgent('session-AFTER-EMPTY');
  await ctxMixed.emit('agent/created', { agent: afterEmpty.agent, source: 'startup' });
  check('S5.skip_when_agent_id_empty', emptyIdPluginCalled === false,
    `agent.id 为空时不调用 ctx.plugin（called=${emptyIdPluginCalled}）`);
  check('S5.no_collateral_damage', afterEmpty.mounts.length === 1,
    `空 id 会话之后，正常会话仍能挂载（${afterEmpty.mounts.length} 次，期望 1）`);

  // 5b. 独立的（缺 command 的）ctx：任何会话都不挂载
  const ctxNoCmd = makeCtx();
  apply(ctxNoCmd, { ...CONFIG, command: '' });
  const noCommand = makeAgent('session-NOCMD');
  await ctxNoCmd.emit('agent/created', { agent: noCommand.agent, source: 'startup' });
  check('S5.skip_when_config_incomplete',
    noCommand.mounts.length === 0,
    `缺 command 时不挂载（挂载 ${noCommand.mounts.length} 次，期望 0）`);

  // ---- S6：单个会话抛错被隔离 ----
  const ctxThrow = makeCtx();
  apply(ctxThrow, CONFIG);
  const bad = makeAgent('session-BAD', { throwOnPlugin: true });
  const good = makeAgent('session-GOOD');
  let bubbled = false;
  try {
    await ctxThrow.emit('agent/created', { agent: bad.agent, source: 'startup' });
  } catch {
    bubbled = true;
  }
  check('S6.error_isolated', bubbled === false,
    `挂载抛错没有冒泡出事件派发（bubbled=${bubbled}）`);
  await ctxThrow.emit('agent/created', { agent: good.agent, source: 'startup' });
  check('S6.next_agent_unaffected', good.mounts.length === 1,
    `抛错会话之后的会话仍能挂载（${good.mounts.length} 次，期望 1）`);

  // ---- S7：dispose 后可重新挂载 ----
  const ctxRe = makeCtx();
  apply(ctxRe, CONFIG);
  const re = makeAgent('session-RE');
  await ctxRe.emit('agent/created', { agent: re.agent, source: 'startup' });
  await ctxRe.emit('agent/disposed', { agent: re.agent });
  await ctxRe.emit('agent/created', { agent: re.agent, source: 'startup' });
  check('S7.remount_after_dispose', re.mounts.length === 2,
    `dispose 后重新创建可再次挂载（${re.mounts.length} 次，期望 2）`);

  // ---- S9：回填**已存在**的会话 ----
  // 场景：插件是在会话已经跑起来之后才被加载的（安装、改配置、修 bug 后重载都属于这种）。
  // `agent/created` 不会为这些会话再触发，所以插件必须在加载时回填，否则它们永远没有账号。
  const pre1 = makeAgent('session-PRE-1');
  const pre2 = makeAgent('session-PRE-2');
  const ctxBackfill = makeCtx({ existingAgents: [pre1.agent, pre2.agent] });
  await apply(ctxBackfill, CONFIG);
  check('S9.backfill_existing_mounted',
    pre1.mounts.length === 1 && pre2.mounts.length === 1,
    `加载时已存在的两个会话各自被挂载（PRE-1=${pre1.mounts.length} PRE-2=${pre2.mounts.length}，期望各 1）`);
  check('S9.backfill_identity_correct',
    pre1.mounts[0]?.config?.env?.MAILBOX_SESSION_ID === 'session-PRE-1'
    && pre2.mounts[0]?.config?.env?.MAILBOX_SESSION_ID === 'session-PRE-2',
    `回填时注入的是各自 agent.id：${pre1.mounts[0]?.config?.env?.MAILBOX_SESSION_ID} / ${pre2.mounts[0]?.config?.env?.MAILBOX_SESSION_ID}`);
  // 回填后再来一个新建会话，行为应与回填互不干扰
  const afterBackfill = makeAgent('session-AFTER-BACKFILL');
  await ctxBackfill.emit('agent/created', { agent: afterBackfill.agent, source: 'startup' });
  check('S9.new_agent_after_backfill', afterBackfill.mounts.length === 1,
    `回填之后的新会话仍能正常挂载（${afterBackfill.mounts.length} 次，期望 1）`);

  // ---- 汇总 ----
  // 用替身落盘的调用记录做**独立计数**：这条不依赖"selftest 与插件是否 import 到同一
  // 模块实例"，因此不会被 loader 解析差异骗过。
  let log = [];
  try {
    const raw = readFileSync(LOG_PATH, 'utf8');
    log = raw.split('\n').filter(Boolean).map((line) => JSON.parse(line));
  } catch (error) {
    check('S8.apply_log_readable', false, `读不到替身日志（${LOG_PATH}）：${error}`);
  }
  const byLabel = (label) => log.filter((entry) => entry.ctxLabel === label);
  check('S8.main_ctx_mounted_twice', byLabel('agent:session-AAA').length === 1
    && byLabel('agent:session-BBB').length === 1,
    `主 ctx 上 AAA/BBB 各 1 次：AAA=${byLabel('agent:session-AAA').length} BBB=${byLabel('agent:session-BBB').length}`);
  check('S8.log_session_ids_match', log.some((e) => e.sessionId === 'session-AAA')
    && log.some((e) => e.sessionId === 'session-BBB'),
    `日志里记录了真实会话身份：${log.map((e) => e.sessionId).filter(Boolean).join(', ')}`);
  check('S8.log_capability_all_zero', log.length > 0 && log.every((e) => e.capabilityLevel === '0'),
    `所有挂载的能力等级都是 0（${log.map((e) => e.capabilityLevel).join(',')}）`);
  check('S8.no_mount_for_empty_or_incomplete',
    byLabel('agent:').length === 0 && byLabel('agent:session-NOCMD').length === 0,
    `空 id 与缺 command 的会话均未挂载（空 id=${byLabel('agent:').length} NOCMD=${byLabel('agent:session-NOCMD').length}）`);
  check('S8.total_mounts_as_expected', log.length === 9,
    `替身实际被调用 ${log.length} 次（期望 9：AAA、BBB、AFTER-EMPTY、GOOD、RE×2、PRE-1、PRE-2、AFTER-BACKFILL）`);

  const passed = results.filter(([, ok]) => ok).length;
  const failed = results.length - passed;
  console.log('\n' + '='.repeat(70));
  console.log(`离线自检：${passed} PASS / ${failed} FAIL（共 ${results.length}）`);
  console.log(`替身实际挂载记录 = ${log.length} 条`);
  console.log('='.repeat(70));
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error('自检自身出错：', error);
  process.exit(2);
});
