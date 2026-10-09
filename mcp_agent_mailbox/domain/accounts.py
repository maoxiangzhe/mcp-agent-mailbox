"""账号与连接领域模型。

账号（``Account``）是稳定身份，唯一键为
``(host_type, host_instance_id, native_session_id)``；
连接（``Connection``）是账号的一次临时接入实例。

一个邮箱账号对应一个**原生会话**，而不是整个智能体软件：同一台机器上 DSH 的两个
会话是两个账号。相同唯一键重连必须恢复原账号，只更新连接信息（设计文档 §5.1）。

连接代次（``generation``）解决"新旧连接并存"：
新连接注册成功后成为当前主连接（``is_current=True``），旧代次不得确认新投递
（设计文档 §5.3）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from .presence import HostCapabilityLevel

__all__ = [
    "Account",
    "Connection",
    "ConnectionState",
    "HOST_TYPE_PATTERN",
    "HostIdentity",
    "normalize_host_type",
    "render_address",
]


class ConnectionState(enum.Enum):
    """连接生命周期。"""

    ACTIVE = "active"
    """已建立，尚未过期，可续租。"""

    CLOSED = "closed"
    """被显式关闭（客户端正常断开）。"""

    SUPERSEDED = "superseded"
    """被同账号的新代次取代，不再接收实时事件。"""

    EXPIRED = "expired"
    """租约超时被回收器判定失效。"""

    @property
    def is_open(self) -> bool:
        return self is ConnectionState.ACTIVE


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """账号稳定唯一键。

    三个字段都由宿主适配器提供，**不能来自模型参数**。原生会话 ID 必须由宿主给出；
    适配器拿不到时不得用随机值伪造，而应报告能力不足。
    """

    host_type: str
    host_instance_id: str
    native_session_id: str

    def __post_init__(self) -> None:
        for name in ("host_type", "host_instance_id", "native_session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 不能为空")
        object.__setattr__(self, "host_type", normalize_host_type(self.host_type))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.host_type, self.host_instance_id, self.native_session_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "host_type": self.host_type,
            "host_instance_id": self.host_instance_id,
            "native_session_id": self.native_session_id,
        }


# 宿主类型使用小写标识符，便于跨平台比较与日志聚合。
HOST_TYPE_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"


def normalize_host_type(value: str) -> str:
    """规范化宿主类型：去空白、转小写。保留原始拼写用于展示的地方请自行保存。"""
    return value.strip().lower()


def render_address(host_type: str, display_name: str, host_instance_id: str) -> str:
    """人类可读地址，例如 ``dsh:测试@desktop-default``。

    地址允许改名，**不参与引用完整性**；权威标识始终是 ``account_id``（设计文档 §5.1）。
    这里只做展示，不保证唯一，也不得用于路由。
    """
    safe_name = display_name.strip() or "unnamed"
    return f"{host_type}:{safe_name}@{host_instance_id}"


@dataclass(slots=True)
class Account:
    """邮箱账号：一个原生会话的稳定地址。"""

    account_id: str
    identity: HostIdentity
    display_name: str
    address: str
    created_at: datetime
    updated_at: datetime
    workspace_hint: str | None = None
    capability_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY
    blocked: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def host_type(self) -> str:
        return self.identity.host_type

    @property
    def host_instance_id(self) -> str:
        return self.identity.host_instance_id

    @property
    def native_session_id(self) -> str:
        return self.identity.native_session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "host_type": self.host_type,
            "host_instance_id": self.host_instance_id,
            "native_session_id": self.native_session_id,
            "display_name": self.display_name,
            "address": self.address,
            "workspace_hint": self.workspace_hint,
            "capability_level": int(self.capability_level),
            "capability_slug": self.capability_level.slug,
            "blocked": self.blocked,
        }


@dataclass(slots=True)
class Connection:
    """账号的一次临时接入实例。"""

    connection_id: str
    account_id: str
    generation: int
    state: ConnectionState
    opened_at: datetime
    lease_expires_at: datetime
    heartbeat_at: datetime | None = None
    closed_at: datetime | None = None
    is_current: bool = False
    capability_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY
    adapter_name: str | None = None
    remote_hint: str | None = None
    #: 历史字段：早期用来表达"没有实时通道的客户端不按租约过期"。
    #: 现在在线判定只认 ``host_pid``，租约（含本字段）都不参与在线/离线。
    durable: bool = False
    #: 托管该会话的宿主进程 PID。
    #: **在线状态的唯一依据**：这个进程活着，账号就在线；进程退出，账号就离线。
    #: 由宿主进程写入，模型无法指定。
    host_pid: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state.is_open

    def host_process_alive(self) -> bool:
        """托管进程是否还活着；**没有 PID 信息就是不在线**（不看租约）。"""
        if self.host_pid is None:
            return False
        from .process import is_process_alive

        return is_process_alive(self.host_pid)

    def to_dict(self) -> dict[str, object]:
        return {
            "connection_id": self.connection_id,
            "account_id": self.account_id,
            "generation": self.generation,
            "state": self.state.value,
            "is_current": self.is_current,
            "durable": self.durable,
            "host_pid": self.host_pid,
            "opened_at": self.opened_at.isoformat(),
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "lease_expires_at": self.lease_expires_at.isoformat(),
            "capability_level": int(self.capability_level),
            "adapter_name": self.adapter_name,
        }
