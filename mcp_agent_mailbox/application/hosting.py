"""把邮箱消息装配成宿主可注入的形态。

外部消息进入宿主时的**来源信封**是安全边界的一部分（设计文档 §10）：宿主里的模型
必须能一眼看出这是来自另一个智能体账号的不可信内容，而信封里不允许出现任何权限、
沙箱或审批声明。这个模块是信封的唯一构造点，避免各处拼字符串拼出不同的格式。
"""

from __future__ import annotations

from ..domain.messages import DeliveryRecord, Message
from ..ports.host_adapter import ExternalEnvelope, render_envelope

__all__ = ["build_envelope", "render_injection_text"]


def build_envelope(
    *,
    message: Message,
    delivery: DeliveryRecord,
    sender_address: str,
) -> ExternalEnvelope:
    """构造来源信封。

    ``sender_address`` 由服务层从账号表解析后传入，**不接受调用方自定义**，
    因此模型无法伪造"来自谁"。
    """
    return ExternalEnvelope(
        from_address=sender_address,
        conversation_id=message.conversation_id,
        message_id=message.message_id,
        delivery_id=delivery.delivery_id,
        reply_to=message.reply_to,
        content_type=message.content_type.value,
    )


def render_injection_text(
    *,
    message: Message,
    delivery: DeliveryRecord,
    sender_address: str,
    body: str,
) -> str:
    """信封 + 正文的最终注入文本。"""
    return render_envelope(build_envelope(message=message, delivery=delivery, sender_address=sender_address), body)
