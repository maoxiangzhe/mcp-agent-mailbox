/**
 * Verification: the two halves of the `ctx.loader` defect, on REAL cordis 4.0.4.
 *   (a) `ctx.loader` (and even `ctx?.loader?.import`) THROWS without `inject`.
 *   (b) `ctx.get('loader')` does NOT throw and returns undefined when absent;
 *       with the service present it returns it.
 *   (c) injecting 'loader' makes `ctx.loader` resolve.
 */
import { Context } from '@deepseek-ai/cordis'

const results = []
const check = (id, ok, note) => {
  results.push([id, ok])
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${id}: ${note}`)
}

const root = new Context()
await root.plugin({
  name: 'loader-provider',
  apply(ctx) {
    ctx.reflect.provide('loader', {
      name: 'loader-stub',
      calls: [],
      async import(specifier) {
        this.calls.push(specifier)
        return { name: 'mcp-client', inject: ['tools'], apply() {} }
      },
    })
  },
})

// (a) no inject: plain access and optional-chained access both throw
const noInject = await root.plugin({
  name: 'no-inject',
  apply(ctx) {
    try {
      void ctx.loader
      globalThis.__a1 = 'no-throw'
    } catch (error) {
      globalThis.__a1 = error.message
    }
    try {
      void ctx?.loader?.import
      globalThis.__a2 = 'no-throw'
    } catch (error) {
      globalThis.__a2 = error.message
    }
  },
})
await noInject
check('a1.ctx.loader throws without inject', globalThis.__a1 !== 'no-throw', String(globalThis.__a1))
check('a2.ctx?.loader?.import ALSO throws without inject', globalThis.__a2 !== 'no-throw', String(globalThis.__a2))

// (b) ctx.get('loader') is the non-throwing read
await root.plugin({
  name: 'uses-get',
  apply(ctx) {
    globalThis.__b = typeof ctx.get('loader')
  },
})
check('b.ctx.get("loader") returns the service without inject', globalThis.__b === 'object', String(globalThis.__b))

// (c) declaring the injection makes ctx.loader work
await root.plugin({
  name: 'declares-loader',
  inject: ['loader'],
  apply(ctx) {
    globalThis.__c = typeof ctx.loader?.import
  },
})
check('c.ctx.loader resolves when inject includes "loader"', globalThis.__c === 'function', String(globalThis.__c))

// (d) what the bundle does today: ctx?.loader?.import() inside a try/catch
await root.plugin({
  name: 'bundle-shape',
  apply(ctx) {
    try {
      const fn = ctx?.loader?.import
      globalThis.__d = `escaped? fn=${typeof fn}`
    } catch (error) {
      globalThis.__d = `THREW OUTSIDE the guarded bit: ${error.message}`
    }
  },
})
check('d.try/catch around it still catches, but only because the throw is inside the try', globalThis.__d.startsWith('THREW'), String(globalThis.__d))

const passed = results.filter(([, ok]) => ok).length
console.log(`\n${passed}/${results.length} checks passed`)
process.exit(passed === results.length ? 0 : 1)
