/**
 * Offline probe fixture (NOT a deliverable): the exact `stdio` Config schema of
 * @deepseek-ai/dsh-mcp-client, transcribed verbatim from
 * dsh/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js lines 774-800
 * (the union's stdio branch) so the bundle's config can be validated offline.
 */
import z from '@deepseek-ai/schemastery'

const RECONNECT_DEFAULTS = Object.freeze({ enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 })
const MAX_TIMER_DELAY_MS = 2147483647
const DEFAULT_TOOL_CALL_TIMEOUT_MS = 60000
const DEFAULT_MAX_INSTRUCTION_BYTES = 32768

const Reconnect = z.object({
  enabled: z.boolean().default(RECONNECT_DEFAULTS.enabled),
  initialDelayMs: z.number().min(1).max(MAX_TIMER_DELAY_MS).default(RECONNECT_DEFAULTS.initialDelayMs),
  maxDelayMs: z.number().min(1).max(MAX_TIMER_DELAY_MS).default(RECONNECT_DEFAULTS.maxDelayMs),
  maxAttempts: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(RECONNECT_DEFAULTS.maxAttempts),
})

export const Config = z.union([
  z.object({
    transport: z.const('stdio'),
    serverName: z.string().required().pattern(/^[A-Za-z0-9_-]{1,32}$/),
    command: z.string().required(),
    args: z.array(String).default([]),
    env: z.dict(String).default({}),
    cwd: z.string().default(''),
    toolCallTimeoutMs: z.number().default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
    failOnStartupError: z.boolean().default(false),
    maxInstructionBytes: z.number().step(1).min(1).default(DEFAULT_MAX_INSTRUCTION_BYTES),
    reconnect: Reconnect,
  }),
  z.object({
    transport: z.const('streamable-http'),
    serverName: z.string().required().pattern(/^[A-Za-z0-9_-]{1,32}$/),
    url: z.string().required(),
    headers: z.dict(String).default({}),
    toolCallTimeoutMs: z.number().default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
    failOnStartupError: z.boolean().default(false),
    maxInstructionBytes: z.number().step(1).min(1).default(DEFAULT_MAX_INSTRUCTION_BYTES),
    reconnect: Reconnect,
  }),
])
