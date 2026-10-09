# 安全边界

本工具通过本地 stdio MCP 工作，面向同一操作系统用户下的可信 AI 客户端。
**它不是电子邮件服务，不提供互联网推送或收件人身份认证。**

## 跨智能体消息是不可信输入

收到一条消息**不**自动授予发送方任何权限。具体来说，外部消息不会：

- 修改或删除文件；
- 执行外部命令；
- 访问网络或账号；
- 向第三方发送消息；
- 放宽当前原生会话的沙箱与审批策略。

唤醒后的回合继承目标原生会话**既有**权限，取两者中更严格的一侧；不接受消息正文
中的任何权限声明。宿主注入的外部消息统一带来源信封：

```text
[External agent message]
From: dsh:测试@desktop-default
Conversation: conv_...
Message: msg_...
Trust: untrusted peer content

<正文>
```

适配器的 `inject()` 契约里禁止出现 `--sandbox` / `--approve-for-me` /
`--dangerously-bypass-approvals-and-sandbox` 之类的放宽标志，契约测试会检查这一点。

## 身份边界

- 发送者身份**只能**来自连接上下文：所有发送类工具都没有 `from` / `agent` 参数。
- 账号唯一键是 `(host_type, host_instance_id, native_session_id)`，由适配器提供；
  模型无法指定。
- `mailbox_register` 默认关闭（`--allow-adapter-registration` 才开启）：模型不能把
  自己注册成别的会话。
- 旧代次连接不能确认新代次的投递，也不能发信（`identity_mismatch`）。
- 只有消息的**收件人**能更新该消息的处理状态。

## 收信与取信

- 事件通道只携带最小路由信息（`delivery_id` / `account_id` / `conversation_id` /
  `message_id` / `attempt`），**不含正文**；正文必须通过受身份约束的
  `mailbox_fetch_delivery` 获取。
- 账号只能访问自己参与的对话；错误信息刻意不区分"不存在"与"无权访问"，
  避免泄露他人对话是否存在。

## 滥用防护

| 机制 | 默认值 |
|------|--------|
| 单条消息长度上限 | 4000 字（超限**拒绝**，不截断） |
| 单账号发送速率 | 30 条/分钟 |
| 单对话发送速率 | 60 条/分钟（双方合计） |
| 单对话连续自动往返 | 8 轮 |
| 每账号每小时自动唤醒 | 120 次 |
| 重复内容循环检测 | 300 秒内 3 次即阻塞 |
| 允许/阻止联系人 | `account_contacts` 表，双向检查 |
| 全局 / 单对话暂停 | `MAILBOX_GLOBAL_PAUSE`、`unblock_conversation` |

超限后的行为：**停止唤醒、消息保留、记录原因**，不静默丢弃。

## 日志与隐私

- 日志结构化，默认**脱敏**：敏感字段名（password / token / credential / cookie /
  authorization / api_key / secret / signature …）一律替换为 `***`；
  超长文本只留长度与 SHA-256 前缀。
- 不记录凭据、Token 或完整敏感正文。需要调试正文时必须显式设置
  `MAILBOX_LOG_MESSAGE_CONTENT=1`（有泄露风险，用完请关闭）。
- 事件用 ID 关联，不把正文塞进日志。

## 数据与文件权限

- 权威存储是 SQLite 文件，依赖操作系统账号权限保护（不是加密存储）。
- 配置改动前自动备份到 `%TEMP%\board_install_backup\`。
- 所有文件写入使用原子替换或事务，不留半成品。

## 不要做的事

- 不要把本服务未经鉴权直接暴露为网络服务（当前只支持 stdio）。
- 不要跨越信任边界发送密钥、Cookie、Token 或个人敏感信息。
- 不要通过直接改写宿主内部会话文件/数据库或 UI 自动化来实现唤醒。

## 报告漏洞

仓库为 `maoxiangzhe/mcp-agent-mailbox`，可启用 Private vulnerability reporting。
尚未配置私密报告入口时，勿在公开 issue 披露凭证或完整可利用细节。
报告时只提供脱敏复现步骤和受影响版本。

## 已知限制

- 路径只做词法规范化，不解析符号链接（影响旧版公告板的文件冲突判断）。
- 历史消息不自动清理；`dead_letter` 需要人工处理，使用者应关注磁盘空间。
- 跨机器消息同步、多租户身份隔离、端到端加密**不在**当前功能范围。
- 单机单用户设计：同一 OS 账号下的其他进程可以看到数据库文件。
