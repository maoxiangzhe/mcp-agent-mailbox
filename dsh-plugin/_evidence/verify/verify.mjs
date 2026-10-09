/**
 * Independent verification of the EXISTING bundle (index.js / package.json /
 * plugin.patch.yml) against the REAL @deepseek-ai/cordis 4.0.4 runtime and the
 * REAL dsh-mcp-client Config schema, both extracted from app.asar.
 *
 * This does not modify the bundle. It keeps a dedicated resolution anchor at
 * `_evidence/verify/anchor/` that carries a stub `@deepseek-ai/dsh-mcp-client`
 * (with the REAL Config schema transcribed into it), and points the bundle's
 * `$DSH_PROFILE_DIR` at that anchor, which is exactly the anchor the bundle's
 * `resolveMcpClient()` uses via `createRequire`.
 *
 * Run:  node _evidence/verify/verify.mjs        (from the plugin root)
 */
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginRoot = resolve(here, '..', '..')
const probeDir = join(pluginRoot, '_evidence', 'probe')
const anchorDir = join(here, 'anchor')
const stubSrc = join(here, 'stub-package')
const stubDst = join(anchorDir, 'node_modules', '@deepseek-ai', 'dsh-mcp-client')

const steps = [
  ['probe-cordis.mjs', 'cordis 4.0.4 runtime + mcp-client Config schema + plugin() shape'],
  ['verify-existing.mjs', 'the bundle driven by real cordis through agent/created'],
  ['verify-config-schema.mjs', 'the config the bundle builds, against the real Config schema'],
]

if (!existsSync(join(probeDir, 'node_modules', '@deepseek-ai', 'cordis', 'lib', 'index.js'))) {
  console.error('missing _evidence/probe/node_modules/@deepseek-ai/cordis — run _evidence/asar_materialize.py first')
  process.exit(2)
}

let failed = 0
cpSync(stubSrc, stubDst, { recursive: true })
for (const [script, what] of steps) {
  console.log(`\n${'='.repeat(72)}\n### ${script} — ${what}\n${'='.repeat(72)}`)
  const result = spawnSync(process.execPath, [script], {
    cwd: probeDir,
    stdio: 'inherit',
    env: {
      ...process.env,
      // the bundle's createRequire anchor; its node_modules holds the stub
      DSH_PROFILE_DIR: anchorDir,
      DSH_MAILBOX_VERIFY_LOG: join(probeDir, 'apply-log.jsonl'),
    },
  })
  if (result.status !== 0) failed += 1
}

console.log(`\n${'='.repeat(72)}`)
console.log(failed === 0 ? '独立验证：全部通过' : `独立验证：${failed} 个脚本失败`)
console.log('='.repeat(72))
process.exit(failed === 0 ? 0 : 1)
