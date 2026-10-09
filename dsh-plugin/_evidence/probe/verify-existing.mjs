/**
 * Independent verification harness (additive; does not touch the existing bundle).
 *
 * Drives the EXISTING ../index.js against the REAL @deepseek-ai/cordis 4.0.4 runtime
 * (extracted from the DSH asar into _evidence/probe/node_modules) with a stub
 * `@deepseek-ai/dsh-mcp-client` installed temporarily by _evidence/verify/verify.mjs.
 *
 * The stub publishes its records on globalThis, so this script sees the exact cordis
 * contexts and fibers the bundle produced.
 *
 * Run:  node _evidence/verify/verify.mjs
 */
import { createRequire } from 'node:module'
import { Context } from '@deepseek-ai/cordis'
import { apply as mailboxApply } from '../../index.js'

// The bundle resolves the mcp-client through createRequire($DSH_PROFILE_DIR/package.json),
// so the verifier reaches the SAME module instance through the same anchor.
const anchor = process.env.DSH_PROFILE_DIR
const stub = createRequire(`${anchor.replace(/[\\/]+$/, '')}/package.json`)('@deepseek-ai/dsh-mcp-client')
const { getMounts, readMountLog, resetMounts } = stub

const kScope = Symbol('dsh.scope') // stands in for @deepseek-ai/dsh-scope's kScope

function carrierFor(key) {
  return {
    [Context.filter](ctx) {
      const tag = ctx[kScope]
      return tag === undefined ? true : tag === key
    },
  }
}

const results = []
const check = (id, ok, note) => {
  results.push([id, ok])
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${id}: ${note}`)
}

const CONFIG = {
  command: 'C:\\fake\\python.exe',
  args: ['-m', 'mcp_agent_mailbox.cli', 'serve'],
  cwd: 'C:\\fake\\repo',
  serverName: 'mailbox',
  hostInstanceId: 'default',
  mailboxHome: 'C:\\Users\\fake\\.board-mcp',
}

// --- a real cordis root with the services the bundle / mcp-client inject
const root = new Context()
await root.plugin({
  name: 'host-services',
  apply(ctx) {
    ctx.reflect.provide('tools', { name: 'tools-service' })
    // stand-in for @deepseek-ai/dsh-agent's AgentRegistry, published as ctx.agents
    // (dsh-agent/lib/index.js:311 "Agent service (`ctx.agents`)"; :332 super(ctx, "agents"))
    ctx.reflect.provide('agents', { name: 'agents-registry-stub', list: () => [], get: () => undefined })
    // stand-in for @deepseek-ai/cordis-plugin-loader's Loader, published as `loader`
    // (cordis-plugin-loader/lib/index.js:603 ctx.reflect.provide("loader", this, ...)).
    // Its import() is the seam the bundle uses to reach the asar-hosted mcp-client.
    // This stub resolves the package through the same anchor the bundle would use, so
    // the check exercises the whole path; whether the REAL loader reaches the asar is
    // what only a live DSH can settle.
    const anchorRequire = createRequire(`${anchor.replace(/[\\/]+$/, '')}/package.json`)
    ctx.reflect.provide('loader', {
      name: 'loader-stub',
      async import(specifier) {
        if (specifier !== '@deepseek-ai/dsh-mcp-client') throw new Error(`unexpected specifier ${specifier}`)
        return anchorRequire('@deepseek-ai/dsh-mcp-client')
      },
    })
  },
})

// --- activate the bundle EXACTLY as the loader would (same export shape)
const bundle = await root.plugin(
  { name: 'mailbox-per-session', apply: mailboxApply, inject: ['agents'] },
  CONFIG,
)
check('V0.bundle_activated', bundle.ctx.fiber.state === 2, `fiber state=${bundle.ctx.fiber.state} (2=active; 0 would mean a missing injected service)`)

// --- agents shaped like the real Agent: own scope key, own fiber, own session header
function makeAgent(id, cwd) {
  const holder = root.plugin(function agentScope() {})
  const key = Symbol(id)
  return {
    id,
    session: cwd === undefined ? { header: {} } : { header: { cwd } },
    ctx: holder.ctx.extend({ [kScope]: key }),
    fiber: holder.ctx.fiber,
    scopeKey: key,
    carrier: carrierFor(key),
  }
}

resetMounts()
const a = makeAgent('session-AAA', 'E:\\work\\a')
const b = makeAgent('session-BBB')

// serial dispatch with the real carrier, exactly like AgentRegistry.announce
// (dsh-agent/lib/index.js:579 this.ctx.serial(entry.carrier, 'agent/created', {...}))
await root.serial(a.carrier, 'agent/created', { agent: a, source: 'startup' })
await root.serial(b.carrier, 'agent/created', { agent: b, source: 'startup' })

const live = getMounts()
const log = readMountLog()
check('V1.one_mount_per_agent', log.length === 2, `mounts=${log.length} (expected 2)`)
check(
  'V2.session_id_is_agent_id',
  log[0]?.sessionId === 'session-AAA' && log[1]?.sessionId === 'session-BBB',
  `MAILBOX_SESSION_ID = ${log[0]?.sessionId} / ${log[1]?.sessionId}`,
)
check('V2.display_name_is_agent_id', log[0]?.displayName === 'session-AAA' && log[1]?.displayName === 'session-BBB')
check('V2.workspace_from_header_cwd', log[0]?.workspace === 'E:\\work\\a', `A workspace=${log[0]?.workspace}`)
check('V2.no_workspace_when_header_lacks_cwd', log[1]?.workspace === null, `B workspace=${log[1]?.workspace}`)
check('V3.capability_zero', log.every((e) => e.capabilityLevel === '0'), log.map((e) => e.capabilityLevel).join(','))
check('V3.host_type_dsh', log.every((e) => e.hostType === 'dsh'))
check('V3.stdio_transport', log.every((e) => e.transport === 'stdio'))
check('V3.server_name', log.every((e) => e.serverName === 'mailbox'))

// --- per-agent isolation, observed on the REAL cordis contexts/fibers the plugin made
check('V4.two_distinct_instances', live[0]?.ctx !== live[1]?.ctx)
check('V4.two_distinct_child_fibers', live[0]?.fiber !== live[1]?.fiber, `uids=${live[0]?.fiber?.uid} / ${live[1]?.fiber?.uid}`)
check(
  'V4.distinct_scope_per_agent',
  live[0]?.ctx[kScope] !== live[1]?.ctx[kScope],
  'per-agent scope keys differ, so mcp-client activeServerNames (WeakMap keyed on scopeOf) cannot cross agents',
)
check('V5.tools_resolves_from_child_ctx', live[0]?.ctx.get('tools') !== undefined)

// --- idempotence: a duplicate dispatch for the same live agent must not remount
await root.serial(a.carrier, 'agent/created', { agent: a, source: 'resume' })
check('V6.idempotent', readMountLog().length === 2, `mounts=${readMountLog().length} (expected 2)`)

// --- disposal reaps the mounted child fiber: the "会话关闭即回收子进程" claim
const childFiber = live[0].fiber
check('V7.child_fiber_live_before', childFiber.uid !== null, `uid=${childFiber.uid}`)
await a.fiber.dispose()
await new Promise((resolve) => setTimeout(resolve, 20))
check('V7.dispose_reaps_child_fiber', childFiber.uid === null, `uid after agent dispose=${childFiber.uid}`)

// --- error isolation: a throwing plugin must not reject the serial dispatch,
// and the next agent must still mount
const c = makeAgent('session-CC')
const throwingCtx = { plugin() { throw new Error('boom: plugin() failed') } }
let bubbled = false
try {
  await root.serial(c.carrier, 'agent/created', { agent: { id: 'session-CC', ctx: throwingCtx }, source: 'startup' })
} catch (error) {
  bubbled = true
  console.log('  serial dispatch rejected with:', error.message)
}
check('V8.error_isolated', bubbled === false, `bubbled=${bubbled}`)
const d = makeAgent('session-DD')
await root.serial(d.carrier, 'agent/created', { agent: d, source: 'startup' })
check('V8.next_agent_unaffected', readMountLog().length === 3, `mounts=${readMountLog().length} (expected 3)`)

const passed = results.filter(([, ok]) => ok).length
console.log(`\n${passed}/${results.length} checks passed`)
process.exit(passed === results.length ? 0 : 1)
