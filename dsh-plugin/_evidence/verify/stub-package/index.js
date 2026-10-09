/**
 * Verification-only stub for `@deepseek-ai/dsh-mcp-client`.
 *
 * verify.mjs copies this directory into the verification anchor's
 * `node_modules/@deepseek-ai/dsh-mcp-client`, which is the anchor the bundle
 * resolves through `createRequire($DSH_PROFILE_DIR/package.json)`.
 *
 * Export shape mirrors the real namespace plugin
 * (dsh-mcp-client/lib/index.js:761-763, :780-800, :809, :835).
 *
 * `Config` here is a minimal Standard-Schema validator with the same shape and the
 * same defaults as the real schema; the REAL schema is applied separately in
 * `_evidence/probe/verify-config-schema.mjs`, which transcribes the stdio branch of
 * dsh-mcp-client/lib/index.js:774-800 verbatim.
 *
 * Records are published on `globalThis` AND appended to an on-disk JSONL file, so the
 * evidence survives any module-identity question.
 */
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs'

const LOG = process.env.DSH_MAILBOX_VERIFY_LOG ?? 'apply-log.jsonl'
const SLOT = Symbol.for('dsh-mailbox-verify.mounts')

/** Same defaults as the real schema (lib/index.js:774-800). */
function validate(input) {
  const value = { ...(input ?? {}) }
  const issues = []
  if (value.transport !== 'stdio') issues.push({ message: 'transport must be "stdio"', path: ['transport'] })
  if (typeof value.serverName !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(value.serverName)) {
    issues.push({ message: 'serverName must match /^[A-Za-z0-9_-]{1,32}$/', path: ['serverName'] })
  }
  if (typeof value.command !== 'string' || value.command === '') {
    issues.push({ message: 'command is required', path: ['command'] })
  }
  value.args ??= []
  value.env ??= {}
  value.cwd ??= ''
  value.toolCallTimeoutMs ??= 60000
  value.failOnStartupError ??= false
  value.maxInstructionBytes ??= 32768
  value.reconnect ??= { enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 }
  if (issues.length > 0) return { issues }
  return { value }
}

export const Config = { '~standard': { version: 1, vendor: 'dsh-mailbox-verify-stub', validate } }

export function resetMounts() {
  globalThis[SLOT] = []
  writeFileSync(LOG, '')
}

/** Live records: { ctx, fiber, config } for every apply the bundle triggered. */
export function getMounts() {
  return globalThis[SLOT] ?? []
}

/** Disk-backed projection: plain data only, readable from any module instance. */
export function readMountLog() {
  try {
    return readFileSync(LOG, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line))
  } catch {
    return []
  }
}

export const name = 'mcp-client'
export const inject = ['tools']

export async function apply(ctx, config) {
  const records = (globalThis[SLOT] ??= [])
  records.push({ ctx, fiber: ctx?.fiber, config })
  appendFileSync(
    LOG,
    JSON.stringify({
      mountCount: records.length,
      serverName: config?.serverName,
      transport: config?.transport,
      sessionId: config?.env?.MAILBOX_SESSION_ID,
      displayName: config?.env?.MAILBOX_DISPLAY_NAME,
      capabilityLevel: config?.env?.MAILBOX_CAPABILITY_LEVEL,
      workspace: config?.env?.MAILBOX_WORKSPACE ?? null,
      hostType: config?.env?.MAILBOX_HOST_TYPE,
      command: config?.command,
    }) + '\n',
  )
}
