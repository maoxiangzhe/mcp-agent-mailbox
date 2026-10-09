/**
 * Offline probe (NOT a deliverable): drives the REAL @deepseek-ai/cordis 4.0.4
 * runtime extracted from the DSH asar, to verify the assumptions the mailbox
 * bundle's index.js relies on.
 *
 * Run:  cd _evidence/probe && node ../probe-cordis.mjs
 */
import { Context } from '@deepseek-ai/cordis'
import { Config as McpConfig } from './mcp-config-fixture.mjs'

// --- loader seam probe: what shape does a host-loader import hand back?
const fakeLoader = { name: 'loader', import: async (name) => import('./mcp-config-fixture.mjs') }
const loaderShape = await fakeLoader.import('@deepseek-ai/dsh-mcp-client')
console.log('loader.import returns namespace object:', Object.getPrototypeOf(loaderShape) === null)
console.log('loader.import namespace .name is undefined:', loaderShape.name === undefined)

// --- validate the exact config object our bundle builds, against the REAL schema
const sampleConfig = {
  transport: 'stdio',
  serverName: 'mailbox',
  command: 'C:/x/python.exe',
  args: ['-m', 'mcp_agent_mailbox.cli', 'serve'],
  cwd: 'E:/zcz',
  env: { MAILBOX_SESSION_ID: 'session-A' },
  failOnStartupError: false,
  reconnect: undefined,
}
const validated = McpConfig['~standard'].validate(sampleConfig)
console.log('mcp Config validate issues:', JSON.stringify(validated.issues ?? null))
console.log('mcp Config normalized reconnect:', JSON.stringify(validated.value?.reconnect))
console.log('mcp Config preserves env:', JSON.stringify(validated.value?.env))

const kScope = Symbol('dsh.scope') // stands in for @deepseek-ai/dsh-scope's kScope
const carrierKeys = new WeakMap()

function scopeTarget(base, key) {
  const carrier = {
    [Context.filter](ctx) {
      const tag = ctx[kScope]
      if (tag === undefined) return true
      return tag === key
    },
  }
  carrierKeys.set(carrier, key)
  return carrier
}

const results = []
const check = (label, ok, extra = '') => {
  results.push({ label, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? ' :: ' + extra : ''}`)
}

const root = new Context()

// what exactly does ctx.plugin() hand back?
const shapeProbe = root.plugin(function shapePlugin() {})
console.log('shape probe own keys:', Reflect.ownKeys(shapeProbe).map(String).join(','))
console.log('shape  Context.is(shape.ctx) =', Context.is(shapeProbe.ctx))
console.log('shape  shape.ctx.fiber.uid   =', shapeProbe.ctx.fiber.uid)
console.log('shape  ctx.fiber.ctx === ctx  =', shapeProbe.ctx.fiber.ctx === shapeProbe.ctx)
await shapeProbe
console.log('shape  after await fiber.state =', shapeProbe.ctx.fiber.state)

// --- a "mcp-client"-shaped namespace plugin, exactly the export shape of the real one
const mounts = []
const mcpStub = {
  name: 'mcp-client',
  inject: ['tools'],
  Config: McpConfig,
  async apply(ctx, config) {
    mounts.push({ ctx, config })
    // the real plugin uses ctx.effect + ctx.on; exercise both
    ctx.effect(() => () => {}, 'mcp-client.connection')
    ctx.on('internal/plugin', () => {}, { global: true })
    await Promise.resolve()
  },
}

// the real plugin's 'tools' dependency: a service on the root
const toolsPlugin = {
  name: 'tools',
  apply(ctx) {
    ctx.reflect.provide('tools', { name: 'tools-service' })
  },
}
const toolsFiber = root.plugin(toolsPlugin)
await toolsFiber
console.log('tools provided?', root.get('tools') !== undefined)

// --- mailbox bundle under test (logic copied from index.js, import replaced)
const seen = new Map()
const active = []

async function mountMailbox(ctxRoot, config, agent) {
  const key = agent.id
  const live = seen.get(key)
  if (live !== undefined && live.uid !== null) return 'skipped-duplicate'
  if (ctxRoot.get('tools') === undefined) {
    return 'skipped-no-tools'
  }
  const mounted = ctxRoot.plugin(mcpStub, {
    transport: 'stdio',
    serverName: config.serverName,
    command: config.command,
    args: config.args,
    env: {
      MAILBOX_SESSION_ID: agent.id,
      MAILBOX_HOST_TYPE: 'dsh',
    },
  })
  seen.set(key, mounted.ctx.fiber)
  await mounted
  return 'mounted'
}

// --- harness: two agents, one shared root, agent/created dispatched with a carrier
const rootCtx = root
const listener = async ({ agent }) => {
  const outcome = await mountMailbox(agent.ctx, { serverName: 'mailbox', command: 'py', args: ['-m', 'x'] }, agent)
  console.log(`  listener for ${agent.id} -> ${outcome}`)
  return undefined
}
rootCtx.on('agent/created', listener)

function makeAgent(id) {
  const scope = rootCtx.plugin(function scopePlugin() {})
  const agentCtx = scope.ctx.extend({ [kScope]: Symbol(id) })
  return { id, ctx: agentCtx, fiber: scope.ctx.fiber, carrier: scopeTarget({}, agentCtx[kScope]) }
}

const a = makeAgent('session-A')
const b = makeAgent('session-B')

// serial dispatch, exactly like AgentRegistry.announce -> ctx.serial(carrier, 'agent/created', {...})
for (const agent of [a, b]) {
  await rootCtx.serial(agent.carrier, 'agent/created', { agent, source: 'startup' })
}

check('one mcp-client mount per agent/created', mounts.length === 2, `mounts=${mounts.length}`)
check(
  'MAILBOX_SESSION_ID equals the agent id (A)',
  mounts[0]?.config.env.MAILBOX_SESSION_ID === 'session-A',
  String(mounts[0]?.config.env.MAILBOX_SESSION_ID),
)
check(
  'MAILBOX_SESSION_ID equals the agent id (B)',
  mounts[1]?.config.env.MAILBOX_SESSION_ID === 'session-B',
  String(mounts[1]?.config.env.MAILBOX_SESSION_ID),
)
check('two distinct plugin instances', mounts[0].ctx !== mounts[1].ctx)
check('two distinct fibers', mounts[0].ctx.fiber !== mounts[1].ctx.fiber)
check(
  'distinct registration scopes per agent',
  mounts[0].ctx[kScope] !== mounts[1].ctx[kScope],
  `${String(mounts[0].ctx[kScope])} vs ${String(mounts[1].ctx[kScope])}`,
)
check(
  'tools resolves from the agent-scoped plugin ctx',
  mounts[0].ctx.get('tools') !== undefined,
)
check('child plugin mounted under the agent scope fiber', mounts[0].ctx.fiber.parent !== undefined, `parent kind=${mounts[0].ctx.fiber.parent?.constructor?.name}`)

// duplicate event for the same live agent must not mount twice
await rootCtx.serial(a.carrier, 'agent/created', { agent: a, source: 'startup' })
check('duplicate agent/created does not double-mount', mounts.length === 2, `mounts=${mounts.length}`)

// failure isolation: a third agent whose plugin throws must not break the dispatch
let threw = false
mcpStub.apply = async () => {
  throw new Error('simulated startup failure')
}
const c = makeAgent('session-C')
try {
  await rootCtx.serial(c.carrier, 'agent/created', { agent: c, source: 'startup' })
} catch (error) {
  threw = true
  console.log('  raw serial dispatch rejected with:', error.message)
}
check('an unmounting agent rejects the serial dispatch unless the listener catches', threw)

// disposal: disposing the agent scope must dispose the mounted child fiber
const beforeUid = mounts[0].ctx.fiber.uid
await a.fiber.dispose()
await new Promise((resolve) => setTimeout(resolve, 10))
check('agent disposal disposes its mounted child fiber', mounts[0].ctx.fiber.uid === null, `uid ${beforeUid} -> ${mounts[0].ctx.fiber.uid}`)

// root-level listener admits the untagged root ctx and the scope-tagged agent ctx
let rootFired = 0
rootCtx.on('probe/plain', () => { rootFired += 1 })
rootCtx.emit('probe/plain')
check('root listener receives a plain root dispatch', rootFired === 1)

console.log('\nsummary:', results.filter((r) => r.ok).length + '/' + results.length, 'checks passed')
process.exit(results.every((r) => r.ok) ? 0 : 1)
