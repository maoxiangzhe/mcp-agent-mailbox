"""结构化、可脱敏的观测能力。

两条硬要求（设计文档 §10、§14）：

1. **日志不得记录凭据、Token 或完整敏感正文。** 因此这里的 ``redact`` 是默认行为，
   不是可选项：字段名命中敏感模式一律替换为 ``***``，长文本一律只留长度与哈希前缀。
2. 事件用 ID 关联（account_id / conversation_id / message_id / delivery_id），
   而不是把正文塞进日志。

同时提供一个进程内的事件计数器，用于 ``mailbox status`` 诊断：不引入 Prometheus
这类重型依赖，诊断命令读它就够了。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime
from typing import Any, Mapping

__all__ = [
    "CounterRegistry",
    "METRIC_COUNTER",
    "audit_event_types",
    "configure_logging",
    "get_logger",
    "redact",
    "setup_diagnostic_logging",
]

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass(word|wd)?|secret|token|credential|cookie|authorization|auth|api[_-]?key|"
    r"private[_-]?key|session[_-]?key|bearer|signature)",
    re.IGNORECASE,
)

#: 超过这个长度的文本一律不进日志，只留长度与哈希前缀。
_MAX_LOGGED_TEXT = 120

_REDACTED = "***"
_counter_lock = threading.Lock()
METRIC_COUNTER: "CounterRegistry"


class CounterRegistry:
    """进程内计数器。线程安全，只增不减。"""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[name] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


METRIC_COUNTER = CounterRegistry()


def redact(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """递归脱敏：敏感字段名整体替换，超长文本只留摘要。

    实现对 ``Mapping`` / ``Sequence`` 递归，其余类型原样返回；超过 6 层就不再深入，
    避免恶意嵌套结构把日志路径拖成无界递归。
    """
    if depth > 6:
        return "<depth-limit>"
    if key is not None and _SENSITIVE_KEY_PATTERN.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _summarize_text(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return f"<{type(value).__name__}>"


def _summarize_text(text: str) -> str:
    if len(text) <= _MAX_LOGGED_TEXT:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"<text len={len(text)} sha256={digest}>"


class _JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象，便于机器解析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, Mapping):
            payload.update(redact(dict(extra)))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *,
    level: str = "INFO",
    log_dir=None,
    console: bool = False,
    filename: str = "broker.log",
) -> logging.Logger:
    """配置根日志：结构化 JSON 写文件，可选控制台输出。

    重复调用不会叠加 handler（幂等），因此可以在 CLI、Broker、测试里各自调用。
    """
    logger = logging.getLogger("mcp_agent_mailbox")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_mailbox_managed", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = _JsonFormatter()
    if log_dir is not None:
        from pathlib import Path

        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(directory / filename, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler._mailbox_managed = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)

    if console or log_dir is None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler._mailbox_managed = True  # type: ignore[attr-defined]
        logger.addHandler(stream_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger；父 logger 的 handler 决定输出位置。"""
    return logging.getLogger("mcp_agent_mailbox" if not name else f"mcp_agent_mailbox.{name}")


def setup_diagnostic_logging(settings) -> logging.Logger:
    """按配置初始化日志（数据目录下 ``logs/``）。"""
    return configure_logging(
        level=settings.log_level,
        log_dir=settings.data_dir / "logs",
        console=False,
    )


def audit_event_types() -> tuple[str, ...]:
    """可观测事件类型清单（设计文档 §14）。

    这份清单同时被文档测试使用：文档里列出的类型必须都能在这里找到，反之亦然。
    """
    return (
        "account.registered",
        "connection.opened",
        "connection.renewed",
        "connection.closed",
        "connection.superseded",
        "connection.recycled",
        "presence.changed",
        "conversation.created",
        "conversation.blocked",
        "conversation.paused",
        "message.created",
        "message.duplicate",
        "delivery.queued",
        "delivery.dispatched",
        "delivery.acknowledged",
        "delivery.failed",
        "delivery.retry_scheduled",
        "delivery.dead_letter",
        "delivery.cancelled",
        "wake.requested",
        "wake.started",
        "wake.failed",
        "wake.suppressed",
        "processing.changed",
        "reply.created",
        "rate.limited",
        "loop.detected",
        "legacy.tool_used",
    )
