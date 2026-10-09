"""MCP 连接上下文：把一次 MCP 连接绑定到唯一邮箱账号。

这是**身份安全**在 MCP 层的落点：

- 发送者身份来自 ``connection_id``，工具参数里没有、也不允许有 ``from`` / ``agent``；
- 原生会话 ID 可由宿主注入，也可在安装配置开放后由 AI 会话通过
  ``connect_mailbox`` 提交自己的 Shell 会话 ID；
- 当宿主不提供会话 ID 时，绑定失败必须显式报错并给出补救方式，绝不能退回
  "猜一个 ID 先跑起来"，那会把"会话寻址"退化成不可信的广播。
"""

from __future__ import annotations

import hashlib
import os
import platform
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.accounts import HostIdentity
from ..domain.errors import ValidationError
from ..domain.presence import HostCapabilityLevel
from ..ports.host_adapter import ProbeResult

__all__ = [
    "ENV_HOST_INSTANCE_ID",
    "ENV_HOST_TYPE",
    "ENV_SESSION_ID",
    "EnvironmentSessionProvider",
    "MailboxConnection",
    "SessionDescriptor",
    "SessionIdentityProvider",
    "UnavailableSessionProvider",
    "machine_instance_id",
]

# 适配器/宿主通过这些环境变量把身份交给 MCP 进程。
ENV_SESSION_ID = "MAILBOX_SESSION_ID"
ENV_HOST_TYPE = "MAILBOX_HOST_TYPE"
ENV_HOST_INSTANCE_ID = "MAILBOX_HOST_INSTANCE_ID"
ENV_CAPABILITY_LEVEL = "MAILBOX_CAPABILITY_LEVEL"

#: 各宿主已知的会话 ID 环境变量。DSH 的 MCP 子进程拿不到 ``DSH_SESSION_ID``
#: （DSH 会过滤掉所有 ``DSH_`` 前缀变量），所以这里只是"如果有就用"的兜底，
#: 不构成对 DSH 能力的宣称。
_HOST_SESSION_ENV = {
    "dsh": ("DSH_SESSION_ID",),
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
    "claude": ("CLAUDE_SESSION_ID",),
    "opencode": ("OPENCODE_SESSION_ID",),
}


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    """适配器提交的会话描述，对应设计文档 §5.1 的注册 JSON。"""

    host_type: str
    host_instance_id: str
    native_session_id: str
    display_name: str
    workspace_hint: str | None = None
    capability_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY
    adapter_name: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> HostIdentity:
        return HostIdentity(self.host_type, self.host_instance_id, self.native_session_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "host_type": self.host_type,
            "host_instance_id": self.host_instance_id,
            "native_session_id": self.native_session_id,
            "display_name": self.display_name,
            "workspace_hint": self.workspace_hint,
            "capability_level": int(self.capability_level),
            "capability_slug": self.capability_level.slug,
        }


@runtime_checkable
class SessionIdentityProvider(Protocol):
    """提供"当前 MCP 连接属于哪个原生会话"的权威来源。"""

    def probe(self) -> ProbeResult:
        """只读探测：能否拿到可用的会话身份，以及据此能达到的能力等级。"""
        ...

    def describe(self) -> SessionDescriptor:
        """返回会话描述；拿不到时抛 ``ValidationError``。"""
        ...


def machine_instance_id(data_dir: Path, environ: dict[str, str] | None = None) -> str:
    """稳定的"宿主实例"标识。

    同一台机器上的同一个数据目录必须得到同一个值，否则每次启动都会注册成新账号，
    "重连恢复原账号"就失效了。这里先查环境变量，再落一个持久化文件；不引入任何
    机器指纹采集。

    ``environ`` 显式传入时不读进程环境，便于测试构造隔离环境。
    """
    source = os.environ if environ is None else environ
    explicit = source.get(ENV_HOST_INSTANCE_ID)
    if explicit and explicit.strip():
        return explicit.strip()

    marker = Path(data_dir) / "host-instance-id"
    try:
        existing = marker.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        # 目录不可写时退回"按机器名+用户名推导"，仍然稳定，只是无法人工指定。
        pass

    derived = hashlib.sha256(
        f"{platform.node()}|{os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}".encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    value = f"auto-{derived}"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(value + "\n", encoding="utf-8")
    except OSError:
        pass
    return value


class EnvironmentSessionProvider:
    """从环境变量读取会话身份。

    适配器负责在启动 MCP 进程时把 ``MAILBOX_SESSION_ID`` 等变量放进子进程环境。
    对 DSH 来说，这需要 profile 里为该 MCP 服务器显式配置 ``env:`` 或由一个
    in-process 插件代跑——DSH 默认会过滤 ``DSH_*``，所以**不能**指望自动拿到。
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        host_type: str | None = None,
        default_capability: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._host_type = (host_type or os.environ.get(ENV_HOST_TYPE) or "").strip().lower()
        self._default_capability = default_capability
        self._environ = dict(environ) if environ is not None else dict(os.environ)

    # -- 探测 -------------------------------------------------------------

    def _session_id(self) -> tuple[str | None, str]:
        """返回 ``(会话 ID, 来源说明)``。"""
        direct = self._environ.get(ENV_SESSION_ID, "").strip()
        if direct:
            return direct, f"环境变量 {ENV_SESSION_ID}"
        if not self._host_type:
            return None, "未设置 MAILBOX_HOST_TYPE，无法推断宿主专有变量"
        for name in _HOST_SESSION_ENV.get(self._host_type, ()):
            value = self._environ.get(name, "").strip()
            if value:
                return value, f"环境变量 {name}"
        candidates = ", ".join(_HOST_SESSION_ENV.get(self._host_type, ())) or "（无）"
        return None, (
            f"未找到会话 ID：请设置 {ENV_SESSION_ID}；"
            f"{self._host_type} 的候选变量 {candidates} 均未出现"
        )

    def probe(self) -> ProbeResult:
        session_id, detail = self._session_id()
        capability = self._capability_level()
        if session_id is None:
            return ProbeResult(
                supported=False,
                level=HostCapabilityLevel.TOOLS_ONLY,
                detail=(
                    "拿不到原生会话 ID，无法把本连接绑定到唯一邮箱账号。"
                    f"（{detail}）"
                ),
                evidence=(
                    f"host_type={self._host_type or '未设置'}",
                    f"{ENV_SESSION_ID}={'有' if self._environ.get(ENV_SESSION_ID) else '无'}",
                ),
            )
        return ProbeResult(
            supported=True,
            level=capability,
            detail=f"已从{detail}取得原生会话 ID",
            evidence=(f"native_session_id_source={detail}",),
        )

    def _capability_level(self) -> HostCapabilityLevel:
        raw = self._environ.get(ENV_CAPABILITY_LEVEL, "").strip()
        if raw:
            try:
                return HostCapabilityLevel(int(raw))
            except ValueError as exc:
                raise ValidationError(
                    f"{ENV_CAPABILITY_LEVEL} 必须是 0/1/2，实际为 {raw!r}"
                ) from exc
        return self._default_capability

    # -- 描述 -------------------------------------------------------------

    def describe(self) -> SessionDescriptor:
        session_id, detail = self._session_id()
        if session_id is None:
            raise ValidationError(
                "本 MCP 连接没有原生会话身份，无法注册邮箱账号。"
                f"请让宿主适配器在启动本进程时注入 {ENV_SESSION_ID}（{detail}）。"
                "在拿到会话 ID 之前，邮箱工具只能报告不可用，不会替你猜一个身份。"
            )
        host_type = self._host_type or "unknown"
        display = self._environ.get("MAILBOX_DISPLAY_NAME", "").strip() or (
            f"{host_type}/{session_id}"
        )
        return SessionDescriptor(
            host_type=host_type,
            host_instance_id=machine_instance_id(self._data_dir, self._environ),
            native_session_id=session_id,
            display_name=display,
            workspace_hint=self._environ.get("MAILBOX_WORKSPACE") or os.getcwd(),
            capability_level=self._capability_level(),
            adapter_name=f"{host_type}-env",
            metadata={"identity_source": detail},
        )


class UnavailableSessionProvider:
    """明确"拿不到会话身份"的提供者：用于没有适配器的宿主。

    它的存在是为了让降级可见：工具会返回明确原因，而不是假装注册成功。
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def probe(self) -> ProbeResult:
        return ProbeResult(
            supported=False,
            level=HostCapabilityLevel.TOOLS_ONLY,
            detail=self._reason,
            evidence=("provider=unavailable",),
        )

    def describe(self) -> SessionDescriptor:
        raise ValidationError(self._reason)


class ExplicitRegistrationProvider:
    """调用方通过 MCP 工具显式提交会话描述时使用。

    ``MailboxConnection`` 默认拒绝显式注册；只有宿主安装配置明确开启时才接受。
    这用于无法把会话 ID 直接传入 MCP 子进程的宿主，例如 Codex Desktop。
    """

    def __init__(self) -> None:
        self._descriptor: SessionDescriptor | None = None
        self._reason = "适配器尚未提交会话描述"

    def submit(self, descriptor: SessionDescriptor) -> None:
        self._descriptor = descriptor

    def probe(self) -> ProbeResult:
        if self._descriptor is None:
            return ProbeResult(
                supported=False,
                level=HostCapabilityLevel.TOOLS_ONLY,
                detail=self._reason,
                evidence=("provider=explicit",),
            )
        return ProbeResult(
            supported=True,
            level=self._descriptor.capability_level,
            detail="已由适配器显式提交会话描述",
            evidence=("provider=explicit",),
        )

    def describe(self) -> SessionDescriptor:
        if self._descriptor is None:
            raise ValidationError(self._reason)
        return self._descriptor


class MailboxConnection:
    """当前 MCP 连接与邮箱账号的绑定状态。"""

    def __init__(
        self,
        *,
        provider: SessionIdentityProvider,
        allow_model_registration: bool = False,
    ) -> None:
        self._provider = provider
        self._allow_model_registration = allow_model_registration
        self.account_id: str | None = None
        self.connection_id: str | None = None
        self.generation: int | None = None
        #: 本进程 PID，绑定后记录；账号在线状态的依据。
        self.host_pid: int | None = None
        self.descriptor: SessionDescriptor | None = None
        self.created: bool = False
        self._binding_lock = threading.RLock()

    # -- 状态 -------------------------------------------------------------

    @property
    def is_bound(self) -> bool:
        return self.account_id is not None and self.connection_id is not None

    @property
    def allow_model_registration(self) -> bool:
        return self._allow_model_registration

    def probe(self) -> ProbeResult:
        return self._provider.probe()

    def describe_if_available(self) -> SessionDescriptor | None:
        """尽力返回描述；拿不到返回 ``None``（用于 ``whoami`` 的未绑定分支）。"""
        try:
            return self._provider.describe()
        except ValidationError:
            return None

    # -- 绑定 -------------------------------------------------------------

    def bind(self, service, *, reason: str = "auto") -> "MailboxConnection":
        """注册（或恢复）账号并绑定一条连接。幂等：已绑定则直接返回。

        绑定同时记录本进程 PID（由 ``AccountService.open_connection`` 写入），
        因此"宿主进程活着 => 账号在线"从这一刻起成立。
        """
        with self._binding_lock:
            return self._bind_locked(service, reason=reason)

    def _bind_locked(self, service, *, reason: str) -> "MailboxConnection":
        descriptor = self._provider.describe()
        self._assert_same_identity(descriptor)
        if self.is_bound:
            return self
        # 显式传"真正长期活着的进程"的 PID：Windows 上 .venv 的 python.exe 是启动器，
        # 它会 re-exec 真解释器；用启动器 PID 会导致它一退出账号就误判离线。
        from ..domain.process import own_process_id

        bound = service.bind_session(
            descriptor.identity,
            display_name=descriptor.display_name,
            workspace_hint=descriptor.workspace_hint,
            capability_level=descriptor.capability_level,
            adapter_name=descriptor.adapter_name,
            metadata={**descriptor.metadata, "binding": reason},
            host_pid=own_process_id(),
        )
        self.account_id = bound.account_id
        self.connection_id = bound.connection_id
        self.generation = bound.generation
        self.host_pid = bound.connection.host_pid
        self.descriptor = descriptor
        self.created = bound.created
        return self

    def _assert_same_identity(self, descriptor: SessionDescriptor) -> None:
        """One MCP connection cannot impersonate a different native session.

        Keep this constraint after a stale connection fails: rebinding may renew
        the same account, but cannot turn its existing context into another one.
        """
        if self.descriptor is not None and descriptor.identity != self.descriptor.identity:
            raise ValidationError(
                "当前 MCP 连接已经绑定其他会话，不能切换邮箱身份："
                f"{self.descriptor.identity.key!r} -> {descriptor.identity.key!r}。"
                "请为目标会话建立独立 MCP 连接。"
            )

    def fail(self, reason: str) -> None:
        """标记绑定失败（例如连接已被回收），下次调用会重新绑定。"""
        with self._binding_lock:
            self.account_id = None
            self.connection_id = None
            self.generation = None
            self._last_failure_reason = reason

    # -- 显式注册（适配器协议） -------------------------------------------

    def submit_adapter_registration(
        self,
        service,
        *,
        host_type: str,
        host_instance_id: str,
        native_session_id: str,
        display_name: str | None = None,
        workspace_hint: str | None = None,
        capability_level: int = 0,
        **extra,
    ) -> "MailboxConnection":
        """显式提交会话描述，供兼容适配器和 ``connect_mailbox`` 内部复用。"""
        if not self._allow_model_registration:
            raise ValidationError(
                "本进程未允许显式注册会话；会话身份必须由适配器在启动时通过环境变量"
                f"（{ENV_SESSION_ID}）注入。"
            )
        descriptor = SessionDescriptor(
            host_type=host_type,
            host_instance_id=host_instance_id,
            native_session_id=native_session_id,
            display_name=display_name or f"{host_type}/{native_session_id}",
            workspace_hint=workspace_hint,
            capability_level=HostCapabilityLevel(int(capability_level)),
            adapter_name=extra.get("adapter_name") or f"{host_type}-explicit",
            metadata={k: str(v) for k, v in extra.items() if v is not None},
        )
        with self._binding_lock:
            self._assert_same_identity(descriptor)
            if self.is_bound:
                return self
            self._provider = _FixedProvider(descriptor)
            return self._bind_locked(service, reason="adapter_explicit")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"MailboxConnection(account_id={self.account_id!r}, "
            f"connection_id={self.connection_id!r}, generation={self.generation!r})"
        )


class _FixedProvider:
    """把一份固定描述包装成 provider（显式注册后使用）。"""

    def __init__(self, descriptor: SessionDescriptor) -> None:
        self._descriptor = descriptor

    def probe(self) -> ProbeResult:
        return ProbeResult(
            supported=True,
            level=self._descriptor.capability_level,
            detail="已由适配器显式提交会话描述",
            evidence=("provider=explicit",),
        )

    def describe(self) -> SessionDescriptor:
        return self._descriptor


def new_request_id() -> str:
    """生成请求标识（幂等键的默认来源）。"""
    return uuid.uuid4().hex
