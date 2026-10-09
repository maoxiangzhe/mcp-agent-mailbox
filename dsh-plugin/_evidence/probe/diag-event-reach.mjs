/** Diagnostic (verification only): does a root-level `ctx.on('agent/created')`
 *  listener see a serial dispatch carrying an agent scope carrier? */
import { Context } from '@deepseek-ai/cordis'

const kScope = Symbol('dsh.scope')
const root = new Context()

let rootLevelHits = 0
let globalHits = 0
let untaggedHits = 0

root.on('agent/created', () => { rootLevelHits += 1 })
root.on('agent/created', () => { globalHits += 1 }, { global: true })
root.on('agent/created', () => { untaggedHits += 1 })

console.log('listeners registered')

const carrier = {
  [Context.filter](ctx) {
    const tag = ctx[kScope]
    return tag === undefined ? true : tag === 'KEY'
  },
}

const agent = { id: 'session-X' }
await root.serial(carrier, 'agent/created', { agent, source: 'startup' })

console.log('carrier dispatch ->', { rootLevelHits, globalHits, untaggedHits })

// and a plain emit with no carrier
root.emit('agent/created', { agent, source: 'startup' })
console.log('plain emit      ->', { rootLevelHits, globalHits, untaggedHits })

// a listener added from inside a child fiber's ctx (what the bundle does)
const child = root.plugin(function childPlugin(ctx) {
  ctx.on('agent/created', () => { globalThis.__childHit = (globalThis.__childHit ?? 0) + 1 })
})
await child
globalThis.__childHit = 0
await root.serial(carrier, 'agent/created', { agent, source: 'startup' })
console.log('child-fiber listener hit ->', globalThis.__childHit)
console.log('rootLevelHits now ->', rootLevelHits)

const ok = rootLevelHits === 3 && globalHits === 3
console.log(ok ? 'PASS: root-level listener receives scope-carried dispatches' : 'FAIL')
process.exit(ok ? 0 : 1)
