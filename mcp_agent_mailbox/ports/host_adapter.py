"""宿主适配器端口。

适配器把邮箱协议接到宿主原生会话系统上。统一契约见 ``docs/host-capability-matrix.md``；
等级定义见 ``domain.presence.HostCapabilityLevel``。

两条不可越界的规则：

1. **不得伪造能力。** 探测不到正式注入/唤醒接口时，适配器必须显式返回
   ``unsupported`` 并降级到 Level 0/1，禁止通过 UI 自动化或直接改写宿主内部
   会话文件／数据库来"实现"唤醒。
2. **注入必须带来源信封。** 消息正文是不可信数据，注入时必须以结构化信封标明
   来源账号、对话和消息 ID，宿主侧不得据此放宽沙箱或审批策略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.presence import HostCapabilityLevel

__all__ = [
    "AdapterCapabilities",
    "ExternalEnvelope",
    "HostAdapter",
    "InjectionOutcome",
    "InjectionResult",
    "ProbeResult",
    "Registration",
    "SessionBinding",
    "WakeResult",
    "render_envelope",
]


@dataclass(frozen=True, slots=True)
class ExternalEnvelope:
    """注入宿主时使用的结构化来源信封（设计文档 §10）。

    目的有两个：
        - 让宿主里的模型清楚知道这是**来自另一个智能体账号的外部内容**；
        - 让安全边界可审计——信封里没有、也不允许有权限声明。
    """

    from_address: str
    conversation_id: str
    message_id: str
    delivery_id: str
    reply_to: str | None = None
    content_type: str = "text/plain"

    def header(self) -> str:
        lines = [
            "[External agent message]",
            f"From: {self.from_address}",
            f"Conversation: {self.conversation_id}",
            f"Message: {self.message_id}",
        ]
        if self.reply_to:
            lines.append(f"In-Reply-To: {self.reply_to}")
        lines.append("Trust: untrusted peer content")
        return "\n".join(lines)


def render_envelope(envelope: ExternalEnvelope, content: str) -> str:
    """把信封与正文拼成最终注入文本。

    正文原样附在信封之后，不做指令化改写；也不在信封里追加任何权限或审批声明。
    """
    return f"{envelope.header()}\n\n{content}"


@dataclass(frozen=True, slots=True)
class SessionBinding:
    """宿主返回的原生会话绑定信息。

    三个字段都必须来自宿主，不得由模型参数指定，也不得用随机值兜底。
    拿不到 ``native_session_id`` 时适配器应报告能力不足，而不是伪造一个。
    """

    host_type: str
    host_instance_id: str
    native_session_id: str
    display_name: str
    workspace_hint: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """适配器探测结果。"""

    level: HostCapabilityLevel
    adapter_name: str
    verified: bool
    """``True`` 表示本机实测通过；``False`` 表示按设计/文档声明但未在本机验证。"""

    can_receive_realtime: bool
    can_wake: bool
    notes: tuple[str, ...] = ()
    def to_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter_name,
            "level": int(self.level),
            "level_slug": self.level.slug,
            "verified": self.verified,
            "can_receive_realtime": self.can_receive_realtime,
            "can_wake": self.can_wake,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """能力探测结果。"""

    supported: bool
    level: HostCapabilityLevel
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InjectionResult:
    """一次消息注入的结果。

    ``outcome`` 的语义必须严格：
        ``injected``     已写入宿主正式 inbox 并由宿主持久化
        ``duplicate``    该 ``delivery_id`` 已注入过，本次为去重命中
        ``unsupported``  当前适配器不支持注入，调用方应降级
        ``failed``       尝试失败，可重试
    """

    outcome: str
    detail: str = ""
    injected_at: datetime | None = None
    wake_requested: bool = False

    @property
    def is_success(self) -> bool:
        return self.outcome in ("injected", "duplicate")


@dataclass(frozen=True, slots=True)
class WakeResult:
    """一次会话唤醒请求的结果。"""

    requested: bool
    started: bool
    detail: str = ""
    unsupported_reason: str | None = None


@dataclass(slots=True)
class Registration:
    """适配器向 Broker 注册时提交的信息。"""

    binding: SessionBinding
    capabilities: AdapterCapabilities
    adapter_name: str
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class HostAdapter(Protocol):
    """宿主适配器统一契约。

    生命周期：``probe()`` -> ``register()`` -> 循环(``renew()`` / ``inject()`` / 接收事件)
    -> ``shutdown()``。
    """

    @property
    def name(self) -> str:
        ...

    def probe(self) -> ProbeResult:
        """只读探测宿主是否提供正式的注入/唤醒接口。

        实现**不得**写入宿主任何状态；探测失败要如实返回 ``supported=False``。
        """
        ...

    def register(self, connection_id: str) -> Registration:
        """获取原生会话绑定并声明能力等级。"""
        ...

    def renew(self, connection_id: str) -> None:
        """续租心跳。"""
        ...

    def receive_event(self, timeout: float) -> dict[str, object] | None:
        """等待下一个实时事件；无事件返回 ``None``。"""
        ...

    def inject(self, delivery: dict[str, object], envelope: ExternalEnvelope, content: str) -> InjectionResult:
        """把消息注入目标原生会话的正式 inbox。

        ``delivery`` 至少包含 ``delivery_id`` / ``connection_id`` / ``generation``，
        适配器必须按 ``delivery_id`` 去重。重复注入必须返回 ``duplicate``。
        """
        ...

    def wake(self, connection_id: str) -> WakeResult:
        """请求宿主为对应原生会话启动一个新模型回合。

        只有 Level 2 适配器可以返回 ``started=True``；其余必须返回
        ``requested=False`` 并给出 ``unsupported_reason``。
        """
        ...

    def report_processing(
        self, message_id: str, state: str, *, result: str | None = None
    ) -> bool:
        """把处理状态（running/completed/blocked/cancelled/failed）回传邮箱。"""
        ...

    def shutdown(self, connection_id: str) -> None:
        """连接结束时释放资源。不得删除宿主数据。"""
        ...
