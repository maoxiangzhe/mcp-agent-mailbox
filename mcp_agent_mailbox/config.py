"""集中配置。

为什么要有这一层：设计文档明确要求心跳间隔、租约期限、断线宽限、消息长度、速率
限制、自动互聊轮次等参数**必须可配置并可测试，不能散落为魔法数字**。因此所有可调
参数都在这一个冻结数据类里，从环境变量与显式参数装载，测试可以整体替换。

环境变量前缀统一为 ``MAILBOX_``；数据目录沿用旧版的 ``BOARD_MCP_ROOT`` 以保留兼容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .domain.messages import MAX_MESSAGE_CHARS
from .domain.presence import PresencePolicy

__all__ = ["Settings", "load_settings", "DATA_DIR_ENV", "LEGACY_DATA_DIR_ENV"]

DATA_DIR_ENV = "MAILBOX_HOME"
LEGACY_DATA_DIR_ENV = "BOARD_MCP_ROOT"  # 兼容旧版安装，不改动既有 data 目录
PROJECT_ENV = "BOARD_MCP_PROJECT"

_DEFAULT_DB_NAME = "mailbox.sqlite3"


@dataclass(frozen=True, slots=True)
class RateLimits:
    """速率限制与循环控制。

    默认值是保守的：宁可少自动唤醒一次，也不要让两个智能体无限互聊。
    """

    #: 单个对话内连续自动往返的硬上限（设计文档 §11 建议默认 8）。
    max_auto_turns_per_conversation: int = 8
    #: 每小时每账号允许的自动唤醒次数。
    max_auto_wakes_per_hour: int = 120
    #: 同一对话内相同正文哈希连续出现多少次即判定为循环。
    repeated_content_limit: int = 3
    #: 循环检测的时间窗（秒）：只在最近这段时间内统计重复正文。
    repeated_content_window_seconds: int = 300
    #: 单账号每分钟最多发送多少条消息。
    max_sends_per_minute: int = 30
    #: 单对话每分钟最多多少条消息（两个账号合计）。
    max_messages_per_conversation_per_minute: int = 60
    #: 触发限流时建议的等待时间。
    retry_after_ms: int = 60_000

    @property
    def rate_window_seconds(self) -> int:
        return 60

    @property
    def auto_wake_window_seconds(self) -> int:
        return 3600


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """投递与重试策略。"""

    #: 单条投递最多尝试几次，超过进入死信。
    max_attempts: int = 5
    #: 首次重试退避（毫秒），按指数增长。
    base_backoff_ms: int = 2_000
    #: 退避上限（毫秒）。
    max_backoff_ms: int = 5 * 60_000
    #: 单次派发批量上限。
    dispatch_batch_size: int = 100
    #: 只能"通知"的通道（Level 0/1）重试间隔（秒）。
    #: 比二进制退避长得多，因为它不可能变成送达，重试只是为了等对端主动取信。
    notify_retry_seconds: int = 60

    def backoff_ms(self, attempt: int) -> int:
        """第 ``attempt`` 次失败后的退避毫秒数（指数退避，带上限）。"""
        exponent = max(attempt - 1, 0)
        value = self.base_backoff_ms * (2**exponent)
        return min(value, self.max_backoff_ms)


@dataclass(frozen=True, slots=True)
class Settings:
    """全部可调参数。"""

    data_dir: Path
    database_path: Path
    presence: PresencePolicy = field(default_factory=PresencePolicy)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    delivery: DeliveryPolicy = field(default_factory=DeliveryPolicy)
    max_message_chars: int = MAX_MESSAGE_CHARS
    #: 全局暂停：为真时不再自动唤醒任何会话（消息仍然持久化，可手动读）。
    global_pause: bool = False
    #: 日志级别。
    log_level: str = "INFO"
    #: 是否允许旧版不安全工具（自由填写发送者）。默认关闭。
    legacy_tools_enabled: bool = False
    #: 是否记录消息正文到调试日志。默认关闭；开启需配合保留期限。
    log_message_content: bool = False

    def with_overrides(self, **changes) -> "Settings":
        """返回替换了部分字段的新配置（便于测试局部调参）。"""
        return replace(self, **changes)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字，实际为 {raw!r}") from exc


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """数据目录解析顺序：显式参数 -> ``MAILBOX_HOME`` -> 旧版 ``BOARD_MCP_ROOT`` -> 默认。

    保留 ``BOARD_MCP_ROOT`` 是为了让已有安装不换目录就能升级；新用户推荐用
    ``MAILBOX_HOME``。
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    for name in (DATA_DIR_ENV, LEGACY_DATA_DIR_ENV):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return Path(raw).expanduser()
    return Path.home() / ".board-mcp"


def load_settings(
    *,
    data_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    **overrides,
) -> Settings:
    """从环境变量装载配置，允许显式覆盖。"""
    root = resolve_data_dir(data_dir)
    db_path = Path(database_path) if database_path is not None else root / _DEFAULT_DB_NAME

    presence = PresencePolicy(
        heartbeat_interval_seconds=_env_float("MAILBOX_HEARTBEAT_SECONDS", 20.0),
        lease_seconds=_env_float("MAILBOX_LEASE_SECONDS", 60.0),
        grace_seconds=_env_float("MAILBOX_GRACE_SECONDS", 10.0),
    )
    rate_limits = RateLimits(
        max_auto_turns_per_conversation=_env_int("MAILBOX_MAX_AUTO_TURNS", 8),
        max_auto_wakes_per_hour=_env_int("MAILBOX_MAX_AUTO_WAKES_PER_HOUR", 120),
        repeated_content_limit=_env_int("MAILBOX_REPEATED_CONTENT_LIMIT", 3),
        repeated_content_window_seconds=_env_int("MAILBOX_REPEATED_CONTENT_WINDOW_SECONDS", 300),
        max_sends_per_minute=_env_int("MAILBOX_MAX_SENDS_PER_MINUTE", 30),
        max_messages_per_conversation_per_minute=_env_int(
            "MAILBOX_MAX_MESSAGES_PER_CONVERSATION_PER_MINUTE", 60
        ),
    )
    delivery = DeliveryPolicy(
        max_attempts=_env_int("MAILBOX_MAX_DELIVERY_ATTEMPTS", 5),
        base_backoff_ms=_env_int("MAILBOX_DELIVERY_BACKOFF_MS", 2_000),
        max_backoff_ms=_env_int("MAILBOX_DELIVERY_MAX_BACKOFF_MS", 5 * 60_000),
        dispatch_batch_size=_env_int("MAILBOX_DISPATCH_BATCH_SIZE", 100),
    )
    settings = Settings(
        data_dir=root,
        database_path=db_path,
        presence=presence,
        rate_limits=rate_limits,
        delivery=delivery,
        max_message_chars=_env_int("MAILBOX_MAX_MESSAGE_CHARS", MAX_MESSAGE_CHARS),
        global_pause=_env_flag("MAILBOX_GLOBAL_PAUSE", False),
        log_level=os.environ.get("MAILBOX_LOG_LEVEL", "INFO").upper(),
        legacy_tools_enabled=_env_flag("MAILBOX_LEGACY_TOOLS", False),
        log_message_content=_env_flag("MAILBOX_LOG_MESSAGE_CONTENT", False),
    )
    return replace(settings, **overrides) if overrides else settings
