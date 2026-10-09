/**
 * `@deepseek-ai/dsh-mcp-client` 的可观测替身（仅离线自检用）。
 *
 * 真机契约（来自 asar 只读取证）：
 *   - 命名空间插件：导出对象上有 `apply`；cordis 用 `plugin.name` 作为运行时记录的键
 *     （cordis/lib/index.js:1625）。
 *   - 被 `ctx.plugin(...)` 挂载时，cordis 调用 `apply(ctx, config)`。
 *
 * 记录方式刻意用**文件**（`_selftest/apply-log.jsonl`）：内存数组会依赖
 * "selftest 与插件 import 到同一个模块实例"，一旦 loader hook 的解析路径与预期不同，
 * 计数就会假通过。落盘则是无歧义的证据。
 */
import { appendFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const LOG = fileURLToPath(new URL('./apply-log.jsonl', import.meta.url));

export function resetLog() {
  try {
    rmSync(LOG, { force: true });
  } catch {
    /* 文件不存在也无所谓 */
  }
}

export const name = 'dsh-mcp-client';

export function apply(ctx, config) {
  try {
    appendFileSync(
      LOG,
      JSON.stringify({
        at: new Date().toISOString(),
        ctxLabel: ctx?.__label ?? '(unknown)',
        serverName: config?.serverName,
        sessionId: config?.env?.MAILBOX_SESSION_ID,
        capabilityLevel: config?.env?.MAILBOX_CAPABILITY_LEVEL,
        transport: config?.transport,
      }) + '\n',
      'utf8',
    );
  } catch {
    /* 记录失败不影响被观测的行为 */
  }
}

export default { name, apply };
