"""模型可见的邮箱工具。

设计约束（设计文档 §8，任务书"禁止"清单）：

- **没有任何工具接受发送者参数**：``from`` / ``agent`` / ``sender`` 一律不存在，
  发送者只能由连接上下文解析；
- 权限检查全部在**服务层**完成，这里只做参数校验与结果整形，一行 SQL 都没有；
- 返回稳定的结构化结果：``{"ok": ..., ...}``；失败时带 ``error``（机器可读 ``code``）
  与 ``recovery``（给人/模型看的下一步）；
- 明确区分"已送达"与"已完成"：``delivery_state`` 说明传输状态，并附提示文本。

工具函数全部是纯函数式包装（收 ``ToolRuntime``），便于在没有 MCP 的情况下单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..application.conversation_service import ConversationService, SendResult
from ..daemon.broker import Broker
from ..domain.errors import DomainError, IdentityMismatchError, NotFoundError
from ..domain.messages import MAX_MESSAGE_CHARS
from ..domain.limits import (
    MAX_ACCOUNT_NAME_CHARS,
    MAX_SESSION_ID_CHARS,
    MAX_WORKSPACE_HINT_CHARS,
    normalize_field,
)
from .connection_context import ENV_SESSION_ID, MailboxConnection

__all__ = ["ToolRuntime", "register_tools", "tool_error", "tool_ok"]

#: 送达语义提示。反复出现，是因为"delivered 被读成 completed"是这类系统最容易
#: 造成误判的地方，值得在每个发送结果里重复一次。
DELIVERY_NOTE = (
    "delivery_state=delivered 只表示目标宿主已可靠接收，不代表模型已阅读或任务完成；"
    "需要答复时用 reply_message（成功后自动确认原收件）；"
    "纯通知处理后用 set_message_status(completed)，无需发送确认消息。"
)

#: 未绑定时的统一提示。
_NOT_BOUND = (
    "本 MCP 连接尚未绑定邮箱账号。若当前宿主未自动传入会话身份，请从 Shell 读取"
    "当前会话 ID（Codex 使用 CODEX_SESSION_ID），再调用 "
    "connect_mailbox(session_id=..., account_name=...)。"
)


def tool_ok(**payload: Any) -> dict[str, Any]:
    """成功结果。"""
    result: dict[str, Any] = {"ok": True}
    result.update(payload)
    return result


def pending_notice(count: int) -> dict[str, Any]:
    """待取消息提示，附在每个工具结果上。

    为什么这是第一版可用性的关键：MCP 只有"客户端调用工具"这一个入口，服务器无法
    主动把消息塞进模型上下文。对 Level 0/1 宿主（不能唤醒），模型唯一能发现新消息的
    机会就是**每次调用工具时的返回值**。

    因此每个工具结果都带一个极小的计数提示，让会话在正常干活的过程中就能发现
    "有人给我发消息了"，然后主动 `mailbox_inbox` 取全文。

    刻意只放计数与指引，不放正文：
        - 正文走受身份约束的读取接口，通知通道不泄露消息内容；
        - 结果保持小体积，不污染模型的上下文预算。
    """
    return {
        "count": count,
        "hint": "有待处理收件：调用 mailbox_inbox；按内容决定是否回复，纯通知无需回复。",
    }


def tool_error(
    exc: Exception | str,
    *,
    code: str | None = None,
    recovery: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """失败结果。领域错误直接映射 ``code``；其他异常归为 ``internal_error``。"""
    if isinstance(exc, DomainError):
        message = exc.message
        resolved_code = code or exc.code
    else:
        message = str(exc)
        resolved_code = code or "internal_error"
    result: dict[str, Any] = {"ok": False, "error": resolved_code, "message": message}
    if recovery:
        result["recovery"] = recovery
    result.update(extra)
    return result


@dataclass(slots=True)
class ToolRuntime:
    """工具执行所需的一切：Broker 与当前连接绑定。"""

    broker: Broker
    connection: MailboxConnection
    host_type: str = "unknown"
    host_instance_id: str = "default"

    @property
    def conversations(self) -> ConversationService:
        return self.broker.conversations

    def ensure_bound(self) -> MailboxConnection:
        """确保已绑定；未绑定则尝试自动绑定，仍失败就抛领域错误。"""
        if self.connection.is_bound:
            return self.connection
        probe = self.connection.probe()
        if not probe.supported:
            from ..domain.errors import ValidationError

            raise ValidationError(f"{_NOT_BOUND} 探测结果：{probe.detail}")
        return self.connection.bind(self.broker.accounts)

    def require_connection_id(self) -> str:
        """取当前连接 ID。已回收/被取代的绑定会自动重新绑定一次。"""
        connection = self.ensure_bound()
        return connection.connection_id or ""

    def data_dir(self):
        return self.broker.settings.data_dir

    def pending_count(self) -> int:
        """当前账号未完成收件数；已读不会隐藏未完成收件。查询失败不影响主调用。"""
        account_id = self.connection.account_id
        if not account_id:
            return 0
        try:
            with self.broker.unit_of_work().transaction() as uow:
                return uow.messages.pending_count(account_id)
        except Exception:  # noqa: BLE001 - 提示信息不得影响主流程
            return 0


# ---------------------------------------------------------------------------
# 模型可见的八个工具
# ---------------------------------------------------------------------------


def whoami(runtime: ToolRuntime) -> dict[str, Any]:
    """返回当前账号、宿主、原生会话、能力等级与连接状态。

    这一层只做**异常兜底**：15 个模型可见工具里原先只有它没有 try/except，
    连接 id 损坏（``UnicodeEncodeError``）或数据库故障（``DatabaseError``）会直接冒到
    调用方，而其它工具在同一故障下都返回结构化 ``internal_error``。`whoami` 又是模型
    接入后第一个调用的工具，报错会让模型误判"工具坏了"。
    """
    try:
        return _whoami_impl(runtime)
    except DomainError as exc:
        return tool_error(exc, recovery=_recovery_for(exc))
    except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都转结构化错误，绝不冒泡
        return tool_error(
            exc,
            recovery="请重试；若持续失败，用 `cli doctor` 检查数据库与连接状态。",
        )


def _whoami_impl(runtime: ToolRuntime) -> dict[str, Any]:
    """返回当前账号、宿主、原生会话、能力等级与连接状态。

    设计文档 §5.1 要求"每个原生会话接入邮箱后自动注册账号"，所以这里在身份可用时
    会**自动完成绑定**；只有在拿不到原生会话身份时才返回 ``bound=False`` 与原因。
    这种"我还不知道我是谁"的查询本身应该可回答，报错反而让模型误以为工具坏了。
    """
    if not runtime.connection.is_bound:
        probe = runtime.connection.probe()
        if not probe.supported:
            return tool_ok(
                bound=False,
                account_id=None,
                database_path=str(runtime.broker.settings.database_path.resolve()),
                reason=probe.detail,
                capability_level=int(probe.level),
                capability_slug=probe.level.slug,
                can_wake=False,
                can_receive=False,
                recovery=(
                    "请从 Shell 读取当前会话 ID（Codex 使用 CODEX_SESSION_ID），"
                    "再调用 connect_mailbox(session_id=..., account_name=...)；"
                    f"宿主也可以直接注入 {ENV_SESSION_ID}。"
                ),
            )
        try:
            runtime.connection.bind(runtime.broker.accounts, reason="auto_on_whoami")
        except DomainError as exc:
            return tool_error(exc, recovery=_recovery_for(exc))

    connection_id = runtime.connection.connection_id or ""
    with runtime.broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(connection_id)
        account = uow.accounts.get(runtime.connection.account_id or "")
    if connection is None or account is None:
        runtime.connection.fail("绑定已失效")
        return tool_error(
            NotFoundError("连接或账号已不存在，请重新调用工具以重新绑定"),
            recovery="再次调用 whoami 会自动重新注册并绑定。",
        )
    presence = runtime.broker.presence.compute(
        account_id=account.account_id,
        connection=connection,
        host_type=account.host_type,
    )
    return tool_ok(
        bound=True,
        account_id=account.account_id,
        database_path=str(runtime.broker.settings.database_path.resolve()),
        address=account.address,
        display_name=account.display_name,
        host_type=account.host_type,
        host_instance_id=account.host_instance_id,
        native_session_id=account.native_session_id,
        workspace_hint=account.workspace_hint,
        connection_id=connection.connection_id,
        generation=connection.generation,
        is_current_connection=connection.is_current,
        session_created=runtime.connection.created,
        presence=presence.state.value,
        online=presence.is_online,
        capability_level=presence.capability_level,
        capability_slug=presence.capability_slug,
        can_receive=presence.can_receive,
        can_wake=presence.can_wake,
        wake_basis=presence.wake_basis,
        wake_channel=presence.wake_channel,
        wake_channels=_wake_channel_status(runtime),
        host_pid=connection.host_pid,
        host_process_alive=connection.host_process_alive(),
        presence_basis="托管进程是否活着（唯一依据；进程退出即离线）",
        note=(
            "presence=connected 表示托管进程活着、账号在线（可以直接投递）；"
            "offline 表示进程已退出，此时别人发来的消息仍然会被持久化，等你再上线时补投。"
        ),
    )


def _wake_channel_status(runtime: ToolRuntime) -> dict[str, Any] | None:
    """当前宿主注入通道的现状（B 优先 → A 兜底），让模型看清"为什么不能注入"。"""
    waker = getattr(runtime.broker.context, "waker", None)
    status = getattr(waker, "channel_status", None)
    if not callable(status):
        return None
    try:
        return status()
    except Exception:  # noqa: BLE001 - 诊断信息不该拖垮工具调用
        return None


def list_contacts(
    runtime: ToolRuntime,
    status: str | None = None,
    host_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """列出允许联系的会话账号，并按在线能力如实标注。"""
    try:
        connection_id = runtime.require_connection_id()
        contacts = runtime.conversations.list_contacts(
            connection_id=connection_id,
            status=status,
            host_type=host_type,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转结构化错误
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(
        contacts=[contact.to_dict() for contact in contacts],
        count=len(contacts),
        filtered_by={"status": status, "host_type": host_type},
        note=(
            "presence=connected 表示对方托管进程活着（在线，可以投递）；"
            "offline 表示对方进程不在——消息仍然发得出去，会排队等它上线再收。"
            "被自己加入阻止列表的账号不会出现在这里。"
        ),
    )


def start_conversation(
    runtime: ToolRuntime,
    to_account_id: str,
    text: str,
    idempotency_key: str | None = None,
    wait_for_reply: bool = False,
) -> dict[str, Any]:
    """创建一对一对话并发送首条消息。发送者来自连接上下文，不可指定。"""
    try:
        connection_id = runtime.require_connection_id()
        result = runtime.conversations.start_conversation(
            connection_id=connection_id,
            to_account_id=to_account_id,
            text=text,
            idempotency_key=idempotency_key,
            wait_for_reply=wait_for_reply,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return _send_payload(result)


def mailbox_inbox(
    runtime: ToolRuntime, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    """只读分页取未完成收件。已读消息仍可处理，终态回执不会重复返回。"""
    try:
        connection_id = runtime.require_connection_id()
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))

    try:
        size = max(1, min(int(limit or 20), 100))
        with runtime.broker.unit_of_work().transaction() as uow:
            caller = runtime.conversations.resolve_caller(uow, connection_id)
            page = uow.messages.list_pending_for_account(
                caller.account_id, cursor=cursor, limit=size
            )
            items: list[dict[str, Any]] = []
            for view in page.items:
                message = view.message
                sender = uow.accounts.get(message.sender_account_id)
                # 旧版本借 auto_generated 存期待回复；新消息显式记录在已有 metadata 中。
                expects_reply = message.metadata.get("expects_reply")
                items.append(
                    {
                        "conversation_id": message.conversation_id,
                        "message_id": message.message_id,
                        "from_account_id": message.sender_account_id,
                        "from_display_name": sender.display_name if sender else None,
                        "from_address": sender.address if sender else None,
                        "content": message.content,
                        "sent_at": message.created_at.isoformat(),
                        "reply_to": message.reply_to,
                        "delivery": view.delivery.value if view.delivery else None,
                        "visibility": view.visibility.value,
                        "processing": view.processing.value,
                        "processing_result": view.processing_result,
                        "expects_reply": expects_reply == "true" if expects_reply is not None else message.auto_generated,
                        "reply_with": {
                            "tool": "reply_message",
                            "args": {"message_id": message.message_id, "text": "<答复内容>"},
                        },
                        "mark_done_with": {
                            "tool": "set_message_status",
                            "args": {
                                "message_id": message.message_id,
                                "processing": "completed",
                                "result": "<可选处理摘要>",
                            },
                        },
                    }
                )
            pending_total = uow.messages.pending_count(caller.account_id)
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))

    return tool_ok(
        messages=items,
        count=len(items),
        pending_total=pending_total,
        next_cursor=page.next_cursor,
        work_order=(
            "先看 content，按消息内容决定是否回复；expects_reply 表示发件人期待答复。"
            "需要答复时用 reply_with，成功后自动确认原收件已处理。"
            "纯通知或无需答复的消息用 mark_done_with 确认即可，无需回复确认消息。"
        ),
        note=(
            "本工具只读：不会改变投递/已读/处理状态。"
            "pending_total 统计全部未完成收件，不受已读位点影响；"
            "用 next_cursor 翻页，已完成/取消/失败消息可在 read_conversation 查看。"
        ),
    )


def send_message(
    runtime: ToolRuntime,
    conversation_id: str,
    text: str,
    idempotency_key: str | None = None,
    wait_for_reply: bool = False,
) -> dict[str, Any]:
    """向已参与的对话发送新消息。"""
    try:
        connection_id = runtime.require_connection_id()
        result = runtime.conversations.send_message(
            connection_id=connection_id,
            conversation_id=conversation_id,
            text=text,
            idempotency_key=idempotency_key,
            wait_for_reply=wait_for_reply,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return _send_payload(result)


def reply_message(
    runtime: ToolRuntime,
    message_id: str,
    text: str,
    idempotency_key: str | None = None,
    wait_for_reply: bool = False,
) -> dict[str, Any]:
    """回复指定消息。收件人固定为原消息的发送者，不能借此转给第三方。"""
    try:
        connection_id = runtime.require_connection_id()
        result = runtime.conversations.reply_message(
            connection_id=connection_id,
            message_id=message_id,
            text=text,
            idempotency_key=idempotency_key,
            wait_for_reply=wait_for_reply,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return _send_payload(result)


def list_conversations(
    runtime: ToolRuntime,
    cursor: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """分页列出当前账号参与的对话。"""
    try:
        connection_id = runtime.require_connection_id()
        views, next_cursor = runtime.conversations.list_conversations(
            connection_id=connection_id,
            cursor=cursor,
            unread_only=unread_only,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(
        conversations=[view.to_dict() for view in views],
        count=len(views),
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


def read_conversation(
    runtime: ToolRuntime,
    conversation_id: str,
    after_message_id: str | None = None,
    limit: int = 50,
    mark_seen: bool = True,
) -> dict[str, Any]:
    """读取对话消息。

    ``mark_seen=True``（默认）会推进 visibility=seen 并清零未读；传 ``False`` 只读。
    """
    try:
        connection_id = runtime.require_connection_id()
        page = runtime.conversations.read_conversation(
            connection_id=connection_id,
            conversation_id=conversation_id,
            after_message_id=after_message_id,
            limit=limit,
            mark_seen=mark_seen,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(
        **page.to_dict(),
        has_more=page.next_cursor is not None,
        mark_seen_applied=mark_seen,
        note=(
            "每条消息带 delivery / visibility / processing 三个独立状态；"
            "delivered 不等于 completed。"
        ),
    )


def set_message_status(
    runtime: ToolRuntime,
    message_id: str,
    processing: str,
    result: str | None = None,
) -> dict[str, Any]:
    """回传处理状态（pending/running/completed/blocked/cancelled/failed）。"""
    try:
        connection_id = runtime.require_connection_id()
        record = runtime.conversations.set_message_status(
            connection_id=connection_id,
            message_id=message_id,
            processing=processing,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(
        record=record.to_dict(),
        note=(
            "processing=completed 是**处理回执**，与回复是两件事："
            "需要对方知道结论时请另外调用 reply_message。"
        ),
    )


# ---------------------------------------------------------------------------
# 适配器协议工具（供宿主适配器调用，不是模型流程的一部分）
# ---------------------------------------------------------------------------


def _declared_capability(runtime: ToolRuntime) -> int:
    """本进程**适配器已声明**的能力等级。

    取自连接上下文对宿主身份的探测结果，而不是调用方传参——能力等级描述的是"这个
    宿主能不能被唤醒"，只有适配器有权声明。
    """
    try:
        return int(runtime.connection.probe().level)
    except Exception:  # noqa: BLE001 - 探测失败按最低能力处理，绝不抬高
        return 0


def mailbox_register(
    runtime: ToolRuntime,
    host_type: str,
    host_instance_id: str,
    native_session_id: str,
    display_name: str | None = None,
    workspace_hint: str | None = None,
    capability_level: int = 0,
    adapter_name: str | None = None,
) -> dict[str, Any]:
    """为当前 MCP 连接显式注册会话身份。

    默认拒绝；宿主安装配置显式开启后，AI 会话可以读取自己 Shell 中的原生会话 ID
    （例如 Codex 的 ``CODEX_SESSION_ID``）并注册。后续发送者仍从当前连接上下文解析，
    发送工具不接受 ``from`` 参数。

    安全边界（本函数是模型唯一能**提交身份三元组**的入口，因此必须自己收紧）：

    * 只接受与本进程**自己**的 ``host_type`` / ``host_instance_id`` 一致的三元组。
      账号唯一键是 ``(host_type, host_instance_id, native_session_id)``，如果允许模型
      自带前两个分量，任何会话都能用它拼出**别人**的键、拿到对方的 ``account_id``
      （实测可改名、可把对方能力等级永久抬到 2、可让对方连接失效），这属于账号冒用。
    * ``capability_level`` 以本进程适配器声明的等级为上限。能力等级必须诚实：调用方
      传得再高也不会被记录，"未验证却显示可唤醒"正是本项目明令禁止的。
    """
    # 授权判定**必须先于**身份判定：本进程没开放自注册时，"未允许显式注册"才是
    # 正确且更有用的答复（既有测试与错误契约都依赖这个优先级）。身份边界只在
    # 允许自注册的前提下才有讨论意义。
    if not runtime.connection.allow_model_registration:
        from ..domain.errors import ValidationError

        return tool_error(
            ValidationError(
                "本进程未允许显式注册会话；会话身份必须由适配器在启动时通过环境变量"
                f"（{ENV_SESSION_ID}）注入。"
            ),
            recovery=(
                "请改为让适配器在启动本进程时注入 "
                f"{ENV_SESSION_ID}/MAILBOX_HOST_TYPE/MAILBOX_HOST_INSTANCE_ID，"
                "或显式以 --allow-adapter-registration 启动邮箱服务。"
            ),
        )
    self_key = (runtime.host_type, runtime.host_instance_id)
    claimed_key = (host_type, host_instance_id)
    if claimed_key != self_key:
        from ..domain.errors import ValidationError

        return tool_error(
            ValidationError(
                "身份三元组的前两个分量必须与本进程一致："
                f"本进程是 {self_key!r}，收到 {claimed_key!r}。"
            ),
            recovery=(
                "host_type / host_instance_id 由适配器在启动本进程时注入，不能由模型指定。"
                "会话只能提供自己的原生会话 ID。"
            ),
        )
    declared = _declared_capability(runtime)
    try:
        # 钳制而不是拒绝：适配器声明的等级是上限，调用方传超了也不会被记录（能力必须诚实），
        # 但不该因此把一次合法注册整体打回。
        # 注意：`int()` 必须留在 try 里——红队实测 capability_level="abc" 会让
        # ValueError 逃出工具函数（本文件自称供"没有 MCP 的单测"使用，进程内调用者会命中）。
        effective_level = max(0, min(int(capability_level), declared))
        native_session_id = normalize_field(
            native_session_id,
            name="native_session_id",
            max_chars=MAX_SESSION_ID_CHARS,
            required=True,
        )
        connection = runtime.connection.submit_adapter_registration(
            runtime.broker.accounts,
            host_type=host_type,
            host_instance_id=host_instance_id,
            native_session_id=native_session_id,
            display_name=display_name,
            workspace_hint=workspace_hint,
            capability_level=effective_level,
            adapter_name=adapter_name,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(
            exc,
            recovery=(
                "请改为让适配器在启动本进程时注入 "
                f"{ENV_SESSION_ID}/MAILBOX_HOST_TYPE/MAILBOX_HOST_INSTANCE_ID，"
                "或显式以 --allow-adapter-registration 启动邮箱服务。"
            ),
        )
    return tool_ok(
        account_id=connection.account_id,
        connection_id=connection.connection_id,
        generation=connection.generation,
        created=connection.created,
        descriptor=connection.descriptor.to_dict() if connection.descriptor else None,
    )


def connect_mailbox(
    runtime: ToolRuntime,
    session_id: str,
    account_name: str,
    workspace_hint: str | None = None,
) -> dict[str, Any]:
    """把当前 AI 会话连接到自己的邮箱账号。

    ``session_id`` 是会话从宿主 Shell 读取的稳定原生会话 ID；宿主类型与实例由
    MCP 服务配置决定，能力固定为 Level 0。相同会话 ID 重连时恢复同一账号。
    """
    if not runtime.connection.allow_model_registration:
        from ..domain.errors import ValidationError

        return tool_error(
            ValidationError("本邮箱服务未开放会话自注册。"),
            recovery="请用对应宿主的安装器重新安装邮箱 MCP 后重启宿主。",
        )
    try:
        # 字段上限：这些值会进 accounts 表、审计 detail 与工具返回值。不设限时
        # 一次调用就能把库和模型上下文一起打爆（红队实测 1MiB 名字 -> 库 +24.3MB）。
        session_id = normalize_field(
            session_id, name="session_id", max_chars=MAX_SESSION_ID_CHARS, required=True
        )
        account_name = normalize_field(
            account_name, name="account_name", max_chars=MAX_ACCOUNT_NAME_CHARS, required=True
        )
        workspace_hint = normalize_field(
            workspace_hint, name="workspace_hint", max_chars=MAX_WORKSPACE_HINT_CHARS
        )
        connection = runtime.connection.submit_adapter_registration(
            runtime.broker.accounts,
            host_type=runtime.host_type,
            host_instance_id=runtime.host_instance_id,
            native_session_id=session_id,
            display_name=account_name,
            workspace_hint=workspace_hint,
            capability_level=0,
            adapter_name=f"{runtime.host_type}-self-connect",
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery="确认 session_id 与 account_name 非空后重试。")
    descriptor = connection.descriptor
    return tool_ok(
        account_id=connection.account_id,
        connection_id=connection.connection_id,
        generation=connection.generation,
        created=connection.created,
        display_name=descriptor.display_name if descriptor else account_name,
        native_session_id=descriptor.native_session_id if descriptor else session_id,
        workspace_hint=descriptor.workspace_hint if descriptor else workspace_hint,
        capability_level=0,
        capability_slug="tools-only",
        note="当前连接已绑定；后续发送者由连接自动确定。",
    )


def mailbox_heartbeat(runtime: ToolRuntime) -> dict[str, Any]:
    """续租心跳（适配器协议）。"""
    try:
        connection_id = runtime.require_connection_id()
        connection = runtime.broker.accounts.renew(connection_id)
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery="连接可能已被回收；请重新注册。")
    return tool_ok(
        connection_id=connection.connection_id,
        generation=connection.generation,
        lease_expires_at=connection.lease_expires_at.isoformat(),
    )


def mailbox_fetch_delivery(runtime: ToolRuntime, delivery_id: str) -> dict[str, Any]:
    """按投递 ID 取完整消息（受身份约束）。事件里只有路由信息，正文必须走这里。"""
    try:
        connection_id = runtime.require_connection_id()
        payload = runtime.broker.deliveries.fetch_for_delivery(
            connection_id=connection_id, delivery_id=delivery_id
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(**payload)


def mailbox_ack_delivery(runtime: ToolRuntime, delivery_id: str) -> dict[str, Any]:
    """确认宿主已可靠接收（适配器协议）。**不代表任务完成。**"""
    try:
        connection_id = runtime.require_connection_id()
        delivery = runtime.broker.deliveries.acknowledge(
            connection_id=connection_id, delivery_id=delivery_id
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(delivery=delivery.to_dict(), note=DELIVERY_NOTE)


def mailbox_fail_delivery(
    runtime: ToolRuntime, delivery_id: str, reason: str, retryable: bool = True
) -> dict[str, Any]:
    """报告投递失败（适配器协议）。不可重试或超出上限将进入死信。"""
    try:
        connection_id = runtime.require_connection_id()
        delivery = runtime.broker.deliveries.fail(
            connection_id=connection_id,
            delivery_id=delivery_id,
            reason=reason,
            retryable=retryable,
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, recovery=_recovery_for(exc))
    return tool_ok(
        delivery=delivery.to_dict(),
        note="重试会复用同一个 delivery_id，接收方应按它去重，避免重复注入。",
    )


# ---------------------------------------------------------------------------
# 注册到 FastMCP
# ---------------------------------------------------------------------------


def register_tools(mcp, runtime_factory: Callable[[], ToolRuntime], *, include_adapter_tools: bool = True) -> None:
    """把工具注册到 FastMCP 实例。

    ``runtime_factory`` 每次调用返回运行时（同一进程内共享绑定状态）。
    工具函数本身不做 I/O 之外的事，所有领域逻辑都在服务层。

    每个工具的成功结果都会附加 ``pending`` 提示：MCP 无法主动推送，模型只能在
    调用工具的返回值里发现"有新消息"。这是 Level 0/1 宿主唯一可靠的收信信号。
    """

    def finish(runtime: ToolRuntime, payload: dict) -> dict:
        """统一的收尾：给成功结果附上未完成收件计数。"""
        if payload.get("ok"):
            payload.setdefault("pending", pending_notice(runtime.pending_count()))
        return payload

    @mcp.tool(name="whoami")
    def _whoami() -> dict:
        """查看当前邮箱账号、宿主会话、能力等级与连接状态。

        不接收任何身份参数：账号由当前 MCP 连接绑定决定。
        返回 `pending.count` 表示还有多少条未完成收件。
        """
        runtime = runtime_factory()
        return finish(runtime, whoami(runtime))

    @mcp.tool(name="connect_mailbox")
    def _connect_mailbox(
        session_id: str,
        account_name: str,
        workspace_hint: str | None = None,
    ) -> dict:
        """【首次接入】把当前 AI 会话连接到一个邮箱账号。

        当 `whoami.bound=false` 时，先从 Shell 读取当前会话 ID：Codex 使用
        `CODEX_SESSION_ID`；再调用本工具，例如
        `connect_mailbox(session_id=..., account_name="codex-A")`。相同 session_id
        会恢复同一邮箱账号；account_name 是联系人列表中显示的名称。
        """
        runtime = runtime_factory()
        return finish(
            runtime,
            connect_mailbox(
                runtime,
                session_id=session_id,
                account_name=account_name,
                workspace_hint=workspace_hint,
            ),
        )

    @mcp.tool(name="list_contacts")
    def _list_contacts(
        status: str | None = None, host_type: str | None = None, limit: int = 100
    ) -> dict:
        """列出可联系的会话账号（可按在线状态或宿主类型过滤）。

        在线状态只有两态：connected（托管进程活着）/ offline（进程不在）。
        offline 的账号照样可以发信：消息会排队，等它上线再收。
        """
        runtime = runtime_factory()
        return finish(
            runtime, list_contacts(runtime, status=status, host_type=host_type, limit=limit)
        )

    @mcp.tool(name="start_conversation")
    def _start_conversation(
        to_account_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> dict:
        """与指定账号创建一对一对话并发送首条消息。

        发送者恒为当前连接绑定的账号，工具不接受发送者参数。
        idempotency_key 相同则返回原消息，不会重复发送；重试请复用它。
        wait_for_reply=True 表示"我期待回复"（会计入自动往返上限）。
        """
        runtime = runtime_factory()
        return finish(
            runtime,
            start_conversation(
                runtime,
                to_account_id=to_account_id,
                text=text,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
            ),
        )

    @mcp.tool(name="mailbox_inbox")
    def _mailbox_inbox(limit: int = 20, cursor: str | None = None) -> dict:
        """分页取未完成收件；可传上次 next_cursor。已读不等于已处理。

        返回每条消息的 conversation_id / message_id / 发件人 / 正文，以及
        按内容决定是否答复；纯通知用 mark_done_with 确认，无需回复。
        reply_with 成功后自动确认原收件。本工具只读，不改变任何状态。
        """
        runtime = runtime_factory()
        return finish(runtime, mailbox_inbox(runtime, limit=limit, cursor=cursor))

    @mcp.tool(name="send_message")
    def _send_message(
        conversation_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> dict:
        """向已参与的对话发送新消息。发送者来自连接上下文，不可指定。"""
        runtime = runtime_factory()
        return finish(
            runtime,
            send_message(
                runtime,
                conversation_id=conversation_id,
                text=text,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
            ),
        )

    @mcp.tool(name="reply_message")
    def _reply_message(
        message_id: str,
        text: str,
        idempotency_key: str | None = None,
        wait_for_reply: bool = False,
    ) -> dict:
        """回复指定消息（带 reply_to）。成功后同时确认原收件，无需再发确认回复。"""
        runtime = runtime_factory()
        return finish(
            runtime,
            reply_message(
                runtime,
                message_id=message_id,
                text=text,
                idempotency_key=idempotency_key,
                wait_for_reply=wait_for_reply,
            ),
        )

    @mcp.tool(name="list_conversations")
    def _list_conversations(
        cursor: str | None = None, unread_only: bool = False, limit: int = 50
    ) -> dict:
        """分页列出当前账号参与的对话（游标分页，顺序稳定）。

        想知道"有没有人给我发消息"时优先用 unread_only=True。
        """
        runtime = runtime_factory()
        return finish(
            runtime,
            list_conversations(
                runtime, cursor=cursor, unread_only=unread_only, limit=limit
            ),
        )

    @mcp.tool(name="read_conversation")
    def _read_conversation(
        conversation_id: str,
        after_message_id: str | None = None,
        limit: int = 50,
        mark_seen: bool = True,
    ) -> dict:
        """读取对话消息；mark_seen=False 为只读，不推进已读位点。

        只影响已读计数；未完成收件仍在 mailbox_inbox，pending.count 不因阅读下降。
        """
        runtime = runtime_factory()
        return finish(
            runtime,
            read_conversation(
                runtime,
                conversation_id=conversation_id,
                after_message_id=after_message_id,
                limit=limit,
                mark_seen=mark_seen,
            ),
        )

    @mcp.tool(name="set_message_status")
    def _set_message_status(
        message_id: str, processing: str, result: str | None = None
    ) -> dict:
        """回传处理状态；completed 是回执，不等于回复。

        需要对方知道结论时请另外调用 reply_message。
        """
        runtime = runtime_factory()
        return finish(
            runtime,
            set_message_status(
                runtime, message_id=message_id, processing=processing, result=result
            ),
        )

    if not include_adapter_tools:
        return

    @mcp.tool(name="mailbox_register")
    def _mailbox_register(
        host_type: str,
        host_instance_id: str,
        native_session_id: str,
        display_name: str | None = None,
        workspace_hint: str | None = None,
        capability_level: int = 0,
        adapter_name: str | None = None,
    ) -> dict:
        """【首次接入】把当前 MCP 连接注册为一个邮箱账号。

        默认关闭；以 ``--allow-adapter-registration`` 启动后允许调用。Codex 会话在
        ``whoami.bound=false`` 时，从自己的 Shell 读取 ``CODEX_SESSION_ID``，将它原样
        作为 ``native_session_id``，并自行设置 ``display_name``（如 codex-A）。注册后
        再调用 ``whoami`` 确认绑定。能力等级：0=tools-only，1=notify，2=wake；未验证
        实时唤醒时必须填 0。
        """
        return mailbox_register(
            runtime_factory(),
            host_type=host_type,
            host_instance_id=host_instance_id,
            native_session_id=native_session_id,
            display_name=display_name,
            workspace_hint=workspace_hint,
            capability_level=capability_level,
            adapter_name=adapter_name,
        )

    @mcp.tool(name="mailbox_heartbeat")
    def _mailbox_heartbeat() -> dict:
        """【宿主适配器协议】续租心跳，维持账号在线状态。"""
        return mailbox_heartbeat(runtime_factory())

    @mcp.tool(name="mailbox_fetch_delivery")
    def _mailbox_fetch_delivery(delivery_id: str) -> dict:
        """【宿主适配器协议】按投递 ID 取消息全文与来源信封（受身份约束）。"""
        return mailbox_fetch_delivery(runtime_factory(), delivery_id=delivery_id)

    @mcp.tool(name="mailbox_ack_delivery")
    def _mailbox_ack_delivery(delivery_id: str) -> dict:
        """【宿主适配器协议】确认宿主已可靠接收该投递。不代表任务完成。"""
        return mailbox_ack_delivery(runtime_factory(), delivery_id=delivery_id)

    @mcp.tool(name="mailbox_fail_delivery")
    def _mailbox_fail_delivery(delivery_id: str, reason: str, retryable: bool = True) -> dict:
        """【宿主适配器协议】报告投递失败；不可重试将进入死信。"""
        return mailbox_fail_delivery(
            runtime_factory(), delivery_id=delivery_id, reason=reason, retryable=retryable
        )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _send_payload(result: SendResult) -> dict[str, Any]:
    payload = result.to_dict()
    payload["ok"] = True
    payload["note"] = DELIVERY_NOTE
    return payload


def _recovery_for(exc: Exception) -> str:
    """把常见错误映射成可执行的下一步。"""
    if isinstance(exc, IdentityMismatchError):
        return "本连接已被同账号的新连接取代，或连接已回收；请重新调用工具重新绑定。"
    if isinstance(exc, NotFoundError):
        return "请用 list_contacts / list_conversations 确认标识是否正确。"
    code = getattr(exc, "code", "")
    return {
        "validation_error": (
            f"请检查参数（消息正文上限 {MAX_MESSAGE_CHARS} 字，超限不会被截断）。"
        ),
        "recipient_blocked": "对方或你已设置阻止策略；请先在邮箱侧解除后再发送。",
        "not_a_participant": "只能读写自己参与的对话；请先 start_conversation。",
        "loop_limit_exceeded": (
            "自动互聊已达上限，对话被阻塞且消息保留。需要继续时请人工调用邮箱接口解除阻塞。"
        ),
        "rate_limited": "已触发速率限制，请稍后重试；重试请复用同一个 idempotency_key。",
        "conversation_closed": "对话已关闭或被阻塞，请新建对话或等待人工恢复。",
        "invalid_transition": "状态迁移非法（例如 completed 不能退回 running）。",
    }.get(code, "请检查参数后重试；若重复出现请查看邮箱日志与 doctor 诊断。")
