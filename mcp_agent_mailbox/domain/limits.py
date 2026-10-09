"""字段长度上限与校验。

**为什么单独开一个模块**：消息正文早有上限（`domain.messages.MAX_MESSAGE_CHARS = 4000`），
但身份类字段一直没有——红队实测：

    connect_mailbox(account_name="名" * 1MiB)
      -> 成功，库 +24.3MB（accounts 载荷 2.1MB + 索引 1.05MB + 审计 detail 1.05MB）
      -> 之后 list_contacts(limit=500) 返回 6,292,675 字节，直接灌进模型上下文

也就是说：**一次工具调用就能把数据库和模型上下文一起打爆**。这里的常量给身份类字段
定上限，并在入库前明确拒绝（**不截断**——截断会静默改变身份，比报错危险得多）。
"""

from __future__ import annotations

from .errors import ValidationError

__all__ = [
    "MAX_ACCOUNT_NAME_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_SESSION_ID_CHARS",
    "MAX_WORKSPACE_HINT_CHARS",
    "normalize_field",
]

#: 原生会话 ID。宿主生成的 ID 通常 < 100 字符，256 足够宽松。
MAX_SESSION_ID_CHARS = 256

#: 显示名 / 账号名。要进 accounts 表并出现在审计 detail 与工具返回值里，不宜过大。
MAX_ACCOUNT_NAME_CHARS = 256

#: 工作目录提示。路径不可能这么长，256 已远超 Windows 的 260 上限量级。
MAX_WORKSPACE_HINT_CHARS = 256

#: 幂等键。它是**索引列**（ux_messages_idempotency），过大既撑库又撑 B 树。
MAX_IDEMPOTENCY_KEY_CHARS = 256


def normalize_field(
    value: str | None,
    *,
    name: str,
    max_chars: int,
    required: bool = False,
) -> str | None:
    """校验并规范化一个身份类字段。

    规则：
        - 必须是字符串（``None`` 只在非必填时允许）；
        - 去首尾空白；必填字段去空白后不能为空；
        - 超过 ``max_chars`` **明确拒绝，不截断**。

    返回规范化后的值（非必填且为空时返回 ``None``）。
    """
    if value is None:
        if required:
            raise ValidationError(f"{name} 不能为空")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} 必须是文本")
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValidationError(f"{name} 不能为空")
        return None
    if len(normalized) > max_chars:
        raise ValidationError(
            f"{name} 超过上限 {max_chars} 字（实际 {len(normalized)} 字）；"
            "内容不会被截断，请改用更短的值"
        )
    return normalized
