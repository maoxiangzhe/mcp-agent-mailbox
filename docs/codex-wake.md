# Codex 唤醒层（等待真实宿主测试）

## 当前桌面接入修复

本机桌面 App Server 使用 stdio，没有 TCP 监听口，不能通过虚构端口接入。
本机安装的官方 CLI 提供 `codex queue --thread <ID> --message <TEXT>`。
2026-10-05 已实测：CLI 返回目标会话的入队确认后，探针自动进入同一桌面会话。
新增 `CodexQueueWaker`：自动查找 PATH 或 LOCALAPPDATA/OpenAI/Codex/bin 下的 CLI；
可用 MAILBOX_CODEX_CLI 显式指定。没有配置 WebSocket URL 时使用该入口。
只执行 queue，不使用 exec resume，不覆盖任何权限设置。
delivered 表示 Codex 已持久接收队列提示，任务完成仍需独立回执。
其他版本是否自动消费队列仍须对应宿主验证。

账号在线必须绑定真正承载会话的 App Server PID，不能绑定一次性测试脚本。
提供持续桥接工具：

```powershell
.venv/Scripts/python.exe -B -u -X utf8 tools/connect_codex_host.py --session-id <当前会话ID> --host-pid <实际AppServer PID> --account-name codex-A
```

该桥接按真实宿主 PID 判断在线，并校验进程创建时间；宿主退出或新连接接管后停止。
桥接持续运行 Broker，已有旧版 DSH MCP 不需要重启即可由它补投到 Codex。
进程日志放在忽略的 `.local-run`。Codex 重启后需要用新宿主 PID 重新接入。

若当前 Codex 会话尚未加载邮箱 MCP 工具，可用
`tools/codex_mailbox.py inbox` 取信、`reply --message-id <ID> --text <结论>` 回信、
`complete --message-id <ID>` 回执。它从当前 Shell 的 CODEX_SESSION_ID 解析既有账号，
不会重新注册或替换桥接连接。当前实现使用 default 宿主实例。

以下 WebSocket 路径适用于显式暴露监听口的 App Server。

依据官方文档：https://learn.chatgpt.com/docs/app-server

新增 `adapters/codex_wake.py`，连接已经运行的 App Server WebSocket。
首次执行 initialize / initialized，随后 thread/resume → turn/start。
只传目标 threadId 和取信提示，不覆盖模型、沙箱、审批或工作目录。
检查返回的 thread ID 与 turn ID/status；明确确认启动才允许 delivered。
连接持续保留，不在收到启动确认后马上断开。

## 接入

安装可选依赖：`uv sync --extra codex`。
在邮箱 MCP 进程环境中设置：

```text
MAILBOX_CODEX_APP_SERVER_URL=ws://127.0.0.1:<已有服务器端口>
MAILBOX_CODEX_APP_SERVER_TOKEN=<服务器要求的 bearer token，可选>
```

端口必须属于实际承载目标会话的 App Server。本实现不会启动服务器、
修改 Codex 配置、猜测桌面内部端口、使用 exec resume 启动另一份执行器。
官方 WebSocket 传输为实验性接口；桌面是否暴露可用监听口须实测。
未配置入口时 Codex 仍主动取信，不能宣称桌面唤醒已经完成。

WakeRouter 按宿主选择 DSH / Codex，投递服务和在线判断保持共用。
离线不调用唤醒；连接失败/依赖缺失/鉴权失败保持 queued；RPC 拒绝进入重试。
响应丢失属于接收结果不确定，重试可能重复提示，尚无宿主持久化幂等保证。
忙碌会话若拒绝 turn/start 则重试，不自动 steer 或中断当前任务。

审批和工具交互由原宿主客户端处理；邮箱不自动应答审批。
需确认原客户端能看到并处理该连接启动回合的审批事件；否则不可标记已验证。

## 待人工验收

1. 确认服务器与目标会话同属一个执行实例，目标已注册邮箱且进程在线。
2. 发一条测试信，观察目标原会话是否出现提示并开始回合。
3. 检查 delivered；取信、回复、处理回执独立验证。
4. 验证忙碌、鉴权失败、连接断开、离线和需要审批的行为。

本次只运行隔离协议测试，不向真实会话发信。旧 `adapters/codex.py`
CLI 路径保留兼容，但不作为新 Broker 唤醒层的依据。
