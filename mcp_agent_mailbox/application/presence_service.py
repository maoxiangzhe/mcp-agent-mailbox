"""在线状态服务：**只由宿主进程决定**。

presence 是**计算出来的**，不是模型声明的：

    connected  托管该会话的进程活着（在线）
    offline    没有连接、连接已关闭，或托管进程已经退出（离线）

租约、心跳、能力等级都不参与这个判定。能力等级只回答"能不能把消息注入目标会话并
启动一个回合"（``can_wake``），它不改变在线/离线。

``whoami`` / ``list_contacts`` / 投递决策都走这里，因此界面上显示的在线状态与投递
决策用的是同一份事实，不会出现"列表显示在线、实际投不进去"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.accounts import Account, Connection
from ..domain.presence import PresenceState, compute_presence
from .context import ServiceContext

__all__ = ["PresenceService", "PresenceView"]


@dataclass(frozen=True, slots=True)
class PresenceView:
    """一个账号的在线状态视图。"""

    account_id: str
    state: PresenceState
    capability_level: int
    capability_slug: str
    generation: int | None
    connection_id: str | None
    #: 托管该会话的进程 PID；在线状态的**唯一**依据。
    host_pid: int | None = None
    #: 仅诊断保留：租约到期时间不再参与在线判定。
    lease_expires_at: datetime | None = None
    #: 邮箱侧用来把消息**注入该宿主会话并启动回合**的通道名，没有就是 ``None``：
    #: ``plugin_channel``（DSH 进程内插件，B）/ ``http_channel``（DSH 本地接口，A）。
    wake_channel: str | None = None

    @property
    def is_online(self) -> bool:
        return self.state.is_online

    @property
    def can_receive(self) -> bool:
        """在线就能收：离线时消息只排队，等它上线。"""
        return self.is_online

    @property
    def has_wake_channel(self) -> bool:
        return self.wake_channel is not None

    @property
    def can_wake(self) -> bool:
        """能否"直接接收并开工"：在线 + 有注入通道。

        "有通道"有两种来源，都算数：
        * 邮箱侧有对得上该宿主的注入通道（B 插件优先，A 本地接口兜底）；
        * 目标自己声明的能力等级达到 Level 2（某个适配器会取走事件去注入）。
        """
        return self.is_online and (self.capability_level >= 2 or self.has_wake_channel)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "presence": self.state.value,
            "online": self.is_online,
            "can_receive": self.can_receive,
            "can_wake": self.can_wake,
            "wake_basis": self.wake_basis,
            "wake_channel": self.wake_channel,
            "capability_level": self.capability_level,
            "capability_slug": self.capability_slug,
            "generation": self.generation,
            "connection_id": self.connection_id,
            "host_pid": self.host_pid,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
        }

    @property
    def wake_basis(self) -> str:
        """``can_wake`` 的依据，方便人看清"为什么不能注入"。"""
        if not self.is_online:
            return "offline"
        if self.wake_channel is not None:
            return self.wake_channel
        if self.capability_level >= 2:
            return "declared_level2"
        return "no_channel"


class PresenceService:
    """从权威事实推导在线状态。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._policy = context.settings.presence

    def compute(
        self,
        *,
        account_id: str,
        connection: Connection | None,
        capability_level: int | None = None,
        host_type: str | None = None,
    ) -> PresenceView:
        channel = self.wake_channel_for(host_type)
        if connection is None:
            return PresenceView(
                account_id=account_id,
                state=PresenceState.OFFLINE,
                capability_level=0,
                capability_slug="tools-only",
                generation=None,
                connection_id=None,
                wake_channel=channel,
            )
        from ..domain.presence import HostCapabilityLevel

        level = HostCapabilityLevel(
            capability_level if capability_level is not None else int(connection.capability_level)
        )
        state = compute_presence(
            host_pid=connection.host_pid,
            closed=not connection.is_open,
        )
        return PresenceView(
            account_id=account_id,
            state=state,
            capability_level=int(level),
            capability_slug=level.slug,
            generation=connection.generation,
            connection_id=connection.connection_id,
            host_pid=connection.host_pid,
            lease_expires_at=connection.lease_expires_at,
            wake_channel=channel,
        )

    def wake_channel_for(self, host_type: str | None) -> str | None:
        """这个宿主现在**可用的**注入通道名；没有就返回 ``None``。

        不猜：通道链自己会探测（DSH 的 B 通道问插件的 healthz，A 通道看凭据能不能读）。
        这样 ``can_wake``/``wake_basis`` 显示的就是事实，而不是"配了就算有"。
        """
        waker = self._context.waker
        selector = getattr(waker, "for_host", None)
        if callable(selector):
            waker = selector(host_type)
        if waker is None or getattr(waker, "host_type", None) != host_type:
            return None
        probe = getattr(waker, "channel_available", None)
        if callable(probe) and not probe():
            return None
        name = getattr(waker, "channel_name", None)
        # 替身/旧实现没有通道名：只要能装配出来就记成 direct_channel。
        return str(name) if name else "direct_channel"

    def for_account(self, account_id: str) -> PresenceView:
        """单账号状态。"""
        with self._context.uow().transaction() as uow:
            connection = uow.connections.current_for_account(account_id)
            account = uow.accounts.get(account_id)
        return self.compute(
            account_id=account_id,
            connection=connection,
            host_type=(account.host_type if account is not None else None),
        )

    def for_accounts(self, accounts: list[Account]) -> dict[str, PresenceView]:
        """批量状态：一次事务里取全部主连接，避免 N 次查询。"""
        with self._context.uow().transaction() as uow:
            connections = {
                account.account_id: uow.connections.current_for_account(account.account_id)
                for account in accounts
            }
        return {
            account.account_id: self.compute(
                account_id=account.account_id,
                connection=connections.get(account.account_id),
                host_type=account.host_type,
            )
            for account in accounts
        }

    def for_account_ids(self, account_ids: list[str]) -> dict[str, PresenceView]:
        """按账号 ID 批量取状态（调用方只需要 ID 时用这个，不必构造占位账号）。"""
        with self._context.uow().transaction() as uow:
            connections = {
                account_id: uow.connections.current_for_account(account_id)
                for account_id in account_ids
            }
            accounts = {account_id: uow.accounts.get(account_id) for account_id in account_ids}
        return {
            account_id: self.compute(
                account_id=account_id,
                connection=connections.get(account_id),
                host_type=(
                    accounts[account_id].host_type
                    if accounts.get(account_id) is not None
                    else None
                ),
            )
            for account_id in account_ids
        }

    def can_wake(self, account_id: str) -> bool:
        """该账号能否被直接注入并开工（在线 + 有注入通道）。"""
        return self.for_account(account_id).can_wake
