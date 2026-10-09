/** Diagnostic (verification only): replicate exactly what the bundle's listener does. */
import { Context } from '@deepseek-ai/cordis'
import * as mcpNs from '@deepseek-ai/dsh-mcp-client'

const kScope = Symbol('dsh.scope')
const root = new Context()
await root.plugin({
  name: 'host-services',
  apply(ctx) {
    ctx.reflect.provide('tools', { name: 'tools' })
    ctx.reflect.provide('agents', { name: 'agents' })
  },
})

const holder = root.plugin(function agentScope() {})
const agentCtx = holder.ctx.extend({ [kScope]: Symbol('k') })

// A: pass the same namespace object the diagnostic imported
try {
  const mounted = agentCtx.plugin(mcpNs, { serverName: 'mailbox', transport: 'stdio', command: 'x', args: [], env: {} })
  await mounted
  console.log('A: namespace object -> OK, child uid =', mounted.ctx.fiber.uid)
} catch (error) {
  console.log('A: namespace object -> THREW:', error.message)
}

// B: pass it through a plain variable (what the bundle does)
const plugin = mcpNs
console.log('B: plugin === ns:', plugin === mcpNs, 'typeof plugin.apply:', typeof plugin.apply)
try {
  const mounted = agentCtx.plugin(plugin, { serverName: 'mailbox', transport: 'stdio', command: 'x', args: [], env: {} })
  await mounted
  console.log('B: via variable -> OK, child uid =', mounted.ctx.fiber.uid)
} catch (error) {
  console.log('B: via variable -> THREW:', error.message)
}

// C: what the bundle actually receives when it require()s
import { createRequire } from 'node:module'
const require = createRequire('E:\\zcz\\dsh-mailbox-plugin\\package.json')
const loaded = require('@deepseek-ai/dsh-mcp-client')
const plugin2 = loaded?.default ?? loaded
console.log('C: require === ns:', loaded === mcpNs, '| apply is same fn:', plugin2.apply === mcpNs.apply)
try {
  const mounted = agentCtx.plugin(plugin2, { serverName: 'mailbox', transport: 'stdio', command: 'x', args: [], env: {} })
  await mounted
  console.log('C: require path -> OK, child uid =', mounted.ctx.fiber.uid)
} catch (error) {
  console.log('C: require path -> THREW:', error.message)
}
process.exit(0)
