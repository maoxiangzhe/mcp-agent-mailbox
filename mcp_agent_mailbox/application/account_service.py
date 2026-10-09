"""账号与连接服务：自动注册、幂等恢复、连接代次、心跳续租。

这一层是**身份安全**的执行点：

- 发送者身份永远来自 ``connection_id``，绝不接受调用方传入的账号标识；
- 只有当前主连接（``is_current``）能确认投递；旧代次连接确认一律拒绝
  （``IdentityMismatchError``）；
- 重连恢复同一个 ``account_id``，只推进连接代次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..domain.accounts import (
    Account,
    Connection,
    ConnectionState,
    HostIdentity,
    render_address,
)
from ..domain.errors import IdentityMismatchError, NotFoundError, ValidationError
from ..domain.ids import new_account_id, new_connection_id
from ..domain.presence import HostCapabilityLevel, PresenceState
from ..domain.process import own_process_id
from ..domain.timestamps import format_timestamp, plus_seconds
from ..ports.event_transport import Event, Subscription
from .context import ServiceContext

__all__ = ["AccountService", "BoundSession", "ContactPolicyEntry", "RegisterResult"]


@dataclass(frozen=True, slots=True)
class RegisterResult:
    """注册/重连的结果。"""

    account: Account
    connection: Connection
    created: bool
    generation: int

    @property
    def account_id(self) -> str:
        return self.account.account_id

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account.account_id,
            "created": self.created,
            "generation": self.generation,
            "address": self.account.address,
            "host_type": self.account.host_type,
            "host_instance_id": self.account.host_instance_id,
            "native_session_id": self.account.native_session_id,
            "capability_level": int(self.connection.capability_level),
            "capability_slug": self.connection.capability_level.slug,
        }


@dataclass(frozen=True, slots=True)
class BoundSession:
    """一次会话绑定的完整信息，供 MCP 连接上下文与适配器使用。"""

    account: Account
    connection: Connection
    created: bool

    @property
    def account_id(self) -> str:
        return self.account.account_id

    @property
    def connection_id(self) -> str:
        return self.connection.connection_id

    @property
    def generation(self) -> int:
        return self.connection.generation


@dataclass(frozen=True, slots=True)
class ContactPolicyEntry:
    """联系人策略条目。"""

    account_id: str
    policy: str  # 'allow' | 'block'
    note: str | None = None


class AccountService:
    """账号注册、连接生命周期与联系人策略。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._settings = context.settings
        self._clock = context.clock

    # -- 注册 -------------------------------------------------------------

    def register_account(
        self,
        identity: HostIdentity,
        *,
        display_name: str,
        workspace_hint: str | None = None,
        capability_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY,
        metadata: dict[str, str] | None = None,
    ) -> tuple[Account, bool]:
        """按稳定唯一键注册账号；已存在则恢复并刷新元数据。

        返回 ``(账号, 是否新建)``。这是幂等的：相同身份重复注册只会更新
        ``display_name`` / ``workspace_hint`` / 能力等级，不产生新账号。
        """
        now = self._clock.now()
        extra = dict(metadata or {})
        with self._context.uow().transaction() as uow:
            existing = uow.accounts.find_by_identity(identity)
            if existing is None:
                account = Account(
                    account_id=new_account_id(),
                    identity=identity,
                    display_name=display_name.strip() or identity.native_session_id,
                    address=render_address(
                        identity.host_type, display_name, identity.host_instance_id
                    ),
                    workspace_hint=workspace_hint,
                    capability_level=capability_level,
                    metadata=extra,
                    created_at=now,
                    updated_at=now,
                )
                uow.accounts.add(account)
                self._context.audit(
                    uow,
                    "account.registered",
                    now=now,
                    account_id=account.account_id,
                    detail={
                        "host_type": identity.host_type, "created": True,
                        "registration": "new",
                        "host_instance_id": identity.host_instance_id,
                        "native_session_id": identity.native_session_id,
                        "display_name": account.display_name,
                    },
                )
                created = True
            else:
                account = existing
                previous_display_name = account.display_name
                account.display_name = display_name.strip() or existing.display_name
                account.address = render_address(
                    identity.host_type, account.display_name, identity.host_instance_id
                )
                if workspace_hint is not None:
                    account.workspace_hint = workspace_hint
                account.capability_level = max(account.capability_level, capability_level)
                account.metadata = {**account.metadata, **extra}
                account.updated_at = now
                uow.accounts.update(account)
                self._context.audit(
                    uow,
                    "account.registered",
                    now=now,
                    account_id=account.account_id,
                    detail={
                        "host_type": identity.host_type, "created": False,
                        "registration": "restored",
                        "host_instance_id": identity.host_instance_id,
                        "native_session_id": identity.native_session_id,
                        "display_name": account.display_name,
                        "previous_display_name": previous_display_name,
                    },
                )
                if previous_display_name != account.display_name:
                    self._context.audit(
                        uow, "account.renamed", now=now, account_id=account.account_id,
                        detail={
                            "from": previous_display_name, "to": account.display_name,
                            "host_type": identity.host_type,
                            "host_instance_id": identity.host_instance_id,
                            "native_session_id": identity.native_session_id,
                        },
                    )
                created = False
        return account, created

    # -- 连接 -------------------------------------------------------------

    def open_connection(
        self,
        account_id: str,
        *,
        adapter_name: str | None = None,
        capability_level: HostCapabilityLevel | None = None,
        remote_hint: str | None = None,
        host_pid: int | None = None,
    ) -> Connection:
        """为账号开启新连接，并成为当前主连接。

        步骤（同一事务内完成，避免出现两个主连接）：
            1. 把账号原有的当前连接降级为 ``superseded``；
            2. 生成新的连接代次（账号级单调递增）；
            3. 写入新连接，标记 ``is_current=1``，记录托管进程 PID。

        ``host_pid`` 默认取当前进程——**在线状态的首要依据就是这个进程是否活着**。
        调用方只有在替别的进程登记时才需要显式传入（例如运维脚本）。
        """
        now = self._clock.now()
        lease_until = plus_seconds(now, self._settings.presence.lease_seconds)
        pid = own_process_id() if host_pid is None else int(host_pid)
        with self._context.uow().transaction() as uow:
            account = uow.accounts.get(account_id)
            if account is None:
                raise NotFoundError(f"账号不存在：{account_id}")

            superseded = uow.connections.demote_current(
                account_id, state=ConnectionState.SUPERSEDED, at=now
            )
            generation = uow.accounts.next_generation(account_id)
            level = capability_level if capability_level is not None else account.capability_level
            connection = Connection(
                connection_id=new_connection_id(),
                account_id=account_id,
                generation=generation,
                state=ConnectionState.ACTIVE,
                is_current=True,
                capability_level=level,
                adapter_name=adapter_name,
                remote_hint=remote_hint,
                host_pid=pid,
                opened_at=now,
                heartbeat_at=now,
                lease_expires_at=lease_until,
            )
            uow.connections.add(connection)
            uow.accounts.set_current_generation(account_id, generation, now)
            uow.deliveries.resume_offline(account_id, at=now)
            if superseded:
                self._context.audit(
                    uow,
                    "connection.superseded",
                    now=now,
                    account_id=account_id,
                    detail={"superseded_count": superseded, "new_generation": generation},
                )
            self._context.audit(
                uow,
                "connection.opened",
                now=now,
                account_id=account_id,
                connection_id=connection.connection_id,
                detail={
                    "generation": generation,
                    "capability_level": int(level),
                    "adapter": adapter_name,
                },
            )
        return connection

    def bind_session(
        self,
        identity: HostIdentity,
        *,
        display_name: str,
        workspace_hint: str | None = None,
        capability_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY,
        adapter_name: str | None = None,
        metadata: dict[str, str] | None = None,
        host_pid: int | None = None,
    ) -> BoundSession:
        """注册（或恢复）账号并绑定一条新连接。适配器接入的标准入口。

        ``host_pid`` 是**托管该会话的进程**。默认取当前进程，但调用方应当显式传入
        "真正长期活着的那个进程"的 PID：在 Windows 上 ``.venv\\Scripts\\python.exe``
        是个启动器，它会 re-exec 真正的解释器，于是"当前进程"是启动器——启动器一退出，
        账号就会错误地变成离线。
        """
        account, created = self.register_account(
            identity,
            display_name=display_name,
            workspace_hint=workspace_hint,
            capability_level=capability_level,
            metadata=metadata,
        )
        connection = self.open_connection(
            account.account_id,
            adapter_name=adapter_name,
            capability_level=capability_level,
            host_pid=host_pid,
        )
        subscription = Subscription(
            account_id=account.account_id,
            connection_id=connection.connection_id,
            generation=connection.generation,
            capability_level=int(connection.capability_level),
        )
        self._context.publish(
            Event(
                type="connection/opened",
                account_id=account.account_id,
                created_at=connection.opened_at,
                generation=connection.generation,
                payload={
                    "connection_id": connection.connection_id,
                    "generation": connection.generation,
                    "account_id": account.account_id,
                },
            )
        )
        # 让适配器有机会立刻订阅（实现方是 Broker 时这里会登记订阅）。
        register = getattr(self._context.publisher, "register", None)
        if callable(register):
            try:
                register(subscription)
            except Exception:  # noqa: BLE001 - 订阅失败不应让注册回滚
                self._context.logger.exception(
                    "订阅登记失败",
                    extra={
                        "event": "subscription.failed",
                        "context": {"account_id": account.account_id},
                    },
                )
        return BoundSession(account=account, connection=connection, created=created)

    def renew(self, connection_id: str) -> Connection:
        """续租心跳。只有活动连接可以续租；已关闭/过期的连接必须拒绝。"""
        now = self._clock.now()
        lease_until = plus_seconds(now, self._settings.presence.lease_seconds)
        with self._context.uow().transaction() as uow:
            connection = uow.connections.get(connection_id)
            if connection is None:
                raise NotFoundError(f"连接不存在：{connection_id}")
            if not connection.is_open:
                raise IdentityMismatchError(
                    f"连接 {connection_id} 状态为 {connection.state.value}，不能续租"
                )
            connection.heartbeat_at = now
            connection.lease_expires_at = lease_until
            uow.connections.update(connection)
            self._context.audit(
                uow,
                "connection.renewed",
                now=now,
                account_id=connection.account_id,
                connection_id=connection_id,
                detail={"generation": connection.generation},
            )
        return connection

    def close_connection(
        self,
        connection_id: str,
        *,
        reason: str = "client_closed",
    ) -> Connection:
        """正常关闭连接。幂等：已关闭的连接再次关闭不报错。"""
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            connection = uow.connections.get(connection_id)
            if connection is None:
                raise NotFoundError(f"连接不存在：{connection_id}")
            if connection.is_open:
                connection.state = ConnectionState.CLOSED
                connection.is_current = False
                connection.closed_at = now
                connection.heartbeat_at = connection.heartbeat_at or now
                uow.connections.update(connection)
                self._context.audit(
                    uow,
                    "connection.closed",
                    now=now,
                    account_id=connection.account_id,
                    connection_id=connection_id,
                    detail={"reason": reason, "generation": connection.generation},
                )
        self._context.publish(
            Event(
                type="connection/closed",
                account_id=connection.account_id,
                created_at=now,
                generation=connection.generation,
                payload={
                    "connection_id": connection_id,
                    "reason": reason,
                    "account_id": connection.account_id,
                },
            )
        )
        unregister = getattr(self._context.publisher, "unregister", None)
        if callable(unregister):
            try:
                unregister(connection_id)
            except Exception:  # noqa: BLE001
                self._context.logger.exception(
                    "订阅注销失败",
                    extra={"event": "subscription.failed", "context": {"connection_id": connection_id}},
                )
        return connection

    def sweep_disconnected_connections(self) -> list[str]:
        """回收托管进程已经退出的活动连接。

        在线严格参照进程，所以这里**不看租约**：进程还活着的连接永不因时间回收
        （会话在思考时不发心跳，按租约回收会把活着的账号判离线）；进程已退出或
        没有进程信息的连接一律回收。
        """
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            expired = uow.connections.mark_disconnected()
            if expired:
                self._context.audit(
                    uow,
                    "connection.recycled",
                    now=now,
                    detail={"expired_count": len(expired), "connection_ids": expired[:20]},
                )
        return expired

    # -- 身份校验（安全边界） ---------------------------------------------

    def require_current_connection(self, uow, connection_id: str) -> Connection:
        """要求连接存在、处于活动状态、且是账号的当前主连接。

        适配器确认投递、更新处理状态前必须过这一关：旧代次连接确认新投递会让
        "谁收到了"变得不可信。
        """
        connection = uow.connections.get(connection_id)
        if connection is None:
            raise NotFoundError(f"连接不存在：{connection_id}")
        if not connection.is_open:
            raise IdentityMismatchError(
                f"连接 {connection_id} 已处于 {connection.state.value}，不能执行该操作"
            )
        current = uow.connections.current_for_account(connection.account_id)
        if current is None or current.connection_id != connection_id:
            raise IdentityMismatchError(
                f"连接 {connection_id} 已被同账号的新连接取代（当前主连接："
                f"{current.connection_id if current else '无'}）"
            )
        return connection

    def require_connection(self, uow, connection_id: str) -> Connection:
        """要求连接存在且处于活动状态（不要求是主连接）。"""
        connection = uow.connections.get(connection_id)
        if connection is None:
            raise NotFoundError(f"连接不存在：{connection_id}")
        if not connection.is_open:
            raise IdentityMismatchError(
                f"连接 {connection_id} 已处于 {connection.state.value}"
            )
        return connection

    def account_for_connection(self, uow, connection_id: str) -> Account:
        """由连接解析账号。这是"发送者身份只能来自连接"的唯一实现。"""
        connection = self.require_connection(uow, connection_id)
        account = uow.accounts.get(connection.account_id)
        if account is None:
            raise NotFoundError(f"连接对应的账号不存在：{connection.account_id}")
        if account.blocked:
            raise ValidationError(f"账号已被停用：{account.account_id}")
        return account

    # -- 联系人策略 -------------------------------------------------------

    def set_contact_policy(
        self, owner_account_id: str, contact_account_id: str, policy: str, *, note: str | None = None
    ) -> ContactPolicyEntry:
        """设置允许/阻止联系人。``policy`` 只能是 ``allow`` 或 ``block``。"""
        if policy not in ("allow", "block"):
            raise ValidationError("联系人策略只能是 allow 或 block")
        if owner_account_id == contact_account_id:
            raise ValidationError("不能把自己设为联系人")
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            if uow.accounts.get(owner_account_id) is None:
                raise NotFoundError(f"账号不存在：{owner_account_id}")
            if uow.accounts.get(contact_account_id) is None:
                raise NotFoundError(f"账号不存在：{contact_account_id}")
            uow.contacts.set_policy(
                owner_account_id=owner_account_id,
                contact_account_id=contact_account_id,
                policy=policy,
                note=note,
                now=now,
            )
            self._context.audit(
                uow,
                "contact.policy_changed",
                now=now,
                account_id=owner_account_id,
                detail={"contact_account_id": contact_account_id, "policy": policy},
            )
        return ContactPolicyEntry(account_id=contact_account_id, policy=policy, note=note)

    def list_contact_policies(self, owner_account_id: str) -> list[ContactPolicyEntry]:
        with self._context.uow().transaction() as uow:
            rows = uow.contacts.list_policies(owner_account_id)
        return [ContactPolicyEntry(**row) for row in rows]

    def assert_contact_allowed(self, uow, sender_account_id: str, recipient_account_id: str) -> None:
        """检查双方联系人策略：任一方 block 则拒绝。

        放在服务层（而不是 MCP 层）是为了保证所有入口——工具、适配器、兼容层——
        都过同一道检查。
        """
        from ..domain.errors import RecipientBlockedError

        policies = uow.contacts.policy_between(sender_account_id, recipient_account_id)
        if policies.get((sender_account_id, recipient_account_id)) == "block":
            raise RecipientBlockedError("你已将对方加入阻止列表，无法发送")
        if policies.get((recipient_account_id, sender_account_id)) == "block":
            raise RecipientBlockedError("对方已将你加入阻止列表，消息未发送")


def summarize_presence(state: PresenceState) -> str:
    """给人类与模型看的简短说明（在线只看托管进程）。"""
    return {
        PresenceState.CONNECTED: "在线（托管进程活着：可以直接投递并让它开工）",
        PresenceState.OFFLINE: "离线（托管进程不在：消息排队，等它上线再收）",
    }[state]


def format_lease(connection: Connection) -> str:
    return format_timestamp(connection.lease_expires_at)
