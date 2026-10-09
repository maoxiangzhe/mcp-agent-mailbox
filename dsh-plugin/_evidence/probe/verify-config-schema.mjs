/**
 * Independent check (verification only): validate the config object the EXISTING
 * index.js builds against the REAL @deepseek-ai/dsh-mcp-client Config schema
 * (transcribed into mcp-config-fixture.mjs from lib/index.js:774-800).
 *
 * 更新（作者会话修 D1 之后）：插件不再用 `createRequire` 解析 mcp-client，改为
 * **借引擎 loader 的 import**（`ctx.get('loader')`），并且 `inject` 现在含 `'loader'`。
 * 所以这里必须提供一个 `loader` 服务，并把 `'loader'` 加进 inject；否则插件会
 * 明确报"loader 服务不可用"而不是 psilently 什么都不做。
 *
 * Run:  cd _evidence/probe && node verify-config-schema.mjs
 */
import { createRequire } from 'node:module'
import { Context } from '@deepseek-ai/cordis'
import { Config as McpConfig } from './mcp-config-fixture.mjs'
import { apply as mailboxApply } from '../../index.js'

// same anchor the verification harness materializes the stub into
const anchor = process.env.DSH_PROFILE_DIR
const stub = createRequire(`${anchor.replace(/[\\/]+$/, '')}/package.json`)('@deepseek-ai/dsh-mcp-client')
const { getMounts, resetMounts } = stub

const kScope = Symbol('dsh.scope')
const root = new Context()
root.plugin({
  name: 'host-services',
  apply(ctx) {
    ctx.reflect.provide('tools', { name: 'tools' })
    // 与真实 AgentRegistry 同形状：插件加载时会调 list() 回填已存在的会话。
    // 若只给 `{name}` 而没有 list，插件的诊断会报 `agents.list() 不是数组`（这是它的自证之一）。
    ctx.reflect.provide('agents', {
      name: 'agents-stub',
      list: () => [],
    })
    // 插件通过 loader.import 解析 mcp-client；这里给出与真机同形状的服务
    ctx.reflect.provide('loader', {
      name: 'loader-stub',
      import: async (specifier) => {
        if (specifier !== '@deepseek-ai/dsh-mcp-client') {
          throw new Error(`unexpected specifier: ${specifier}`)
        }
        return stub
      },
    })
  },
})
await root.plugin(
  { name: 'mailbox-per-session', apply: mailboxApply, inject: ['agents', 'loader'] },
  {
    command: 'E:\\zcz\\modle\\MCP\\mcp-agent-mailbox\\.venv\\Scripts\\python.exe',
    args: ['-m', 'mcp_agent_mailbox.cli', 'serve'],
    cwd: 'E:\\zcz\\modle\\MCP\\mcp-agent-mailbox',
    serverName: 'mailbox',
    hostInstanceId: 'default',
    mailboxHome: 'C:\\Users\\mxz\\.board-mcp',
    toolCallTimeoutMs: 60000,
    failOnStartupError: false,
  },
)

const makeAgent = (id, cwd) => {
  const holder = root.plugin(function agentScope() {})
  const key = Symbol(id)
  return {
    id,
    session: cwd === undefined ? { header: {} } : { header: { cwd } },
    ctx: holder.ctx.extend({ [kScope]: key }),
    carrier: { [Context.filter]: (ctx) => ctx[kScope] === undefined || ctx[kScope] === key },
  }
}

resetMounts()
const agent = makeAgent('session-77aa', 'E:\\zcz')
await root.serial(agent.carrier, 'agent/created', { agent, source: 'startup' })
const built = getMounts()[0]?.config
console.log('built config =', JSON.stringify(built, null, 2))

const result = McpConfig['~standard'].validate(built)
console.log('\nreal mcp-client Config issues:', JSON.stringify(result.issues ?? null))
console.log('normalized failOnStartupError:', result.value?.failOnStartupError)
console.log('normalized toolCallTimeoutMs:', result.value?.toolCallTimeoutMs)
console.log('normalized args:', JSON.stringify(result.value?.args))
console.log('normalized env:', JSON.stringify(result.value?.env))
console.log('normalized cwd:', JSON.stringify(result.value?.cwd))
console.log('reconnect defaults applied:', JSON.stringify(result.value?.reconnect))

const ok = result.issues === undefined && result.value?.transport === 'stdio'
console.log(`\n${ok ? 'PASS' : 'FAIL'}: built config satisfies the real mcp-client Config schema`)
process.exit(ok ? 0 : 1)
