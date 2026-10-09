"""领域对象与查询行 -> 稳定 JSON DTO。

为什么单独一层：API 的形状不应该随仓储实现或领域对象字段变化而漂移。这里显式列出
每个响应包含哪些键，测试可以直接断言这套契约。

两条必须守住的规则：

1. **三个状态维度分开**：``delivery`` / ``visibility`` / ``processing`` 各自是独立
   字段，且带上人类可读说明，避免"delivered 被读成 completed"。
2. **诊断不夸大**：``capability_level=2`` 与 ``verified=true`` 是两个字段；
   降级原因必须显式给出，不能只靠颜色。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

__all__ = [
    "DELIVERY_EXPLANATIONS",
    "PRESENCE_EXPLANATIONS",
    "PROCESSING_EXPLANATIONS",
    "account_detail",
    "account_row",
    "connection_row",
    "conversation_detail",
    "conversation_row",
    "delivery_row",
    "message_row",
    "participant_row",
    "row_to_dict",
    "timestamp",
]

#: 状态说明文本：界面与 API 共用，保证"文字说明"和"状态值"永远一致。
DELIVERY_EXPLANATIONS = {
    "queued": "已入队，尚未派发（目标离线时停在这里）",
    "dispatched": "已派发，等待适配器确认",
    "delivered": "宿主已可靠接收——**不代表**模型已阅读或任务完成",
    "failed": "本次尝试失败，仍有重试机会",
    "dead_letter": "超过重试上限或不可重试，等待人工处理",
}

VISIBILITY_EXPLANATIONS = {
    "unread": "未读",
    "seen": "已读（只表示调用者推进了可见性）",
}

PROCESSING_EXPLANATIONS = {
    "pending": "尚未开始处理",
    "running": "处理中",
    "completed": "处理完成（回执，不等于已回复）",
    "blocked": "受阻",
    "cancelled": "已取消",
    "failed": "处理失败",
}

PRESENCE_EXPLANATIONS = {
    "connected": "在线：托管该会话的进程活着",
    "offline": "离线：托管进程不在，消息只会排队等它上线",
}

WAKE_BASIS_EXPLANATIONS = {
    "direct_channel": "邮箱侧有该宿主的直连注入通道：发信会直接把消息送进会话并启动回合",
    "declared_level2": "该账号自己声明了 Level 2：由适配器取走事件去注入（声明不等于已验证）",
    "no_channel": "在线但没有注入通道：消息留在队列里，等它下次调用工具取信",
    "offline": "离线：只存不发，绝不尝试唤醒或打开程序",
}

CAPABILITY_LEVEL_SLUGS = {0: "tools-only", 1: "notify", 2: "wake"}


def row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    """把 sqlite3.Row 转成普通 dict（None 原样返回）。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def timestamp(value: Any) -> str | None:
    """数据库文本时间戳原样透出（已是 ISO-8601 UTC）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# 账号与连接
# ---------------------------------------------------------------------------


def connection_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    """连接证据：代次、托管进程 PID、能力等级、适配器（租约/心跳仅留作诊断）。"""
    data = row_to_dict(row)
    if not data:
        return {}
    level = int(data.get("capability_level") or 0)
    return {
        "connection_id": _text(data.get("connection_id")),
        "account_id": _text(data.get("account_id")),
        "generation": int(data.get("generation") or 0),
        "state": _text(data.get("state")),
        "is_current": bool(data.get("is_current")),
        "durable": bool(data.get("durable")),
        "host_pid": (int(data["host_pid"]) if data.get("host_pid") is not None else None),
        "capability_level": level,
        "capability_slug": CAPABILITY_LEVEL_SLUGS.get(level, "tools-only"),
        "adapter_name": _text(data.get("adapter_name")),
        "opened_at": timestamp(data.get("opened_at")),
        "heartbeat_at": timestamp(data.get("heartbeat_at")),
        "lease_expires_at": timestamp(data.get("lease_expires_at")),
        "closed_at": timestamp(data.get("closed_at")),
    }


def account_row(
    row: sqlite3.Row | dict[str, Any],
    *,
    presence: str,
    connection: sqlite3.Row | dict[str, Any] | None = None,
    wake_channel: bool = False,
) -> dict[str, Any]:
    """账号列表/详情共用的 DTO。

    在线只看托管进程：``connected`` 表示进程活着（``online``）。
    ``can_wake`` 表示"能不能直接投进去让它开工"，依据写在 ``wake_basis``：

        direct_channel  邮箱侧有该宿主的直连注入通道（例如 DSH 本地接口）
        declared_level2 该账号自己声明了 Level 2（有适配器会取走事件去注入）
        no_channel      在线但没有注入通道：消息会排队等它自己取信
        offline         离线：只存不发

    ``verified`` 与该能力刻意分开：声明了不等于在本机验证过。
    """
    data = row_to_dict(row)
    connection_data = connection_row(connection)
    level = int(connection_data.get("capability_level") or data.get("capability_level") or 0)
    online = presence == "connected" and bool(connection_data)
    if not online:
        basis = "offline"
    elif wake_channel:
        basis = "direct_channel"
    elif level >= 2:
        basis = "declared_level2"
    else:
        basis = "no_channel"
    can_wake = basis in ("direct_channel", "declared_level2")
    return {
        "account_id": _text(data.get("account_id")),
        "display_name": _text(data.get("display_name")),
        "address": _text(data.get("address")),
        "host_type": _text(data.get("host_type")),
        "host_instance_id": _text(data.get("host_instance_id")),
        "native_session_id": _text(data.get("native_session_id")),
        "workspace_hint": _text(data.get("workspace_hint")),
        "blocked": bool(data.get("blocked")),
        "created_at": timestamp(data.get("created_at")),
        "updated_at": timestamp(data.get("updated_at")),
        "presence": presence,
        "online": online,
        "presence_explanation": PRESENCE_EXPLANATIONS.get(presence, ""),
        "connected": online,
        "capability_level": level,
        "capability_slug": CAPABILITY_LEVEL_SLUGS.get(level, "tools-only"),
        "can_wake": can_wake,
        "wake_basis": basis,
        "wake_basis_explanation": WAKE_BASIS_EXPLANATIONS.get(basis, ""),
        "current_connection": connection_data or None,
        "current_generation": int(data.get("current_generation") or 0),
    }


def account_detail(
    row: sqlite3.Row | dict[str, Any],
    *,
    presence: str,
    connection: sqlite3.Row | dict[str, Any] | None,
    connections: list[sqlite3.Row] | None = None,
    stats: dict[str, Any] | None = None,
    wake_channel: bool = False,
) -> dict[str, Any]:
    """账号详情：在列表 DTO 之上补充连接历史与统计。"""
    payload = account_row(
        row, presence=presence, connection=connection, wake_channel=wake_channel
    )
    payload["connections"] = [connection_row(item) for item in (connections or [])]
    payload["stats"] = dict(stats or {})
    return payload


# ---------------------------------------------------------------------------
# 对话、参与者、消息
# ---------------------------------------------------------------------------


def participant_row(
    row: sqlite3.Row | dict[str, Any], *, display_name: str | None = None
) -> dict[str, Any]:
    data = row_to_dict(row)
    return {
        "account_id": _text(data.get("account_id")),
        "display_name": display_name or _text(data.get("display_name")) or _text(data.get("account_id")),
        "role": _text(data.get("role")),
        "joined_at": timestamp(data.get("joined_at")),
        "unread_count": int(data.get("unread_count") or 0),
        "last_read_message_id": _text(data.get("last_read_message_id")),
    }


def conversation_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = row_to_dict(row)
    return {
        "conversation_id": _text(data.get("conversation_id")),
        "kind": _text(data.get("kind")),
        "participant_key": _text(data.get("participant_key")),
        "created_by": _text(data.get("created_by")),
        "created_at": timestamp(data.get("created_at")),
        "updated_at": timestamp(data.get("updated_at")),
        "closed_at": timestamp(data.get("closed_at")),
        "blocked": bool(data.get("blocked")),
        "blocked_reason": _text(data.get("blocked_reason")),
        "auto_turn_count": int(data.get("auto_turn_count") or 0),
        "last_message_at": timestamp(data.get("last_message_at")),
        "message_count": int(data.get("message_count") or 0),
        "unread_total": int(data.get("unread_total") or 0),
    }


def conversation_detail(
    row: sqlite3.Row | dict[str, Any],
    *,
    participants: list[dict[str, Any]],
    delivery_breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload = conversation_row(row)
    payload["participants"] = list(participants)
    payload["delivery_breakdown"] = {
        state: int(count) for state, count in (delivery_breakdown or {}).items()
    }
    return payload


def message_row(
    row: sqlite3.Row | dict[str, Any],
    *,
    delivery: str | None = None,
    sender_display_name: str | None = None,
    recipient_display_name: str | None = None,
) -> dict[str, Any]:
    """一条消息。

    ``delivery`` 来自该消息**对收件人**的那条投递记录；``visibility`` 与
    ``processing`` 也各自独立返回，并在同一响应里把三者摊平，方便界面并列展示。
    """
    data = row_to_dict(row)
    visibility = _text(data.get("visibility")) or "unread"
    processing = _text(data.get("processing")) or "pending"
    return {
        "message_id": _text(data.get("message_id")),
        "conversation_id": _text(data.get("conversation_id")),
        "reply_to": _text(data.get("reply_to")),
        "sender_account_id": _text(data.get("sender_account_id")),
        "sender_display_name": sender_display_name,
        "recipient_account_id": _text(data.get("recipient_account_id")),
        "recipient_display_name": recipient_display_name,
        "content_type": _text(data.get("content_type")),
        "content": _text(data.get("content")) or "",
        "created_at": timestamp(data.get("created_at")),
        "auto_generated": bool(data.get("auto_generated")),
        "delivery": delivery,
        "delivery_explanation": DELIVERY_EXPLANATIONS.get(delivery or "", ""),
        "visibility": visibility,
        "visibility_explanation": VISIBILITY_EXPLANATIONS.get(visibility, ""),
        "processing": processing,
        "processing_explanation": PROCESSING_EXPLANATIONS.get(processing, ""),
        "processing_result": _text(data.get("processing_result")),
    }


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def delivery_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = row_to_dict(row)
    state = _text(data.get("state")) or "queued"
    return {
        "delivery_id": _text(data.get("delivery_id")),
        "message_id": _text(data.get("message_id")),
        "conversation_id": _text(data.get("conversation_id")),
        "account_id": _text(data.get("account_id")),
        "state": state,
        "state_explanation": DELIVERY_EXPLANATIONS.get(state, ""),
        "attempt": int(data.get("attempt") or 0),
        "created_at": timestamp(data.get("created_at")),
        "updated_at": timestamp(data.get("updated_at")),
        "dispatched_at": timestamp(data.get("dispatched_at")),
        "acked_at": timestamp(data.get("acked_at")),
        "injected_at": timestamp(data.get("injected_at")),
        "notified_at": timestamp(data.get("notified_at")),
        "next_attempt_at": timestamp(data.get("next_attempt_at")),
        "last_error": _text(data.get("last_error")),
    }
