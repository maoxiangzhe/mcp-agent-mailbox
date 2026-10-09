/**
 * DSH 唤醒桥的**纯逻辑**部分（不 import 任何 DSH 包，因此可以用 node 直接测）。
 *
 * 契约（邮箱侧 mcp_agent_mailbox/adapters/dsh_plugin_wake.py 依赖它）：
 *
 *   GET  /healthz      header: x-dsh-wake-token  -> 200 {ok:true, plugin, version}
 *   POST /dsh-wake     header: x-dsh-wake-token  -> 200 {accepted:true, sessionId}
 *                      body: {sessionId, text}      400/401/409 {accepted:false, error}
 *
 * 语义要点：
 * * **只监听本机回环**，并且**必须有令牌**（短于 16 字符直接拒绝启动，宁可不注入也不开
 *   一个无鉴权的"往任意会话塞消息"的口子）；
 * * 只做一件事：resolveAgent(sessionId) -> agent.followup(用户消息)。不改权限、不放宽审批、
 *   不碰会话日志文件；
 * * 任何异常都转成结构化响应，绝不让异常冒到 HTTP 层变成 500 空响应。
 */

import { createHash, timingSafeEqual } from 'node:crypto';

export const WAKE_PATH = '/dsh-wake';
export const HEALTH_PATH = '/healthz';
export const PLUGIN_NAME = 'dsh-mailbox-wake-bridge';
export const PLUGIN_VERSION = '1.0.0';
/** 令牌最短长度：低于这个值不启动，避免"随手写个 1"当密码。 */
export const MIN_TOKEN_LENGTH = 16;
/** 请求体上限：本地回环也不接受无限大的 body。 */
export const MAX_BODY_BYTES = 64 * 1024;
/** 注入文本上限：一条"去取信"的提示，不该更长。 */
export const MAX_TEXT_CHARS = 4000;

function tokenMatches(actual, expected) {
  if (typeof actual !== 'string' || typeof expected !== 'string') return false;
  const a = Buffer.from(actual, 'utf8');
  const b = Buffer.from(expected, 'utf8');
  if (a.length !== b.length || a.length === 0) return false;
  return timingSafeEqual(a, b);
}

function json(status, payload) {
  return {
    status,
    body: payload,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  };
}

function fingerprint(token) {
  return createHash('sha256').update(String(token), 'utf8').digest('hex').slice(0, 8);
}

/**
 * 造一个"收唤醒请求"的函数。
 *
 * @param {object} deps
 * @param {(sessionId: string) => Promise<object>} deps.resolveAgent 返回 {agent} 或 {error}
 * @param {(text: string) => object} deps.buildUserMessage 造一条用户消息（DSH 侧是 createUserMessage）
 * @param {string} deps.token 与邮箱共享的令牌
 * @param {(level: string, message: string, detail?: object) => void} [deps.log]
 */
export function createWakeReceiver({ resolveAgent, buildUserMessage, token, log }) {
  const expected = String(token ?? '');
  const logger = typeof log === 'function' ? log : () => {};

  async function handle(request) {
    const method = String(request?.method ?? 'GET').toUpperCase();
    const path = String(request?.path ?? '/');
    const headers = request?.headers ?? {};
    const presented = headers['x-dsh-wake-token'];

    if (!tokenMatches(presented, expected)) {
      // 令牌不对：可能是别人扫到了这个端口，也可能是邮箱没配对。两者都只回 401。
      logger('warn', 'DSH 唤醒桥：令牌不匹配，已拒绝', {
        presented_fingerprint: fingerprint(presented),
        expected_fingerprint: fingerprint(expected),
      });
      return json(401, { accepted: false, error: 'invalid token' });
    }

    if (method === 'GET' && path === HEALTH_PATH) {
      return json(200, { ok: true, plugin: PLUGIN_NAME, version: PLUGIN_VERSION });
    }

    if (method !== 'POST' || path !== WAKE_PATH) {
      return json(404, { accepted: false, error: `no such endpoint: ${method} ${path}` });
    }

    const payload = request?.json;
    const sessionId = typeof payload?.sessionId === 'string' ? payload.sessionId.trim() : '';
    const text = typeof payload?.text === 'string' ? payload.text : '';
    if (!sessionId || !text.trim()) {
      return json(400, { accepted: false, error: 'sessionId and text are required' });
    }
    if (text.length > MAX_TEXT_CHARS) {
      return json(400, { accepted: false, error: `text too long (max ${MAX_TEXT_CHARS} chars)` });
    }

    let found;
    try {
      found = await resolveAgent(sessionId);
    } catch (error) {
      // 例如 session/writer-held：别把异常抛出去，如实回报。
      logger('error', 'DSH 唤醒桥：解析会话失败', { sessionId, error: String(error) });
      return json(409, { accepted: false, error: `resolveAgent failed: ${String(error)}` });
    }
    if (found === undefined || found === null) {
      return json(409, { accepted: false, error: 'resolveAgent returned nothing' });
    }
    if (found.error !== undefined && found.error !== null) {
      const reason = typeof found.error === 'string' ? found.error : JSON.stringify(found.error);
      logger('warn', 'DSH 唤醒桥：目标会话不可注入', { sessionId, error: reason });
      return json(409, { accepted: false, error: reason });
    }
    const agent = found.agent;
    const followup = agent?.followup;
    if (typeof followup !== 'function') {
      return json(409, { accepted: false, error: 'resolved agent has no followup()' });
    }

    try {
      await followup.call(agent, buildUserMessage(text));
    } catch (error) {
      logger('error', 'DSH 唤醒桥：注入失败', { sessionId, error: String(error) });
      return json(409, { accepted: false, error: `followup failed: ${String(error)}` });
    }

    logger('info', 'DSH 唤醒桥：已注入并启动回合', { sessionId });
    return json(200, { accepted: true, sessionId });
  }

  return { handle };
}

/**
 * 把 node:http 的请求读成 { method, path, headers, json }，交给上面的 handle。
 * 单独导出，便于测试里直接喂假请求。
 */
export function collectRequest(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      let parsed;
      if (raw.trim()) {
        try {
          parsed = JSON.parse(raw);
        } catch {
          parsed = undefined;
        }
      }
      const url = new URL(req.url ?? '/', 'http://127.0.0.1');
      resolve({
        method: req.method ?? 'GET',
        path: url.pathname,
        headers: req.headers ?? {},
        json: parsed,
      });
    });
    req.on('error', reject);
  });
}
