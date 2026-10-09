# 数据库与迁移

## 权威存储

唯一权威状态是 SQLite 文件（默认 `~/.board-mcp/mailbox.sqlite3`，可用
`MAILBOX_HOME` 或 `--data-dir` 改变）。选择它的原因：

- **WAL**：多个 MCP 端点与适配器并发读、单写者写，读不阻塞写；
- **外键**：SQLite 默认关闭外键，本项目每条连接都显式打开；
- **显式事务**：写事务统一 `BEGIN IMMEDIATE`，避免"读事务升级为写事务"时的
  `SQLITE_BUSY` 死锁；
- **唯一性交给数据库**：账号唯一键、幂等键、同一对账号只允许一个未关闭的直接对话、
  每个账号最多一个当前主连接，全部用唯一索引表达，而不是应用层"先查后写"
  （先查后写在并发下必然产生重复）。

Markdown / JSONL 只作为兼容输入或人类可读投影，**不参与**业务状态反向解析。

## 表结构（v1）

| 表 | 作用 | 关键约束 |
|----|------|----------|
| `accounts` | 邮箱账号（一个原生会话一个账号） | 唯一 `(host_type, host_instance_id, native_session_id)` |
| `connections` | 账号的一次临时接入实例 | 部分唯一索引保证每账号最多一个 `is_current=1`；唯一 `(account_id, generation)` |
| `conversations` | 一对一会话线程 | 部分唯一索引保证同一对账号最多一个未关闭的直接对话 |
| `conversation_participants` | 成员、已读位点、未读数 | 主键 `(conversation_id, account_id)` |
| `messages` | 消息（回复是带 `reply_to` 的新消息） | 部分唯一索引 `(sender_account_id, idempotency_key)`；`rowid` 即入队顺序 |
| `deliveries` | 每个收件账号一次投递 | `delivery_id` 唯一；唯一 `(message_id, account_id)`；`delivery_seq` 自增保证补投顺序 |
| `message_processing` | 处理状态（pending/running/completed/…） | 主键 `(message_id, account_id)` |
| `adapter_checkpoints` | 适配器自有的进度存储 | 主键 `(account_id, adapter_name)` |
| `schema_migrations` | 已应用的迁移 | 主键 `version` |
| `audit_events` | 只追加的审计事件 | 按 `event_type`、`conversation_id` 建索引 |
| `account_contacts` | 允许/阻止联系人 | 主键 `(owner_account_id, contact_account_id)` |
| `rate_counters` | 固定窗口速率计数 | 主键 `(scope_key, window_start)` |

### 为什么有两个"顺序"列

- `messages.rowid`：同一毫秒内创建的消息靠它严格排序，分页游标也用它。时间戳精度
  不足以保证稳定顺序，随机 ID 更不能。
- `deliveries.delivery_seq`：自增主键。离线积压补投必须严格按入队顺序，而
  `created_at` 会并列、`delivery_id` 是随机的。

### 三个状态维度（不要合并）

```text
delivery:   queued -> dispatched -> delivered | failed | dead_letter
visibility: unread -> seen
processing: pending -> running -> completed | blocked | cancelled | failed
```

`delivered` 只表示宿主已可靠接收；`completed` 是处理回执；回复是新消息。

## 迁移规则

1. 版本**单调递增**，只追加不修改；已发布的迁移永远不得改写（内容会算入
   `schema_migrations.checksum`）。
2. 每个迁移单独在一个 `BEGIN IMMEDIATE` 事务里执行，**失败即回滚**，绝不留半张表。
3. 支持从空数据库初始化（v1 建全部核心表）。
4. 能检测当前版本；重复调用幂等。
5. 注册表自检在**每次启动**运行：版本重复或非递增会直接报错，而不是静默跳过迁移。

自动化测试见 `tests/test_migrations.py`（含"坏迁移必须回滚且不留半张表"）。

## 运维命令

```bash
# 应用迁移（幂等，可反复执行）
uv run python -m mcp_agent_mailbox.cli migrate

# 完整诊断：完整性检查、账号与在线状态分布、待投递、死信、生效参数
uv run python -m mcp_agent_mailbox.cli doctor

# 列出账号与在线状态
uv run python -m mcp_agent_mailbox.cli accounts

# 某个账号的对话与待投递
uv run python -m mcp_agent_mailbox.cli conversations acc_...
uv run python -m mcp_agent_mailbox.cli deliveries acc_...
```

## 升级与备份

升级前先复制数据库文件（连同 `-wal` / `-shm`，或先做一次 checkpoint）：

```bash
uv run python -c "
from mcp_agent_mailbox.config import load_settings
from mcp_agent_mailbox.infrastructure.sqlite import Database
db = Database(load_settings().database_path)
db.wal_checkpoint('TRUNCATE')
print('checkpoint 完成:', db.integrity_check())
"
```

回滚：停止所有邮箱进程 → 用备份覆盖数据库文件 → 用旧版本代码启动。
**不要**手工删除 `schema_migrations` 里的行来"回滚"，那会让迁移重复执行。

## 已知限制

- 单机单用户设计：没有跨机同步、没有多租户身份认证。
- `audit_events` 只追加不限量增长；当前没有自动归档（诊断命令会显示死信数量，
  可据此人工清理）。
- SQLite 是单写者：极高并发写入会成为瓶颈。当前规模（本地多会话协作）远未触及。
