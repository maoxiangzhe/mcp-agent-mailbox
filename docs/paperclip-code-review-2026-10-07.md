# Paperclip 源码对照与邮箱改进建议

日期：2026-10-07。范围：源码审阅、隔离复现、现有测试；未修改业务实现，未启动 Paperclip，未连接真实宿主或发送真实消息。

## 源码位置与部署关系

Paperclip 官方 master 源码通过 GitHub codeload ZIP 下载，解压到 `E:\zcz\开源项目源码\paperclip-master`；归档为同目录下的 `paperclip-master.zip`。Git clone 未完成，这份源码没有 Git 历史，也不是固定提交的 checkout。后续实施应先记录固定提交或 release。

安装 Paperclip 并不要求拉源码：npm 包即可运行。这里下载源码是为了理解并借鉴机制。邮箱改进也不需要启动 Paperclip 或引入它的依赖。

## Paperclip 的内部工作链路

1. 任务分配、评论、定时事件产生唤醒请求；`agent_wakeup_requests` 保存来源、原因、幂等键、状态及关联 run。
2. `heartbeat.ts` 的 `enqueueWakeup` 识别相同任务范围的排队/执行回合，在适合的情况下合并上下文，保留独立唤醒回执；新消息有时需要单独排入后续回合，不能无条件并入正在运行的回合。
3. `startNextQueuedRunForAgent` 在启动锁内检查 Agent 可执行状态与并发剩余额度，再推进排队回合。其 `agent-start-lock.ts` 是进程内锁，不能当作跨进程数据库租约直接照搬。
4. `issues.ts` 的 checkout/owner 检查把任务归属与执行回合绑定，阻止其他回合随意领取或释放任务。
5. adapter 执行实际模型工具，`heartbeat_runs` 记录状态、前后 session ID、日志引用、usage、结果与恢复信息。新版本还包含 native runner 路径，不能把整个系统简化为一个 CLI 子进程。

关键文件（相对于 Paperclip 源码根）：

- `server/src/services/heartbeat.ts`：20149 行附近的排队启动；27106 行附近的 enqueueWakeup；29135 行附近的合并逻辑。
- `server/src/services/agent-start-lock.ts`：Agent 启动串行化。
- `server/src/services/issues.ts`：11673 行附近的任务 checkout。
- `packages/db/src/schema/agent_wakeup_requests.ts`、`heartbeat_runs.ts`：唤醒与执行分别持久化。
- `packages/adapters/codex-local/src/server/codex-args.ts`：构建 `codex exec` 参数与恢复 session。

这些行号对应本次下载的源码，在线 master 的行号可能不同。

## 邮箱现有基础

已有 SQLite WAL、唯一身份和连接代次、消息与投递原子入队、发送幂等、投递原子抢占、指数退避、超时重投、死信，以及 delivery / visibility / processing 三种独立状态。这些应保留。

邮箱的目标是现有会话之间互通，Paperclip 的目标是管理并执行 Agent 工作。尤其是 Paperclip 的 `codex exec/resume` 思路不能直接替换邮箱现有的 Desktop queue 通道：应维持原生目标会话和它已有的权限。

## 已验证问题，按实施优先级排序

### 1. 暂停和唤醒额度未落实到派发路径

位置：`config.py` 的 `global_pause`、`max_auto_wakes_per_hour`；`application/delivery_service.py` 的 `dispatch_due/_dispatch_one/_inject`。

全仓搜索发现这两个配置在装载、诊断和界面展示处使用，但派发路径没有实施检查。隔离模拟宿主验证：`global_pause=True`、`max_auto_wakes_per_hour=0`，发送三条不同消息，派发仍调用模拟 wake 三次。

建议：在即将产生宿主副作用之前执行统一调度门禁；额度通过 SQLite 事务预留，避免多 Broker 同时绕过。暂停/额度不足时消息保持排队并可主动读取，记录抑制原因；不算传输失败，不扣投递尝试次数。要明确额度统计的是实际唤醒请求，而不是消息数或读取次数。派发时还需核对对话阻塞状态，避免旧积压继续唤醒。

### 2. 离线队首积压导致在线会话饥饿

位置：`infrastructure/sqlite/repositories.py:907`、`application/delivery_service.py:173`。

查询先按全局 `delivery_seq` 取固定数量；离线条目返回 `offline` 后仍立即到期。因此最前面的离线条目反复占满批次。

复现：批次设为 2，前两条发给离线目标，第三条发给在线目标。连续三轮结果均为 `(scanned=2, skipped_offline=2, dispatched=0)`，第三条一直 queued。默认批次 100 时，对应前 100 条离线积压。

建议：按目标账号公平轮转，并在可派发候选选择时过滤/延后离线账号。保留账号内部顺序；重连时明确将其待投递条目重新设为到期。不要只在 Python 遍历末尾增加更多扫描而让成本随离线积压无限增长。

### 3. 抢占没有重新核对到期时间

位置：`infrastructure/sqlite/repositories.py:1006`。

`claim_for_dispatch` 的 UPDATE 仅检查 queued/failed，没有核对 `next_attempt_at`。隔离验证：投递失败并设置未来退避时间后，立即直接调用 claim，仍返回 True。

并发场景：两个 Broker 已读取同一候选；A 抢占后失败并设定退避，B 使用旧候选抢占时便能绕过退避。这不是单轮读取候选必然出现的问题，而是多派发者/过期候选条件下的问题。

建议：claim 的 WHERE 纳入和候选查询一致的到期条件。再使用 attempt/version token 保护外部调用后的结果写入，防止较早一次唤醒的迟到结果覆盖新的投递状态。

## 最值得借鉴的功能：独立、持久化的唤醒调度

现状：direct 通道对每条 delivery 调一次 wake，通知文本相同。同一账号连收多条消息会产生多次取信提示；单条宿主调用的等待也会延迟其他账号。

建议增加轻量 SQLite `wake_requests`，把消息存储、投递记录和“通知目标去取信”拆开：

```text
消息 + delivery 原子入库
          ↓
同一账号尚未派发的 wake 请求合并（保留覆盖消息范围）
          ↓
暂停 / 额度 / 可达性门禁
          ↓
原子领取 wake（owner、generation、attempt、lease）
          ↓
目标宿主持久化接收通知
          ↓
目标按消息 ID 取信，分别报告已读与处理结果
```

重要约束：

- 已排队但尚未发送的 wake 可以合并；已发送后又来的消息应记 dirty/后续请求，避免目标刚取完信而新消息永远没有提示。
- 只能确认该次通知实际覆盖的 delivery；不能把账号所有历史消息一概标 delivered。
- 给宿主传稳定 wake ID 并利用其去重能力；只有本地数据库幂等并不能消除“宿主已接收，但本地确认前崩溃”的重复通知窗口。宿主没有幂等契约时，应诚实保持至少一次语义。
- 初期先限制通知调度，不臆测 Desktop 中 Agent 回合已经结束；后续只有宿主提供可靠 run 状态时才实现执行并发限制。
- 使用有限并发、账号内部串行，避免一个宿主慢请求阻塞整个维护循环。

建议字段：`wake_id, account_id, generation, status, owner_id, attempt, lease_until, next_attempt_at, covered_delivery_seq, dirty, last_error`。最好有覆盖关联表或明确的覆盖区间，不能靠时间戳猜覆盖范围。

## 其他值得做的改进

1. **减少重复 Broker**：`mcp/server.py:71` 默认每个 stdio 服务都创建 Broker，分别有维护线程和进程内 EventBus。共享 SQLite 能保证部分数据原子性，但事件总线并不跨进程。先落实单个调度 owner 的数据库租约；再按实际需求增加本地 Broker + stdio 代理，降低每个会话重复维护的开销。
2. **处理 ownership**：`set_message_status` 当前记录状态，缺少处理回合 owner/租约。任务协作变复杂后，可增加 `claim_processing/renew/complete`，让重连后的旧处理者不能覆盖新处理者。仅靠 `running` 状态不足以证明谁仍在执行。
3. **少做无效轮询**：当前 Broker 每秒维护。可让新消息、重连、ACK 触发调度信号，并根据最近 next_attempt_at 调整等待；仍保留兜底扫描，跨进程写入不能仅靠进程内 Event 探知。
4. **可操作的监控**：优先展示 queued 原因、wake 状态、抑制原因、队列等待时间和宿主失败类别。死信重试/暂停解除等写操作应使用独立受控接口，现有只读 dashboard 的契约保留。

## 实施顺序与验收

第一批：调度门禁、公平候选、到期抢占、回归测试。第二批：wake 合并、持久化覆盖记录和崩溃恢复。第三批：唯一调度 owner、事件驱动维护、按需要增加任务处理 ownership。

必要验收：暂停/额度为零不调用宿主；大量离线积压不挡在线目标；旧候选不提前重试；同账号多消息合并通知后全部可取；合并后的新消息仍能触发后续通知；重启不丢 wake；迟到结果不覆盖新 attempt；慢宿主不拖住其他账号；保留三套消息状态和现有身份边界。

本次运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_conversations_delivery.py tests\test_repositories.py tests\test_codex_queue_wake.py --basetemp=.test-scratch/paperclip-review-20261007 --tb=short
```

结果：78 passed。最初默认 basetemp 的测试初始化遇到旧临时目录权限问题，改用独立目录后通过。上述三个复现均使用隔离 SQLite 和模拟/关闭自动唤醒的 Broker；未接触真实邮箱数据库或真实目标会话。第一段复现退出时日志句柄导致 Windows 临时目录清理失败，业务复现输出已完成；后续复现调用 logging.shutdown 后正常结束。
