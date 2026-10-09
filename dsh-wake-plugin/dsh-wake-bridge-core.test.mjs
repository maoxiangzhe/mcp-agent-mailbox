/**
 * 通道B插件的纯逻辑测试（用 node 直接跑，不需要 DSH）：
 *
 *     node dsh-wake-plugin/dsh-wake-bridge-core.test.mjs
 *
 * 用假的 resolveAgent 验：正常注入会调用 followup、令牌不对被拒、目标不可注入要如实报错、
 * 路径/方法不对要 404、文本超长要 400。
 */

import assert from 'node:assert/strict';

import {
  HEALTH_PATH,
  MAX_TEXT_CHARS,
  WAKE_PATH,
  createWakeReceiver,
} from './dsh-wake-bridge-core.js';

const TOKEN = 't'.repeat(24);
let passed = 0;

function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      passed += 1;
      console.log(`  [OK] ${name}`);
    })
    .catch((error) => {
      console.error(`  [FAIL] ${name}: ${error?.message ?? error}`);
      process.exitCode = 1;
    });
}

function makeReceiver({ found, onFollowup } = {}) {
  const calls = [];
  const receiver = createWakeReceiver({
    resolveAgent: async (sessionId) => {
      calls.push(sessionId);
      if (found !== undefined) return found;
      return {
        agent: {
          followup: async (message) => {
            onFollowup?.(message);
          },
        },
      };
    },
    buildUserMessage: (text) => ({ kind: 'user-message', content: [{ type: 'text', text }] }),
    token: TOKEN,
    log: () => {},
  });
  return { receiver, calls };
}

function request(overrides = {}) {
  return {
    method: 'POST',
    path: WAKE_PATH,
    headers: { 'x-dsh-wake-token': TOKEN },
    json: { sessionId: 'session-abc', text: '去 mailbox_inbox 取信' },
    ...overrides,
  };
}

await check('healthz 带正确令牌 -> 200 ok', async () => {
  const { receiver } = makeReceiver();
  const result = await receiver.handle(
    request({ method: 'GET', path: HEALTH_PATH, json: undefined }),
  );
  assert.equal(result.status, 200);
  assert.equal(result.body.ok, true);
});

await check('令牌不对 -> 401，且不解析会话', async () => {
  const { receiver, calls } = makeReceiver();
  const result = await receiver.handle(
    request({ headers: { 'x-dsh-wake-token': 'wrong-token-wrong-token' } }),
  );
  assert.equal(result.status, 401);
  assert.equal(calls.length, 0);
});

await check('正常唤醒 -> 200 accepted 且确实调了 followup', async () => {
  let message;
  const { receiver, calls } = makeReceiver({ onFollowup: (m) => { message = m; } });
  const result = await receiver.handle(request());
  assert.equal(result.status, 200);
  assert.equal(result.body.accepted, true);
  assert.deepEqual(calls, ['session-abc']);
  assert.equal(message.kind, 'user-message');
  assert.equal(message.content[0].text, '去 mailbox_inbox 取信');
});

await check('目标会话不可注入 -> 409 且原样带出原因', async () => {
  const { receiver } = makeReceiver({ found: { error: 'session/writer-held' } });
  const result = await receiver.handle(request());
  assert.equal(result.status, 409);
  assert.equal(result.body.accepted, false);
  assert.match(result.body.error, /writer-held/);
});

await check('resolveAgent 抛异常 -> 409，不是 500', async () => {
  const receiver = createWakeReceiver({
    resolveAgent: async () => {
      throw new Error('boom');
    },
    buildUserMessage: () => ({}),
    token: TOKEN,
  });
  const result = await receiver.handle(request());
  assert.equal(result.status, 409);
  assert.match(result.body.error, /boom/);
});

await check('agent 没有 followup -> 409', async () => {
  const { receiver } = makeReceiver({ found: { agent: {} } });
  const result = await receiver.handle(request());
  assert.equal(result.status, 409);
  assert.match(result.body.error, /followup/);
});

await check('路径/方法不对 -> 404', async () => {
  const { receiver } = makeReceiver();
  const result = await receiver.handle(request({ path: '/nope' }));
  assert.equal(result.status, 404);
});

await check('缺 sessionId/text -> 400', async () => {
  const { receiver } = makeReceiver();
  const result = await receiver.handle(request({ json: { sessionId: 'x' } }));
  assert.equal(result.status, 400);
});

await check('文本超长 -> 400', async () => {
  const { receiver } = makeReceiver();
  const result = await receiver.handle(
    request({ json: { sessionId: 'x', text: 'y'.repeat(MAX_TEXT_CHARS + 1) } }),
  );
  assert.equal(result.status, 400);
});

console.log(`\n通道B插件逻辑：${passed} 项通过${process.exitCode ? '（有失败）' : ''}`);
