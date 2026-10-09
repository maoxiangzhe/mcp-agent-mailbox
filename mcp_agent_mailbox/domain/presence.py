"""在线状态：**只由宿主进程决定**。

产品规则（唯一事实来源）：

    托管该会话的进程活着   -> connected（在线：可以直接把消息投进去让它开工）
    进程已退出 / 没有进程  -> offline（离线：只收不发——消息排队等它回来）

刻意**不看**租约、心跳、能力等级：租约是"猜"，进程表才是事实。会话在思考时不会发
心跳，租约一到就被判离线，于是"明明开着却发不进去"；反过来，进程早退出了却因为租约
没过期而报在线，就会让发信方以为消息已送达。

``HostCapabilityLevel`` 回答的是**另一个问题**——"能不能把消息注入目标会话并让它
开始一个回合"（WAKE）——它不参与在线/离线判定：级别低只意味着"投进去需要目标自己
取信"，不意味着账号离线。

模型永远不能自己声明在线：``PresenceState`` 只能由 ``compute_presence`` 从事实推导。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import timedelta

__all__ = [
    "HostCapabilityLevel",
    "PresencePolicy",
    "PresenceState",
    "compute_presence",
]


class HostCapabilityLevel(enum.IntEnum):
    """宿主适配器可达到的注入能力等级。

    Level 0  tools-only  只能主动取信，邮箱无法把消息送进会话
    Level 1  notify      能把消息通知到宿主界面，但不能启动模型回合
    Level 2  wake        能把消息注入指定原生会话并**启动一个模型回合**
    """

    TOOLS_ONLY = 0
    NOTIFY = 1
    WAKE = 2

    @property
    def slug(self) -> str:
        return {0: "tools-only", 1: "notify", 2: "wake"}[int(self)]

    @property
    def can_receive_realtime(self) -> bool:
        """Level 1 及以上才有实时通道（哪怕只是通知）。"""
        return int(self) >= int(HostCapabilityLevel.NOTIFY)

    @property
    def can_wake_session(self) -> bool:
        """只有 Level 2 可以启动一个新模型回合。"""
        return int(self) >= int(HostCapabilityLevel.WAKE)


class PresenceState(enum.Enum):
    """账号在线状态：二值，只看托管进程。"""

    CONNECTED = "connected"
    """在线：托管该会话的进程活着。"""

    OFFLINE = "offline"
    """离线：没有托管进程，或该进程已经退出。"""

    @property
    def is_online(self) -> bool:
        return self is PresenceState.CONNECTED

    # 兼容旧调用点：以下三个属性现在都是"在线"的同一件事。
    @property
    def is_healthy(self) -> bool:
        return self.is_online

    @property
    def is_reachable(self) -> bool:
        return self.is_online

    @property
    def can_receive(self) -> bool:
        return self.is_online


@dataclass(frozen=True, slots=True)
class PresencePolicy:
    """租约参数：**只用于诊断，不参与在线判定**。

    保留它们是因为历史数据里仍有 ``lease_expires_at`` 列（便于排查），但
    ``compute_presence`` 不再读它：在线与否只由宿主进程决定。
    """

    heartbeat_interval_seconds: float = 20.0
    lease_seconds: float = 60.0
    grace_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds 必须为正数")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数")
        if self.grace_seconds < 0:
            raise ValueError("grace_seconds 不能为负数")

    @property
    def lease_plus_grace_seconds(self) -> float:
        return self.lease_seconds + self.grace_seconds

    @property
    def grace_delta(self) -> timedelta:
        return timedelta(seconds=self.grace_seconds)


def compute_presence(
    *,
    host_pid: int | None,
    closed: bool = False,
    alive: bool | None = None,
) -> PresenceState:
    """由事实推导在线状态：**托管进程活着 = 在线，否则离线**。

    ``alive`` 允许调用方注入"进程是否活着"这一事实（``None`` = 自己去探测）。做成参数
    而不是在函数里直接探进程，是为了让这条规则本身可测：测试可以明确声明"托管进程已
    退出"，而不用伪造一个真实进程。

    没有 ``host_pid`` 的连接一律判离线（宁可显示离线，也不要谎报在线——谎报会让发信方
    以为消息已送达，而它其实还躺在队列里）。
    """
    if closed:
        return PresenceState.OFFLINE
    if host_pid is None:
        return PresenceState.OFFLINE
    if alive is None:
        from .process import is_process_alive

        alive = is_process_alive(host_pid)
    return PresenceState.CONNECTED if alive else PresenceState.OFFLINE
