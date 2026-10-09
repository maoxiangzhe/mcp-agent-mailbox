"""时间工具。

全部时间戳统一为带时区的 UTC（``datetime.now(timezone.utc)``），落库时写成
ISO-8601 文本。跨平台与跨 SQLite 版本最稳的做法是显式写入带 ``+00:00`` 的文本，
读取时统一解析；不依赖 SQLite 的日期函数与本地时区。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "UTC",
    "ensure_aware",
    "format_timestamp",
    "parse_timestamp",
    "plus_seconds",
    "utc_now",
]

UTC = timezone.utc


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """把朴素时间按 UTC 解释，带时区的原样返回。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def format_timestamp(value: datetime) -> str:
    """序列化为 ISO-8601 文本，精确到微秒，固定 UTC 偏移。"""
    return ensure_aware(value).astimezone(UTC).isoformat(timespec="microseconds")


def parse_timestamp(text: str) -> datetime:
    """解析本模块写出的时间戳文本。

    只接受带时区的 ISO-8601；遇到朴素时间按 UTC 解释，遇到无法解析的文本抛
    ``ValueError``（调用方应把它当数据损坏，不要静默兜底）。
    """
    parsed = datetime.fromisoformat(text)
    return ensure_aware(parsed).astimezone(UTC)


def plus_seconds(value: datetime, seconds: float) -> datetime:
    """在给定时间上加秒数（内部统一走 UTC）。"""
    return ensure_aware(value) + timedelta(seconds=seconds)
