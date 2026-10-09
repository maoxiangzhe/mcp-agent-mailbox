/**
 * Verification-only runner: installs the stub package, runs one diagnostic
 * script from _evidence/probe, and removes the stub again.
 *
 * Usage: node _evidence/verify/run.mjs <script-name>
 */
import { spawnSync } from 'node:child_process'
import { cpSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginRoot = resolve(here, '..', '..')
const probeDir = join(pluginRoot, '_evidence', 'probe')
const anchorDir = join(here, 'anchor')
const script = process.argv[2]
if (script === undefined) {
  console.error('usage: node _evidence/verify/run.mjs <script-in-_evidence/probe>')
  process.exit(2)
}

cpSync(join(here, 'stub-package'), join(anchorDir, 'node_modules', '@deepseek-ai', 'dsh-mcp-client'), { recursive: true })
const result = spawnSync(process.execPath, [script], {
  cwd: probeDir,
  stdio: 'inherit',
  env: {
    ...process.env,
    DSH_PROFILE_DIR: anchorDir,
    DSH_MAILBOX_VERIFY_LOG: join(probeDir, 'apply-log.jsonl'),
  },
})
process.exit(result.status ?? 1)
