"""时钟端口。

时间必须可注入：租约过期、退避重试、速率限制窗口都需要在测试里"拨表"，
不允许业务代码直接调用 ``datetime.now()``。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """提供当前 UTC 时间。实现必须是单调、无副作用且线程安全的。"""

    def now(self) -> datetime:
        ...

    def advance(self, seconds: float) -> None:
        """把时钟向前推进（真实时钟不支持时抛 ``NotImplementedError``）。"""
        ...


class SystemClock:
    """真实系统时钟。``advance`` 不可用。"""

    __slots__ = ()

    def now(self) -> datetime:
        from ..domain.timestamps import utc_now

        return utc_now()

    def advance(self, seconds: float) -> None:
        raise NotImplementedError("真实时钟不能拨动；测试请使用 FrozenClock")

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FrozenClock:
    """测试用可控时钟。

    默认从固定时刻起步，保证测试断言可写死时间文本；``advance`` 只影响本实例。
    """

    __slots__ = ("_now",)

    def __init__(self, start: datetime | None = None) -> None:
        from ..domain.timestamps import utc_now

        self._now = start or utc_now()

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self._now = moment
