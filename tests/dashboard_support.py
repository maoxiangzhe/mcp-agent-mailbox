"""只读监控台的测试支撑：数据种子、快照、真实 HTTP 客户端。

关键设计：**用现有应用服务与仓储写入种子数据**，而不是手写 INSERT。
这样种子数据天然满足约束（唯一索引、外键、状态机），也让"监控台只读"的验证
针对真实形态的数据。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, PresencePolicy, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import ConnectionState, HostIdentity
from mcp_agent_mailbox.domain.messages import DeliveryState, ProcessingState
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel
from mcp_agent_mailbox.domain.timestamps import utc_now
from mcp_agent_mailbox.ports.clock import FrozenClock

#: 业务表清单：快照覆盖这些表的**全部行与列**。
BUSINESS_TABLES = (
    "accounts",
    "connections",
    "conversations",
    "conversation_participants",
    "messages",
    "deliveries",
    "message_processing",
    "adapter_checkpoints",
    "schema_migrations",
    "audit_events",
    "account_contacts",
    "rate_counters",
)

#: 不可能存在的 PID，用来表示"托管进程已退出"。
DEAD_PID = 2_147_483_646


@dataclass(slots=True)
class Seeded:
    """种子数据的句柄，测试用它断言具体内容。"""

    broker: Broker
    clock: FrozenClock
    settings: Any
    accounts: dict[str, str]
    conversations: dict[str, str]
    message_ids: list[str]

    @property
    def database_path(self) -> Path:
        return Path(self.settings.database_path)


def make_settings(tmp_path: Path):
    return load_settings(
        data_dir=tmp_path / "mailbox",
        presence=PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10),
        rate_limits=RateLimits(),
        delivery=DeliveryPolicy(),
    )


def seed(tmp_path: Path, *, message_count: int = 4, extra_accounts: int = 0) -> Seeded:
    """构造一份覆盖各种状态的数据集。

    覆盖：Level 0 / Level 1 / Level 2、活跃连接、过期租约、无连接账号、
    五种投递状态、三种可见性、四种处理状态、reply_to 关系、多页数据。
    """
    settings = make_settings(tmp_path)
    clock = FrozenClock(utc_now())
    # 刻意关掉"自动装配宿主注入通道"：种子数据必须**与环境无关**，
    # 否则在有 DSH 凭据的机器上会真的去 POST /api/session/prompt，
    # 断言也会随环境漂移（投递会被直连通道接管，不再走适配器确认路径）。
    broker = Broker(settings, clock=clock, auto_waker=False)

    def bind(key: str, *, session: str, host: str, level: int, name: str):
        return broker.accounts.bind_session(
            HostIdentity(host, "inst-1", session),
            display_name=name,
            capability_level=HostCapabilityLevel(level),
            adapter_name=f"{host}-adapter",
        )

    a = bind("a", session="session-a", host="dsh", level=2, name="DSH 会话 A")
    b = bind("b", session="session-b", host="dsh", level=1, name="DSH 会话 B")
    c = bind("c", session="task-c", host="codex", level=0, name="Codex 任务 C")
    # 第四个账号是 Level 2，用来演示"真正能收到实时投递并被确认"的路径。
    d = bind("d", session="session-d", host="dsh", level=2, name="DSH 会话 D")

    accounts = {"a": a.account_id, "b": b.account_id, "c": c.account_id, "d": d.account_id}
    for index in range(extra_accounts):
        extra = bind(
            f"extra-{index}",
            session=f"session-extra-{index}",
            host="dsh",
            level=index % 3,
            name=f"额外会话 {index}",
        )
        accounts[f"extra-{index}"] = extra.account_id

    # 一条"托管进程已退出"的连接：账号存在但进程没了 -> 离线。
    expired = bind("expired", session="session-expired", host="dsh", level=2, name="过期会话")
    accounts["expired"] = expired.account_id
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(expired.connection_id)
        assert connection is not None
        # 用"进程已退出"表达离线（产品规则：在线严格参照进程），而不是靠租约。
        connection.host_pid = DEAD_PID
        connection.durable = False
        uow.connections.update(connection)

    conversations: dict[str, str] = {}
    message_ids: list[str] = []

    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="第一条：请复核设计文档"
    )
    conversations["ab"] = first.conversation_id
    message_ids.append(first.message.message_id)

    for index in range(message_count - 1):
        sender = b if index % 2 == 0 else a
        result = broker.conversations.send_message(
            connection_id=b.connection_id if sender is b else a.connection_id,
            conversation_id=first.conversation_id,
            text=_payload(index),
        )
        message_ids.append(result.message.message_id)

    reply = broker.conversations.reply_message(
        connection_id=b.connection_id,
        message_id=message_ids[0],
        text="回复：已复核，结论见上",
    )
    message_ids.append(reply.message.message_id)

    second = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=c.account_id, text="给 Codex 的第一条"
    )
    conversations["ac"] = second.conversation_id
    message_ids.append(second.message.message_id)

    # 一条有正常投递路径（Level 2 目标）的消息：会被真正 dispatched → delivered。
    third = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=d.account_id, text="给 D 的实时投递"
    )
    conversations["ad"] = third.conversation_id
    message_ids.append(third.message.message_id)

    # 派发并区分三种去向：
    #   - 给 B（Level 1）=> 只通知，投递保持未送达
    #   - 给 C（Level 0）=> 无实时通道，投递保持 queued
    #   - 给 D（Level 2）=> dispatched，随后被确认 delivered
    broker.deliveries.dispatch_due()
    broker.deliveries.acknowledge(connection_id=d.connection_id, delivery_id=third.delivery_id)
    broker.deliveries.fail(
        connection_id=c.connection_id,
        delivery_id=second.delivery_id,
        reason="适配器在确认前断开（示例失败原因）",
    )
    # 死在信里之前先造一条"可重试的失败"：这样 failed 与 dead_letter 同时存在，
    # 页面能分别展示"会自己重试"和"必须人工处理"两种积压。
    # 失败两次，中间拨表越过退避窗口：于是这一条同时具备真实尝试次数与下一次重试时间。
    transient = broker.conversations.send_message(
        connection_id=a.connection_id,
        conversation_id=second.conversation_id,
        text="这条只是暂时失败，会按退避重试",
    )
    broker.deliveries.dispatch_due()
    broker.deliveries.fail(
        connection_id=c.connection_id,
        delivery_id=transient.delivery_id,
        reason="目标适配器暂时不可用（示例失败原因）",
        retryable=True,
    )
    clock.advance(5)  # 越过退避窗口，让重试成熟
    broker.deliveries.dispatch_due()
    broker.deliveries.fail(
        connection_id=c.connection_id,
        delivery_id=transient.delivery_id,
        reason="目标适配器暂时不可用（示例失败原因）",
        retryable=True,
    )
    message_ids.append(transient.message.message_id)

    # 死信：直接推进到不可重试。
    extra_message = broker.conversations.send_message(
        connection_id=a.connection_id,
        conversation_id=second.conversation_id,
        text="这条会进入死信",
    )
    broker.deliveries.dispatch_due()
    broker.deliveries.fail(
        connection_id=c.connection_id,
        delivery_id=extra_message.delivery_id,
        reason="Codex 适配器不可重试的错误（示例）",
        retryable=False,
    )

    # 处理状态：running / completed / blocked。
    # 原第一条已被回复自动确认；用另一条发给 B 的消息展示 running。
    broker.conversations.set_message_status(
        connection_id=b.connection_id, message_id=message_ids[2], processing="running"
    )
    broker.conversations.set_message_status(
        connection_id=a.connection_id,
        message_id=reply.message.message_id,
        processing="completed",
        result="复核通过（示例结果）",
    )
    broker.conversations.set_message_status(
        connection_id=c.connection_id, message_id=second.message.message_id, processing="blocked", result="缺少凭据"
    )

    # 可见性：B 读过一部分，A 完全不读 => unread/seen 同时存在。
    broker.conversations.read_conversation(
        connection_id=b.connection_id, conversation_id=first.conversation_id, limit=2
    )

    return Seeded(
        broker=broker,
        clock=clock,
        settings=settings,
        accounts=accounts,
        conversations=conversations,
        message_ids=message_ids,
    )


def _payload(index: int) -> str:
    """生成内容多样的消息，包含恶意标记、Unicode 与超长文本。"""
    if index == 1:
        return "<script>alert('xss')</script> 这条正文包含脚本标签，必须按纯文本显示"
    if index == 2:
        return "<img src=x onerror=alert(1)> 以及 <b>HTML 标签</b> 与 & 符号"
    if index == 3:
        return "Unicode 检查：中文、emoji 🚀、组合字符 é、零宽​字符、RTL ‮مرحبا‬"
    if index == 4:
        return "超长正文行：" + ("长" * 400)
    return f"第 {index} 条测试消息（普通文本）"


def snapshot(database_path: Path) -> dict[str, str]:
    """对所有业务表做内容快照（逐行逐列哈希）。

    用 ``mode=ro`` 之外的普通连接读自己的测试库是可以的：测试库归测试所有。
    这里刻意不用监控台的只读模块，以免"用它自己验证自己"。
    """
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        digest = hashlib.sha256()
        rows_by_table: dict[str, str] = {}
        for table in BUSINESS_TABLES:
            try:
                rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            except sqlite3.OperationalError:
                rows_by_table[table] = "<missing>"
                continue
            table_digest = hashlib.sha256()
            for row in rows:
                payload = json.dumps(
                    {key: row[key] for key in row.keys()}, ensure_ascii=False, sort_keys=True, default=str
                )
                table_digest.update(payload.encode("utf-8"))
                digest.update(payload.encode("utf-8"))
            rows_by_table[table] = f"{len(rows)}:{table_digest.hexdigest()[:16]}"
        rows_by_table["__all__"] = digest.hexdigest()
        return rows_by_table
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 真实 HTTP 客户端
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpClient:
    """只做真实 HTTP 请求的测试客户端（不直接调用内部函数）。"""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, *, token: str | None = None) -> HttpResponse:
        url = self.base_url + path
        request = urllib.request.Request(url, method=method)
        credential = self.token if token is None else token
        if credential:
            request.add_header("Authorization", f"Bearer {credential}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return HttpResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                headers={key.lower(): value for key, value in (exc.headers or {}).items()},
                body=exc.read(),
            )

    def get(self, path: str, *, token: str | None = None) -> HttpResponse:
        return self.request("GET", path, token=token)

    def data(self, path: str) -> Any:
        response = self.get(path)
        assert response.status == 200, f"{path} -> HTTP {response.status}: {response.text[:200]}"
        payload = response.json
        assert payload["ok"] is True
        return payload
