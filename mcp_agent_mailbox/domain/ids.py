"""不可猜测、可按时间排序的外部标识符。

为什么不用自增整数：账号、消息、投递的标识会出现在工具返回值、日志和适配器事件里。
自增整数会泄露总量并让调用方误以为可以推算相邻对象；纯随机 UUID 又不能按时间排序，
不利于游标分页。这里用 RFC 9562 的 UUIDv7 布局（48 位毫秒时间戳 + 版本/变体位 + 随机），
再按对象类型加短前缀，既能按创建顺序排序，也不依赖任何第三方库。

标识符只在领域层生成，仓储不得自行拼造。
"""

from __future__ import annotations

import secrets
import time

__all__ = [
    "ACCOUNT_PREFIX",
    "CONVERSATION_PREFIX",
    "CONNECTION_PREFIX",
    "DELIVERY_PREFIX",
    "MESSAGE_PREFIX",
    "new_account_id",
    "new_connection_id",
    "new_conversation_id",
    "new_delivery_id",
    "new_message_id",
]

ACCOUNT_PREFIX = "acc"
CONNECTION_PREFIX = "con"
CONVERSATION_PREFIX = "conv"
MESSAGE_PREFIX = "msg"
DELIVERY_PREFIX = "del"

_UUID7_RANDOM_BITS = 74
_UUID7_RANDOM_BYTES = 10
_UUID7_VERSION = 0x7
_UUID7_VARIANT = 0b10


def _uuid7_hex() -> str:
    """返回 32 位十六进制的 UUIDv7 文本（不含连字符）。"""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(_UUID7_RANDOM_BITS)
    value = (timestamp_ms << 80) | (_UUID7_VERSION << 76) | (random_bits >> 62 << 64)
    value |= (_UUID7_VARIANT << 62) | (random_bits & ((1 << 62) - 1))
    return f"{value:032x}"


def _new(prefix: str) -> str:
    return f"{prefix}_{_uuid7_hex()}"


def new_account_id() -> str:
    """新账号标识。账号是稳定身份，重连必须复用，不得重新生成。"""
    return _new(ACCOUNT_PREFIX)


def new_connection_id() -> str:
    """新连接标识。每次连接都是临时实例，不复用。"""
    return _new(CONNECTION_PREFIX)


def new_conversation_id() -> str:
    """新对话标识。"""
    return _new(CONVERSATION_PREFIX)


def new_message_id() -> str:
    """新消息标识。"""
    return _new(MESSAGE_PREFIX)


def new_delivery_id() -> str:
    """新投递标识。至少一次投递靠它去重，重试时必须复用同一个值。"""
    return _new(DELIVERY_PREFIX)
