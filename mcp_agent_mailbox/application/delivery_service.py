"""投递服务：入队、派发、确认、重试、死信。

语义要点：

- **在线严格参照进程**：目标进程活着才可能"直接投进去"；进程没了就只排队。
- **至少一次**：``delivery_id`` 是接收方的去重键。重试必须复用同一个
  ``delivery_id``，绝不重新生成；否则接收方无法去重，会出现重复注入。
- **不可重复派发**：派发前用 ``claim_for_dispatch`` 原子抢占（``queued/failed`` ->
  ``dispatched`` 且 ``attempt+1``）。抢占失败说明别人已经在投，本次必须放弃。
- **delivered ≠ 完成**：只有宿主确认已接收（适配器注入成功 / 宿主 accepted）才置
  ``delivered``；模型是否真的处理完由 processing 状态与回复表达。
- **离线只存不发**：目标离线时保持 ``queued``，**不尝试唤醒、不打开程序**，等它上线。
- **没有注入通道就保持 queued**：既不谎报 ``delivered``，也不置 ``failed``——
  ``failed`` 会被读成"投递失败、消息可能丢"，而它其实好好躺在邮箱里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from ..domain.errors import IdentityMismatchError, NotFoundError, ValidationError
from ..domain.ids import new_delivery_id
from ..domain.messages import DeliveryRecord, DeliveryState, Message, check_delivery_transition
from ..domain.presence import HostCapabilityLevel, PresenceState
from ..domain.timestamps import plus_seconds
from ..ports.event_transport import Event
from .context import ServiceContext

__all__ = ["DeliveryService", "DispatchReport"]

#: 注入时给目标会话的提示语：只给"去取信"的指引，正文仍由邮箱按权限给出。
WAKE_NOTICE = (
    "邮箱有新消息待取。请调用 MCP 工具 mailbox_inbox 取信；如有下一页请继续取。"
    "根据消息内容决定是否需要工作或回复。需要回复时用 reply_message；"
    "仅通知、确认或已处理的消息用 set_message_status 标记 completed 即可，"
    "无需再发送收到确认，避免来回回复。"
)


def _online_now(connection) -> bool:
    """这条连接此刻是否在线（只看托管进程）。"""
    from ..domain.presence import compute_presence

    return compute_presence(
        host_pid=connection.host_pid, closed=not connection.is_open
    ).is_online


@dataclass(frozen=True, slots=True)
class _Target:
    """一条投递的目标连接快照：派发决策只需要这几个事实。"""

    connection_id: str
    generation: int
    capability_level: HostCapabilityLevel
    state: PresenceState
    host_type: str
    native_session_id: str

    @property
    def is_online(self) -> bool:
        """在线 = 托管进程活着（唯一依据）。"""
        return self.state.is_online

    def injection_mode(self, waker: object | None) -> str | None:
        """这条投递能用哪种注入方式？

        * ``"direct"``：邮箱侧有对得上该宿主的直连通道 -> 立刻注入并确认；
        * ``"adapter"``：目标自己声明 Level 2 -> 发事件，等适配器注入后确认；
        * ``None``：没有注入通道 -> 消息保持 queued，等目标自己取信。

        离线目标一律返回 ``None``：**不会**去唤醒它、更不会去打开程序。
        """
        if not self.is_online:
            return None
        selector = getattr(waker, "for_host", None)
        if callable(selector):
            waker = selector(self.host_type)
        if waker is not None and getattr(waker, "host_type", None) == self.host_type:
            return "direct"
        if self.capability_level.can_wake_session:
            return "adapter"
        return None

    @property
    def can_notify(self) -> bool:
        """在线但只能通知（适配器有实时通道、不能启动回合）。"""
        return self.is_online and self.capability_level.can_receive_realtime


@dataclass(slots=True)
class DispatchReport:
    """一轮派发的统计。诊断命令与测试都读它，避免靠日志猜测。"""

    scanned: int = 0
    dispatched: int = 0
    notified_only: int = 0
    held_for_pickup: int = 0
    failed: int = 0
    errored: int = 0
    skipped_offline: int = 0
    skipped_raced: int = 0
    dead_lettered: int = 0
    details: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "dispatched": self.dispatched,
            "notified_only": self.notified_only,
            "held_for_pickup": self.held_for_pickup,
            "failed": self.failed,
            "errored": self.errored,
            "skipped_offline": self.skipped_offline,
            "skipped_raced": self.skipped_raced,
            "dead_lettered": self.dead_lettered,
            "details": self.details[:20],
        }


class DeliveryService:
    """投递与重试。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._settings = context.settings
        self._policy = context.settings.delivery
        self._clock = context.clock

    # -- 入队 -------------------------------------------------------------

    def enqueue(
        self,
        uow,
        *,
        message: Message,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        """在**调用方的事务内**为消息创建首条投递记录。

        刻意接收 ``uow`` 而不是自己开事务：设计文档要求"消息创建与首次投递入队
        位于同一事务"，把事务边界交给调用方是唯一能保证这一点的方式。
        """
        moment = now or self._clock.now()
        delivery = DeliveryRecord(
            delivery_id=new_delivery_id(),
            message_id=message.message_id,
            account_id=message.recipient_account_id,
            conversation_id=message.conversation_id,
            state=DeliveryState.QUEUED,
            attempt=0,
            created_at=moment,
            updated_at=moment,
            content_hash=message.content_hash,
        )
        uow.deliveries.add(delivery)
        self._context.audit(
            uow,
            "delivery.queued",
            now=moment,
            account_id=delivery.account_id,
            conversation_id=delivery.conversation_id,
            message_id=delivery.message_id,
            delivery_id=delivery.delivery_id,
            detail={},
        )
        return delivery

    def enqueue_many(self, uow, messages: list[Message], *, now: datetime | None = None) -> list[DeliveryRecord]:
        return [self.enqueue(uow, message=message, now=now) for message in messages]

    # -- 派发 -------------------------------------------------------------

    def dispatch_due(self, *, limit: int | None = None) -> DispatchReport:
        """派发所有到期投递。

        对每条投递：
            1. 目标离线（进程不在）-> 保持 ``queued``，计入 ``skipped_offline``，
               **不做任何唤醒/注入尝试**，也不消耗尝试次数；
            2. 目标在线且适配器能注入 -> 发 ``mailbox/message`` 事件并保持
               ``dispatched``，等适配器确认后才是 ``delivered``；
            3. 目标在线但适配器不能注入 -> 消息仍是 ``queued``（``held_for_pickup``
               或 ``notified_only``），**绝不**置 ``delivered``/``failed``：
               没送达就是没送达，但它好好地躺在邮箱里等目标取。
        """
        batch = limit or self._policy.dispatch_batch_size
        report = DispatchReport()
        if self._settings.global_pause:
            return report
        now = self._clock.now()

        with self._context.uow().transaction() as uow:
            due = uow.deliveries.due_for_dispatch(now=now, limit=batch)
            report.scanned = len(due)
            targets = self._targets_for(uow, due)

        direct_groups: dict[str, list[DeliveryRecord]] = {}
        for delivery in due:
            target = targets.get(delivery.account_id)
            if target is not None and target.injection_mode(self._context.waker) == "direct":
                direct_groups.setdefault(delivery.account_id, []).append(delivery)
        direct_outcomes: dict[str, str] = {}
        for delivery in due:
            # 一条投递出问题不能拖垮整批：队首毒丸会永远挡住后面的消息。
            try:
                if delivery.account_id in direct_groups:
                    if delivery.delivery_id not in direct_outcomes:
                        direct_outcomes.update(self._dispatch_direct_batch(
                            direct_groups[delivery.account_id], targets[delivery.account_id], now=now
                        ))
                    outcome = direct_outcomes[delivery.delivery_id]
                else:
                    outcome = self._dispatch_one(delivery, targets.get(delivery.account_id), now=now)
            except Exception:  # noqa: BLE001 - 单条失败只记录，继续派发后面的
                self._context.logger.exception(
                    "派发单条投递失败（继续处理后续投递）",
                    extra={
                        "event": "delivery.dispatch_failed",
                        "context": {"delivery_id": delivery.delivery_id},
                    },
                )
                outcome = "error"
                for item in direct_groups.get(delivery.account_id, []):
                    direct_outcomes.setdefault(item.delivery_id, "error")
            if outcome == "dispatched":
                report.dispatched += 1
            elif outcome == "notified":
                report.notified_only += 1
            elif outcome == "held":
                report.held_for_pickup += 1
            elif outcome == "failed":
                report.failed += 1
            elif outcome == "offline":
                report.skipped_offline += 1
            elif outcome == "raced":
                report.skipped_raced += 1
            elif outcome == "dead_letter":
                report.dead_lettered += 1
            elif outcome == "error":
                report.errored += 1
            if len(report.details) < 20:
                report.details.append(
                    {
                        "delivery_id": delivery.delivery_id,
                        "account_id": delivery.account_id,
                        "outcome": outcome,
                    }
                )
        return report

    def _dispatch_direct_batch(self, deliveries, target: _Target, *, now: datetime) -> dict[str, str]:
        """Claim a session's batch atomically, then send one pickup notice outside the transaction."""
        outcomes = {d.delivery_id: "raced" for d in deliveries}
        claimed = []
        with self._context.uow().transaction() as uow:
            current = uow.connections.current_for_account(deliveries[0].account_id)
            if current is None or current.connection_id != target.connection_id or not _online_now(current):
                return outcomes
            for delivery in deliveries:
                if uow.deliveries.claim_for_dispatch(delivery.delivery_id, at=now):
                    claimed.append(delivery)
                    self._context.audit(
                        uow, "delivery.dispatched", now=now,
                        account_id=delivery.account_id, conversation_id=delivery.conversation_id,
                        message_id=delivery.message_id, delivery_id=delivery.delivery_id,
                        detail={"mode": "direct", "generation": target.generation,
                                "attempt": delivery.attempt + 1},
                    )
        if not claimed:
            return outcomes
        waker = self._context.waker
        selector = getattr(waker, "for_host", None)
        if callable(selector):
            waker = selector(target.host_type)
        try:
            wake_outcome = waker.wake(target.native_session_id, WAKE_NOTICE)
        except Exception as exc:  # Channel errors must schedule retry for the whole claimed batch.
            wake_outcome = SimpleNamespace(started=False, unavailable=False, detail=str(exc))
        for delivery in claimed:
            outcomes[delivery.delivery_id] = self._finish_injection(
                target, wake_outcome, delivery_id=delivery.delivery_id, now=now
            )
        return outcomes

    def _targets_for(self, uow, deliveries) -> dict[str, _Target]:
        from ..domain.presence import compute_presence

        result: dict[str, _Target] = {}
        for account_id in {d.account_id for d in deliveries}:
            connection = uow.connections.current_for_account(account_id)
            if connection is None:
                continue
            account = uow.accounts.get(account_id)
            if account is None:
                continue
            # 在线与否只看托管进程：租约/心跳/能力等级都不参与。
            state = compute_presence(
                host_pid=connection.host_pid,
                closed=not connection.is_open,
            )
            result[account_id] = _Target(
                connection_id=connection.connection_id,
                generation=connection.generation,
                capability_level=connection.capability_level,
                state=state,
                host_type=account.host_type,
                native_session_id=account.native_session_id,
            )
        return result

    def _dispatch_one(
        self,
        delivery: DeliveryRecord,
        target: "_Target | None",
        *,
        now: datetime,
    ) -> str:
        # 目标离线（托管进程不在）：消息保持排队等它回来。**不尝试唤醒、不打开程序**，
        # 也不消耗尝试次数——离线不是错误，只是"还没到"。
        if target is None or not target.is_online:
            with self._context.uow().transaction() as uow:
                current = uow.connections.current_for_account(delivery.account_id)
                if current is not None and _online_now(current):
                    return "raced"
                uow.deliveries.defer_offline(
                    delivery.delivery_id, at=now,
                    until=plus_seconds(now, self._policy.notify_retry_seconds),
                )
            return "offline"

        publish = True
        waker = self._context.waker
        mode = target.injection_mode(waker)
        with self._context.uow().transaction() as uow:
            current = uow.connections.current_for_account(delivery.account_id)
            if current is None or current.connection_id != target.connection_id or not _online_now(current):
                return "raced"
            claimed = uow.deliveries.claim_for_dispatch(delivery.delivery_id, at=now)
            if not claimed:
                return "raced"
            record = uow.deliveries.get(delivery.delivery_id)
            if record is None:
                raise NotFoundError(f"投递不存在：{delivery.delivery_id}")

            if mode == "direct":
                # 有直连通道：先记 dispatched，真正的注入在事务外做
                # （HTTP 调用不能占着数据库事务）。
                self._context.audit(
                    uow,
                    "delivery.dispatched",
                    now=now,
                    account_id=record.account_id,
                    conversation_id=record.conversation_id,
                    message_id=record.message_id,
                    delivery_id=record.delivery_id,
                    detail={
                        "attempt": record.attempt,
                        "generation": target.generation,
                        "mode": "direct",
                    },
                )
                outcome = "dispatched"
                publish = False
            elif mode == "adapter":
                # 目标自己声明了 Level 2：发事件，等适配器注入后确认。
                self._context.audit(
                    uow,
                    "delivery.dispatched",
                    now=now,
                    account_id=record.account_id,
                    conversation_id=record.conversation_id,
                    message_id=record.message_id,
                    delivery_id=record.delivery_id,
                    detail={
                        "attempt": record.attempt,
                        "generation": target.generation,
                        "mode": "adapter",
                    },
                )
                outcome = "dispatched"
            else:
                # 在线但没有注入通道：消息**保持 queued**——它已经在邮箱里，目标取信即可。
                # 关键点：绝不置 delivered（没送达），也绝不置 failed
                # （failed 会被读成"投递失败/消息可能丢"，而它其实好好躺着）。
                check_delivery_transition(record.state, DeliveryState.QUEUED)
                record.state = DeliveryState.QUEUED
                record.attempt = max(record.attempt - 1, 0)
                if target.can_notify:
                    record.notified_at = now
                record.updated_at = now
                # 没有注入通道时按更长的间隔重扫，避免反复打扰同一个会话。
                record.next_attempt_at = plus_seconds(
                    now, self._policy.notify_retry_seconds
                )
                record.last_error = (
                    "目标宿主只能被通知，不能启动回合；消息保留待目标主动取信"
                    if target.can_notify
                    else "目标宿主不支持注入（不能直接开工）；消息保留待目标主动取信"
                )
                uow.deliveries.update(record)
                self._context.audit(
                    uow,
                    "wake.suppressed",
                    now=now,
                    account_id=record.account_id,
                    conversation_id=record.conversation_id,
                    message_id=record.message_id,
                    delivery_id=record.delivery_id,
                    detail={
                        "reason": "notify_only" if target.can_notify else "tools_only",
                        "generation": target.generation,
                        "state": record.state.value,
                    },
                )
                outcome = "notified" if target.can_notify else "held"
                publish = target.can_notify

        if mode == "direct":
            outcome = self._inject(target, waker, delivery_id=delivery.delivery_id, now=now)

        if publish:
            self._context.publish(
                Event(
                    type="mailbox/message",
                    account_id=delivery.account_id,
                    created_at=now,
                    generation=target.generation,
                    payload={
                        "delivery_id": delivery.delivery_id,
                        "account_id": delivery.account_id,
                        "conversation_id": delivery.conversation_id,
                        "message_id": delivery.message_id,
                        "attempt": delivery.attempt + 1,
                        "mode": "adapter" if mode == "adapter" else "notify",
                    },
                )
            )
        return outcome

    def _inject(self, target: "_Target", waker, *, delivery_id: str, now: datetime) -> str:
        """把消息推进目标会话并启动一个回合；成功才算 delivered。

        失败分两类，语义不能混：
        * 通道不可用（没有密钥/宿主不支持）-> 消息**保持 queued**，等目标自己取信；
        * 通道可用但这次注入失败（HTTP 拒绝、连接出错）-> ``failed``（可重试）。
        """
        selector = getattr(waker, "for_host", None)
        if callable(selector):
            waker = selector(target.host_type)
        outcome = waker.wake(target.native_session_id, WAKE_NOTICE)
        return self._finish_injection(target, outcome, delivery_id=delivery_id, now=now)

    def _finish_injection(self, target: _Target, outcome, *, delivery_id: str, now: datetime) -> str:
        with self._context.uow().transaction() as uow:
            record = uow.deliveries.get(delivery_id)
            if record is None:
                raise NotFoundError(f"投递不存在：{delivery_id}")
            if record.state is not DeliveryState.DISPATCHED:
                return "raced"
            record.updated_at = now
            if getattr(outcome, "started", False):
                check_delivery_transition(record.state, DeliveryState.DELIVERED)
                record.state = DeliveryState.DELIVERED
                record.acked_at = now
                record.injected_at = now
                record.next_attempt_at = None
                record.last_error = None
                uow.deliveries.update(record)
                self._context.audit(
                    uow,
                    "delivery.acknowledged",
                    now=now,
                    account_id=record.account_id,
                    conversation_id=record.conversation_id,
                    message_id=record.message_id,
                    delivery_id=record.delivery_id,
                    detail={"mode": "wake", "generation": target.generation},
                )
                return "dispatched"
            detail = str(getattr(outcome, "detail", "") or "注入失败")
            unavailable = bool(getattr(outcome, "unavailable", False))
            if unavailable:
                # 通道这一刻用不了（没凭据 / 连不上 / 被拒）：不是投递失败，
                # 消息留在队列里等目标自己取信，也别消耗尝试次数。
                check_delivery_transition(record.state, DeliveryState.QUEUED)
                record.state = DeliveryState.QUEUED
                record.attempt = max(record.attempt - 1, 0)
                record.next_attempt_at = plus_seconds(now, self._policy.notify_retry_seconds)
            elif record.attempt >= self._policy.max_attempts:
                # 真失败且重试次数用尽 -> 死信（不能无限重试下去）。
                check_delivery_transition(record.state, DeliveryState.DEAD_LETTER)
                record.state = DeliveryState.DEAD_LETTER
                record.next_attempt_at = None
            else:
                check_delivery_transition(record.state, DeliveryState.FAILED)
                record.state = DeliveryState.FAILED
                record.next_attempt_at = plus_seconds(
                    now, self._policy.backoff_ms(record.attempt) / 1000
                )
            record.last_error = detail[:500]
            uow.deliveries.update(record)
            self._context.audit(
                uow,
                "wake.failed",
                now=now,
                account_id=record.account_id,
                conversation_id=record.conversation_id,
                message_id=record.message_id,
                delivery_id=record.delivery_id,
                detail={
                    "mode": "wake",
                    "unavailable": unavailable,
                    "detail": detail[:200],
                    "state": record.state.value,
                },
            )
            if unavailable:
                return "held"
            if record.state is DeliveryState.DEAD_LETTER:
                return "dead_letter"
            return "failed"

    # -- 确认与失败 -------------------------------------------------------

    def acknowledge(
        self,
        *,
        connection_id: str,
        delivery_id: str,
        injected_at: datetime | None = None,
    ) -> DeliveryRecord:
        """适配器确认"宿主已持久化接收"。

        必须由**当前主连接**确认：旧代次连接确认新投递会让"谁收到了"不可信。
        """
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            connection = self._context_account_service().require_current_connection(
                uow, connection_id
            )
            delivery = uow.deliveries.get(delivery_id)
            if delivery is None:
                raise NotFoundError(f"投递不存在：{delivery_id}")
            if delivery.account_id != connection.account_id:
                raise IdentityMismatchError(
                    f"投递 {delivery_id} 的目标账号是 {delivery.account_id}，"
                    f"与连接所属账号 {connection.account_id} 不一致"
                )
            if delivery.state is DeliveryState.DELIVERED:
                return delivery  # 幂等：重复确认不报错、不改时间
            check_delivery_transition(delivery.state, DeliveryState.DELIVERED)
            delivery.state = DeliveryState.DELIVERED
            delivery.acked_at = now
            delivery.injected_at = injected_at or now
            delivery.updated_at = now
            delivery.next_attempt_at = None
            delivery.last_error = None
            uow.deliveries.update(delivery)
            self._context.audit(
                uow,
                "delivery.acknowledged",
                now=now,
                account_id=delivery.account_id,
                connection_id=connection_id,
                conversation_id=delivery.conversation_id,
                message_id=delivery.message_id,
                delivery_id=delivery.delivery_id,
                detail={"attempt": delivery.attempt},
            )
        self._context.publish(
            Event(
                type="delivery/acknowledged",
                account_id=delivery.account_id,
                created_at=now,
                generation=connection.generation,
                payload={
                    "delivery_id": delivery.delivery_id,
                    "message_id": delivery.message_id,
                    "account_id": delivery.account_id,
                },
            )
        )
        return delivery

    def fail(
        self,
        *,
        connection_id: str,
        delivery_id: str,
        reason: str,
        retryable: bool = True,
    ) -> DeliveryRecord:
        """适配器报告本次投递失败。

        退避按指数增长；超过 ``max_attempts`` 或不可重试时进入死信并广播
        ``delivery/cancelled``，让上层知道"这条不会自己好了"。
        """
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            connection = self._context_account_service().require_connection(uow, connection_id)
            delivery = uow.deliveries.get(delivery_id)
            if delivery is None:
                raise NotFoundError(f"投递不存在：{delivery_id}")
            if delivery.account_id != connection.account_id:
                raise IdentityMismatchError(
                    f"投递 {delivery_id} 的目标账号与连接所属账号不一致"
                )
            if delivery.state.is_terminal:
                return delivery

            exhausted = delivery.attempt >= self._policy.max_attempts
            if not retryable or exhausted:
                check_delivery_transition(delivery.state, DeliveryState.DEAD_LETTER)
                delivery.state = DeliveryState.DEAD_LETTER
                delivery.updated_at = now
                delivery.last_error = reason
                delivery.next_attempt_at = None
                uow.deliveries.update(delivery)
                self._context.audit(
                    uow,
                    "delivery.dead_letter",
                    now=now,
                    account_id=delivery.account_id,
                    delivery_id=delivery.delivery_id,
                    message_id=delivery.message_id,
                    detail={"reason": reason, "attempt": delivery.attempt, "retryable": retryable},
                )
                event_type = "delivery/cancelled"
            else:
                check_delivery_transition(delivery.state, DeliveryState.FAILED)
                backoff_seconds = self._policy.backoff_ms(delivery.attempt) / 1000
                delivery.state = DeliveryState.FAILED
                delivery.updated_at = now
                delivery.last_error = reason
                delivery.next_attempt_at = plus_seconds(now, backoff_seconds)
                uow.deliveries.update(delivery)
                self._context.audit(
                    uow,
                    "delivery.retry_scheduled",
                    now=now,
                    account_id=delivery.account_id,
                    delivery_id=delivery.delivery_id,
                    message_id=delivery.message_id,
                    detail={
                        "reason": reason,
                        "attempt": delivery.attempt,
                        "backoff_seconds": backoff_seconds,
                    },
                )
                event_type = None
        if event_type:
            self._context.publish(
                Event(
                    type=event_type,
                    account_id=delivery.account_id,
                    created_at=now,
                    generation=connection.generation,
                    payload={
                        "delivery_id": delivery.delivery_id,
                        "message_id": delivery.message_id,
                        "reason": reason,
                        "attempt": delivery.attempt,
                    },
                )
            )
        return delivery

    def cancel(self, delivery_id: str, *, reason: str) -> DeliveryRecord:
        """外部取消一条投递（例如对话被阻塞或用户暂停）。"""
        now = self._clock.now()
        with self._context.uow().transaction() as uow:
            delivery = uow.deliveries.get(delivery_id)
            if delivery is None:
                raise NotFoundError(f"投递不存在：{delivery_id}")
            if delivery.state.is_terminal:
                return delivery
            check_delivery_transition(delivery.state, DeliveryState.DEAD_LETTER)
            delivery.state = DeliveryState.DEAD_LETTER
            delivery.updated_at = now
            delivery.last_error = reason
            delivery.next_attempt_at = None
            uow.deliveries.update(delivery)
            self._context.audit(
                uow,
                "delivery.cancelled",
                now=now,
                account_id=delivery.account_id,
                delivery_id=delivery.delivery_id,
                message_id=delivery.message_id,
                detail={"reason": reason},
            )
        self._context.publish(
            Event(
                type="delivery/cancelled",
                account_id=delivery.account_id,
                created_at=now,
                payload={
                    "delivery_id": delivery.delivery_id,
                    "message_id": delivery.message_id,
                    "reason": reason,
                },
            )
        )
        return delivery

    def pending_for_account(self, account_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        """某账号尚未送达的投递，按入队顺序（离线补投的读取入口）。"""
        with self._context.uow().transaction() as uow:
            return uow.deliveries.pending_for_account(account_id, limit=limit)

    def requeue_stalled(self, *, older_than_seconds: float = 60.0) -> int:
        """把"派发后长时间没确认"的投递退回排队。

        覆盖"连接在确认前断开"的场景（设计文档 §13.2）：重试复用同一个
        ``delivery_id``，因此接收方的去重逻辑能挡住重复注入。
        """
        now = self._clock.now()
        cutoff = plus_seconds(now, -older_than_seconds)
        requeued = 0
        with self._context.uow().transaction() as uow:
            rows = uow.deliveries.stalled_dispatched(before=cutoff)
            for delivery in rows:
                connection = uow.connections.current_for_account(delivery.account_id)
                online = connection is not None and _online_now(connection)
                delivery.updated_at = now
                delivery.next_attempt_at = now
                if online:
                    check_delivery_transition(delivery.state, DeliveryState.FAILED)
                    delivery.state = DeliveryState.FAILED
                    delivery.last_error = "派发后未在期限内收到确认"
                    reason = "ack_timeout"
                else:
                    # 目标进程已经不在：这不是"投递失败"，消息退回队列等它上线。
                    check_delivery_transition(delivery.state, DeliveryState.QUEUED)
                    delivery.state = DeliveryState.QUEUED
                    delivery.attempt = max(delivery.attempt - 1, 0)
                    delivery.last_error = "目标托管进程已退出：消息退回队列，等它上线再投"
                    reason = "target_offline"
                uow.deliveries.update(delivery)
                requeued += 1
                self._context.audit(
                    uow,
                    "delivery.retry_scheduled",
                    now=now,
                    account_id=delivery.account_id,
                    delivery_id=delivery.delivery_id,
                    message_id=delivery.message_id,
                    detail={"reason": reason, "attempt": delivery.attempt},
                )
        return requeued

    def _context_account_service(self):
        """延迟构造账号服务：投递需要连接身份校验，但不希望两个服务互相持有。"""
        from .account_service import AccountService

        return AccountService(self._context)

    # -- 适配器协议入口 ---------------------------------------------------

    def fetch_for_delivery(self, *, connection_id: str, delivery_id: str) -> dict[str, object]:
        """受身份约束地取完整消息正文。

        事件通道只发最小路由信息，正文必须走这里取，并校验"这条投递确实属于
        该连接所属账号"，避免通知通道泄露别人的消息（设计文档 §9.1）。
        """
        with self._context.uow().transaction() as uow:
            connection = self._context_account_service().require_connection(uow, connection_id)
            delivery = uow.deliveries.get(delivery_id)
            if delivery is None:
                raise NotFoundError(f"投递不存在：{delivery_id}")
            if delivery.account_id != connection.account_id:
                raise IdentityMismatchError("该投递不属于当前连接所属账号")
            message = uow.messages.get(delivery.message_id)
            if message is None:
                raise NotFoundError(f"消息不存在：{delivery.message_id}")
            sender = uow.accounts.get(message.sender_account_id)
            from .hosting import build_envelope

            envelope = build_envelope(
                message=message,
                delivery=delivery,
                sender_address=sender.address if sender else message.sender_account_id,
            )
            return {
                "delivery": delivery.to_dict(),
                "message": message.to_dict(),
                "envelope": {
                    "from_address": envelope.from_address,
                    "conversation_id": envelope.conversation_id,
                    "message_id": envelope.message_id,
                    "delivery_id": envelope.delivery_id,
                    "reply_to": envelope.reply_to,
                    "content_type": envelope.content_type,
                    "header": envelope.header(),
                },
            }

    # -- 内部工具 ---------------------------------------------------------

    @staticmethod
    def validate_delivery_account(delivery: DeliveryRecord, account_id: str) -> None:
        if delivery.account_id != account_id:
            raise ValidationError("投递与账号不匹配")
