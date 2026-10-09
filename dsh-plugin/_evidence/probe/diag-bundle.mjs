/** Diagnostic (verification only): inspect the bundle's logger output. */
import { Context } from '@deepseek-ai/cordis'
import { getMounts, resetMounts } from '@deepseek-ai/dsh-mcp-client'
import { apply as mailboxApply } from '../../index.js'

const kScope = Symbol('dsh.scope')
const root = new Context()
await root.plugin({
  name: 'host-services',
  apply(ctx) {
    ctx.reflect.provide('tools', { name: 'tools' })
    ctx.reflect.provide('agents', { name: 'agents' })
  },
})

const bundleCtx = (await root.plugin(
  { name: 'mailbox-per-session', apply: mailboxApply, inject: ['agents'] },
  { command: 'x', serverName: 'mailbox' },
)).ctx

console.log('bundle fiber name =', bundleCtx.fiber.name, 'state=', bundleCtx.fiber.state)
console.log('bundleCtx.is(root)?', bundleCtx === root, Context.is(bundleCtx))

resetMounts()
const key = Symbol('k')
const holder = root.plugin(function agentScope() {})
const agent = { id: 'session-DIAG', session: { header: { cwd: 'E:\\zcz' } }, ctx: holder.ctx.extend({ [kScope]: key }) }
const carrier = { [Context.filter]: (ctx) => ctx[kScope] === undefined || ctx[kScope] === key }

await root.serial(carrier, 'agent/created', { agent, source: 'startup' })

const logger = root.logger
console.log('\nlogger buffer (last 10):')
for (const message of logger.buffer.slice(-10)) console.log('  ', JSON.stringify(message))

console.log('\nmounts =', getMounts().length)

// direct call: do what the listener does, by hand, with the bundle's own ctx
console.log('\nmanual replay of the listener body against bundleCtx:')
try {
  const cfg = { serverName: 'mailbox', transport: 'stdio', command: 'x', args: [], env: { MAILBOX_SESSION_ID: agent.id } }
  const mounted = agent.ctx.plugin({ name: 'stub-mcp-client', apply: (c, c2) => { console.log('  child apply fired') }, inject: ['tools'] }, cfg)
  await mounted
  console.log('  mounted ok, child fiber uid =', mounted.ctx.fiber.uid)
} catch (error) {
  console.log('  threw:', error?.message, '\n', error?.stack?.split('\n').slice(0, 4).join('\n'))
}
console.log('mounts now =', getMounts().length)
process.exit(0)
