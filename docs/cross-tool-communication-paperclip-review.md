# DSH / Codex 实测与 Paperclip 通信机制参考

本报告聚焦产品目标：不同智能体工具里的原生会话能相互发现、交流、接续工作和返回结果。可靠投递细节作为支撑，不作为改进主线。

## 已完成的真实实测

2026-10-07 23:54–23:59（北京时间），使用正在运行的 DSH-A 和本次 Codex 聊天进行了两轮交流，没有创建模拟 DSH 会话或启动额外模型执行器。

- Codex 原生会话：`01a116f8-<示例-Codex-会话>`，来自当前 Shell 的 `CODEX_SESSION_ID`。
- DSH 原生会话：`session-<示例-DSH-会话>`，从已有邮箱账号记录识别；账号名 DSH-A。
- Codex 账号：本次登记为 `Codex-Paperclip-review`，绑定现有 App Server PID 26504，未冒用旧聊天的 codex-A 账号。
- 同一对话：`conv_<示例对话>`。
- 唯一测试标识：`MAILBOX-EXCHANGE-20261007-A`。

| 步骤 | 真实结果 |
| --- | --- |
| Codex → DSH 首条消息 | DSH 本地 session/prompt 接口接收，投递 delivered；本条由测试脚本定向派发 |
| DSH 读取和回复 | DSH 从原会话调用邮箱工具，回复“DSH已收到并通过邮箱回复”，约 12.3 秒 |
| DSH → Codex 自动通知 | 回复后台自动 delivered，当前 Codex 聊天收到邮箱通知；准备手工补投时发现已经成功，没有手工补投 |
| Codex 再次回信 | 用当前身份的 `tools/codex_mailbox.py reply` 回到同一对话；没有手工派发，后台自动 delivered |
| DSH 第二次回复 | “第二轮确认：DSH收到Codex回信”，约 6.1 秒；Codex 再次自动收到邮箱通知 |
| 结束 | Codex 读取并标记两条回复已处理；明确结束确认循环 |

四条消息均在同一 conversation，三条后续消息具有连续的 reply_to 链。双向交流与同一对话接续已经验证。

边界：本次 Codex 工具列表没有邮箱 MCP 工具，因此读取/回复使用当前会话身份的 Shell 回退工具。DSH 侧实际调用了已加载的 MCP 工具。本次证明的是原生 DSH ↔ 原生 Codex 的通信与自动通知，没有证明当前 Codex 已原生加载邮箱 MCP，也没有测试宿主重启后的自动重新接入。

DSH 首次回复自述其本地进程没有 Codex 通道，回复当时 queued；实际检查发现后台在约 0.1 秒后已自动 delivered，且当前聊天收到了真实通知。因此不能把 Agent 自述的瞬时状态当作最终通道事实。现有后台已能完成反向派发，本次未启动额外常驻桥接进程。

## Paperclip 最值得参考的通信机制

本次参考源码：`E:\zcz\开源项目源码\paperclip-master`，官方 master ZIP；未启动 Paperclip。

### 1. 统一协议，宿主适配器负责落到具体会话

Paperclip 的 adapter 接收统一执行输入，再转换为具体宿主调用。邮箱也应让模型只面对统一的“找人、发消息、取信、回复”接口；DSH session/prompt、DSH 插件 followup、Codex queue 都是内部实现。

对邮箱的直接改进：统一接入握手返回原生 session、账号、工作区、实际可用收发通道、工具调用方法；发信方不需要知道对方是哪一种软件或如何唤醒。

Paperclip 的 Codex CLI adapter 可以执行/恢复它管理的 CLI session；邮箱需要保持现有 Desktop 原生聊天。借鉴统一输入与会话记录的思路，保留已实测的 Desktop queue 和 DSH 原生入口。

参考：`packages/adapters/codex-local/src/server/execute.ts`、`codex-args.ts`；`packages/db/src/schema/heartbeat_runs.ts` 的 sessionIdBefore / sessionIdAfter。

### 2. 带明确上下文的输入，而不只是“你有新消息”

`packages/adapter-utils/src/server-utils.ts` 的 `renderPaperclipWakePromptBody` 根据新会话/恢复会话、任务、评论、来源、续接信息生成输入；`issue-queued-comment-queue.ts` 保存准确的 comment ID 集合，以权威记录重建上下文。

邮箱目前给 direct 通道的提示是泛化的“mailbox_inbox 取信”。本次需要我再定位对话、读消息、确认 reply_to。这些可以由协议直接表达。

建议通知信封包含：`conversation_id, message_id, sender, reply_to, reason, read_action, reply_action`；正文按现有身份校验取回。首次交流可提供协作背景，后续只提供新增消息和必要的短摘要。MCP 没加载时，通知应带适用于当前宿主的回退命令，而不只要求调用不存在的工具。

这改善的是“模型收到以后是否知道该和谁、围绕什么继续交流”，不是单纯重试算法。

### 3. 稳定的对话与续接关系

Paperclip 的 `agent-conversations.ts` 将对话、会话代次、回合准备和回合收尾关联；重置后旧回合不能覆盖新对话状态。邮箱已经有 conversation / reply_to，可进一步补上本次协作的工作区、主题、相关文件/产物引用和续接摘要。

建议保持“原会话之间连续交流”的主体验：收到回复回到发起会话；不把每次发信做成另一份新聊天或执行器。会话重启/恢复时，显示明确的恢复/失联状态，并按宿主实际能力重新绑定。

参考：`server/src/services/agent-conversations.ts:118` 的 prepareConversationTurn；282 行的 settleConversationTurn。

### 4. 从普通聊天扩展到可跟踪协作

Paperclip 的对话可携带明确的 handoff 上下文、来源资料和完成结果，交接不只是发一个任务 ID。

邮箱可增加可选结构字段：

```json
{
  "kind": "request",
  "conversation_id": "conv_...",
  "correlation_id": "request_...",
  "text": "请检查这段实现并返回结论",
  "context": {
    "workspace": "E:\\zcz\\...",
    "summary": "已有背景与完成的工作",
    "references": ["文件、消息或产物引用"]
  },
  "expected_reply": "结论、依据和剩余问题"
}
```

对方可返回 `answer / progress / blocked / result`，结果自动关联原请求。本地文件引用应确认对方可访问；跨机器不能假设同一个绝对路径有效。结构化请求沿用目标会话既有权限，不把另一 Agent 的消息视为额外授权。

这应是可选层，现有纯文本发信继续可用。

## 建议改进主线与顺序

1. **统一接入**：宿主识别当前真实 session，完成握手并返回可执行的通信入口。本次暴露了“DSH 已有 MCP，而当前 Codex 没加载 MCP”的入口不一致问题。解决后，应无需临时 Python 或让用户反复复制会话 ID。
2. **找得到对方**：联系人除了账号名，增加会话标题、工作区、简短用途和实际可达方式。模型能选择“正在审查某项目的 DSH 会话”，而不只看到不透明 acc ID。
3. **一次调用开始交流**：统一 send/request 接口自动找到/复用对话，返回关联 ID 和后续回复的获取方式；Shell 回退接口也覆盖找人和首次发信，而不只支持 inbox/reply/complete。
4. **收到即可接续**：统一通知信封、精确取信和明确回复对象；当前宿主缺工具时提供现成回退路径。收到结果自动返回原会话并关联请求。
5. **协作带资料**：可选请求类型、背景、文件/产物引用与续接摘要。实际验收以“两种工具完成一个共同小任务并把结果带回”为准。
6. **沿用已验证的底层链路**：DSH 原生入口、Codex queue、SQLite、现有身份和对话模型。暂停、调度公平和幂等问题进入基础修复列表，不替代上述通信产品主线。

建议首先实现 1–4：用户说“让 DSH 看一下这个”，当前 Agent 能找到正确会话、发出带上下文的消息、自动收到回复，并在原对话继续讨论。随后再验收共同工作，而不是只检查数据库状态。

本次新增的是实测记录与通信改进方案，没有修改业务代码或宿主配置。
