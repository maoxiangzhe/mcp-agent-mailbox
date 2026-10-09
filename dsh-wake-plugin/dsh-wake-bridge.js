/**
 * DSH 专用适配器（通道B）：让本机邮箱能把消息**注入指定的 DSH 会话并启动一个回合**。
 *
 * 这是唯一不需要任何凭据的注入路径：DSH 的会话不是 HTTP 服务，"往某个会话里塞一条用户
 * 消息并让它开始干活"只能在 DSH 进程内做，靠的就是这里 ——
 *
 *     ctx.sessionController.resolveAgent(sessionId)   // 冷会话会隐式 resume
 *     agent.followup(createUserMessage({ content, source }))   // = 入 next-turn + wake
 *
 * 只做注入：不改会话权限、不放宽审批策略、不碰 session 日志文件；入口只监听 127.0.0.1，
 * 且必须配置令牌（与邮箱的 MAILBOX_DSH_WAKE_TOKEN 一致）。
 *
 * 装载（官方机制，二选一）：
 *   1) plugin_manager 的 install_bundle，target = 本目录绝对路径（推荐）；
 *   2) 在 $DSH_HOME/cordis.patch.yml 里加一行 cordis:include，指向本目录的
 *      dsh-wake-include.patch.yml（需要一次工作区外的写入批准）。
 * 细节见同目录 README.md。
 */

import { createServer } from 'node:http';
import { createUserMessage } from '@deepseek-ai/dsh-llm';

import {
  HEALTH_PATH,
  MIN_TOKEN_LENGTH,
  PLUGIN_NAME,
  PLUGIN_VERSION,
  WAKE_PATH,
  collectRequest,
  createWakeReceiver,
} from './dsh-wake-bridge-core.js';

export const name = PLUGIN_NAME;
/** 只用会话控制器：不注入就不碰别的东西。 */
export const inject = ['sessionController'];

export const DEFAULT_HOST = '127.0.0.1';
export const DEFAULT_PORT = 8799;

function readConfig(config) {
  const host = String(config?.host ?? DEFAULT_HOST);
  const port = Number(config?.port ?? DEFAULT_PORT);
  const token = String(config?.token ?? '').trim();
  return { host, port, token };
}

export function apply(ctx, config) {
  const { host, port, token } = readConfig(config);
  const logger = ctx?.logger;

  if (token.length < MIN_TOKEN_LENGTH) {
    // 没有令牌就不启动：宁可不注入，也不开一个"谁都能往任意会话塞消息"的本地口子。
    logger?.error?.(
      `${PLUGIN_NAME}: 未配置 token（至少 ${MIN_TOKEN_LENGTH} 个字符），拒绝启动注入入口。` +
        '请在 profile 的 patch 里为该行配置 token，并让邮箱使用同一个 MAILBOX_DSH_WAKE_TOKEN。',
    );
    return;
  }

  const receiver = createWakeReceiver({
    resolveAgent: (sessionId) => ctx.sessionController.resolveAgent(sessionId),
    buildUserMessage: (text) =>
      createUserMessage({
        content: [{ type: 'text', text }],
        source: { kind: 'plugin', plugin: PLUGIN_NAME },
      }),
    token,
    log: (level, message, detail) => {
      const sink = logger?.[level] ?? logger?.info;
      sink?.call?.(logger, `${message} ${JSON.stringify(detail ?? {})}`);
    },
  });

  const server = createServer((req, res) => {
    collectRequest(req)
      .then((request) => receiver.handle(request))
      .catch((error) => ({
        status: 400,
        body: { accepted: false, error: String(error?.message ?? error) },
        headers: { 'content-type': 'application/json; charset=utf-8' },
      }))
      .then((result) => {
        res.writeHead(result.status, result.headers);
        res.end(JSON.stringify({ ...result.body, plugin: PLUGIN_NAME, version: PLUGIN_VERSION }));
      })
      .catch(() => {
        try {
          res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
          res.end(JSON.stringify({ accepted: false, error: 'internal error' }));
        } catch {
          /* 连接已经断了就算了 */
        }
      });
  });

  let listening = false;
  const start = () => {
    if (listening) return;
    server.listen(port, host, () => {
      listening = true;
      logger?.info?.(
        `${PLUGIN_NAME}: 注入入口已监听 http://${host}:${port}${WAKE_PATH} ` +
          `（health ${HEALTH_PATH}）`,
      );
    });
    server.on('error', (error) => {
      logger?.error?.(`${PLUGIN_NAME}: 监听失败 ${String(error)}`);
    });
  };
  const stop = () => {
    if (!listening) return;
    listening = false;
    try {
      server.close();
    } catch {
      /* 已经关了 */
    }
  };

  if (typeof ctx?.effect === 'function') {
    ctx.effect(() => {
      start();
      return stop;
    });
    return;
  }
  start();
  if (typeof ctx?.on === 'function') ctx.on('dispose', stop);
}
