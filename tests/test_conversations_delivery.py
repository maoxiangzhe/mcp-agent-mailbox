"""阶段 3 集成测试：一对一对话、至少一次投递、离线补投、幂等、重试与死信、互聊硬限制。"""

from __future__ import annotations

import pytest

#: 不可能存在的 PID：表示"托管该会话的进程已退出"。
DEAD_PID = 2_147_483_646

from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, Settings, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.errors import (
    ConversationClosedError,
    IdentityMismatchError,
    LoopLimitExceededError,
    NotAParticipantError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from mcp_agent_mailbox.domain.messages import DeliveryState, ProcessingState
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresencePolicy
from mcp_agent_mailbox.domain.timestamps import utc_now
from mcp_agent_mailbox.ports.clock import FrozenClock
from mcp_agent_mailbox.ports.event_transport import Event


@pytest.fixture
def settings(tmp_path) -> Settings:
    return load_settings(
        data_dir=tmp_path / "mailbox",
        presence=PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10),
        rate_limits=RateLimits(),
        delivery=DeliveryPolicy(max_attempts=3, base_backoff_ms=1000, max_backoff_ms=8000),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(utc_now())


@pytest.fixture
def broker(settings, clock) -> Broker:
    instance = Broker(settings, clock=clock)
    try:
        yield instance
    finally:
        instance.stop()


def bind(broker, session: str, *, host: str = "dsh", level=HostCapabilityLevel.WAKE):
    return broker.accounts.bind_session(
        __import__(
            "mcp_agent_mailbox.domain.accounts", fromlist=["HostIdentity"]
        ).HostIdentity(host, "desktop-default", session),
        display_name=f"{host}/{session}",
        capability_level=level,
        adapter_name="test-adapter",
    )


@pytest.fixture
def pair(broker):
    """两个实时会话。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b")
    return a, b


def drain(broker, session) -> list[Event]:
    events = []
    while True:
        event = broker.events.receive(session.connection_id, 0.01)
        if event is None:
            return events
        events.append(event)


# ---------------------------------------------------------------------------
# 对话与消息创建
# ---------------------------------------------------------------------------


def test_start_conversation_creates_message_and_queued_delivery(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="你好"
    )
    assert result.conversation_id.startswith("conv_")
    assert result.delivery_id.startswith("del_")
    assert result.delivery_state == DeliveryState.QUEUED.value
    assert result.message.sender_account_id == a.account_id
    assert result.message.recipient_account_id == b.account_id
    assert result.recipient_presence == "connected"

    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
        message = uow.messages.get(result.message.message_id)
        conversation = uow.conversations.get(result.conversation_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED
    assert message is not None and message.content == "你好"
    assert conversation is not None and conversation.last_message_at is not None


def test_start_conversation_reuses_open_direct_conversation(broker, pair) -> None:
    a, b = pair
    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="第一条"
    )
    second = broker.conversations.start_conversation(
        connection_id=b.connection_id, to_account_id=a.account_id, text="回你"
    )
    assert second.conversation_id == first.conversation_id


def test_conversation_list_marks_unread_for_recipient(broker, pair) -> None:
    a, b = pair
    broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="给 B 的"
    )
    views, cursor = broker.conversations.list_conversations(connection_id=b.connection_id)
    assert cursor is None
    assert len(views) == 1
    assert views[0].unread_count == 1
    assert views[0].counterpart_account_id == a.account_id
    assert views[0].presence == "connected"

    mine, _ = broker.conversations.list_conversations(connection_id=a.connection_id)
    assert mine[0].unread_count == 0


def test_cannot_start_conversation_with_self(broker, pair) -> None:
    a, _ = pair
    with pytest.raises(ValidationError):
        broker.conversations.start_conversation(
            connection_id=a.connection_id, to_account_id=a.account_id, text="自言自语"
        )


def test_unknown_recipient_raises(broker, pair) -> None:
    a, _ = pair
    with pytest.raises(NotFoundError):
        broker.conversations.start_conversation(
            connection_id=a.connection_id, to_account_id="acc_missing", text="喂"
        )


def test_blocked_contact_cannot_receive(broker, pair) -> None:
    from mcp_agent_mailbox.domain.errors import RecipientBlockedError

    a, b = pair
    broker.accounts.set_contact_policy(b.account_id, a.account_id, "block")
    with pytest.raises(RecipientBlockedError):
        broker.conversations.start_conversation(
            connection_id=a.connection_id, to_account_id=b.account_id, text="在吗"
        )


# ---------------------------------------------------------------------------
# 身份与权限
# ---------------------------------------------------------------------------


def test_sender_comes_from_connection_not_parameters(broker, pair) -> None:
    """发送者只能由连接上下文决定：换连接就换发送者，没有任何参数能覆盖。"""
    a, b = pair
    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="A 发的"
    )
    # 同一对话里 B 用**自己的连接**发消息，发送者必须是 B。
    second = broker.conversations.send_message(
        connection_id=b.connection_id, conversation_id=first.conversation_id, text="B 发的"
    )
    assert second.message.sender_account_id == b.account_id
    assert second.message.recipient_account_id == a.account_id


def test_outsider_cannot_read_or_send(broker, pair) -> None:
    a, b = pair
    outsider = bind(broker, "session-c")
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="私事"
    )
    with pytest.raises(NotAParticipantError):
        broker.conversations.read_conversation(
            connection_id=outsider.connection_id, conversation_id=result.conversation_id
        )
    with pytest.raises(NotAParticipantError):
        broker.conversations.send_message(
            connection_id=outsider.connection_id,
            conversation_id=result.conversation_id,
            text="插一句",
        )


def test_old_generation_connection_cannot_send(broker, pair) -> None:
    a, b = pair
    old_connection = a.connection_id
    rebind = bind(broker, "session-a")  # 同账号新连接，旧连接被取代
    assert rebind.account_id == a.account_id
    with pytest.raises(IdentityMismatchError):
        broker.conversations.start_conversation(
            connection_id=old_connection, to_account_id=b.account_id, text="旧连接"
        )


def test_reply_goes_back_to_sender_not_third_party(broker, pair) -> None:
    a, b = pair
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="问题"
    )
    reply = broker.conversations.reply_message(
        connection_id=b.connection_id, message_id=started.message.message_id, text="答复"
    )
    assert reply.message.reply_to == started.message.message_id
    assert reply.message.sender_account_id == b.account_id
    assert reply.message.recipient_account_id == a.account_id


def test_cannot_reply_to_unknown_message(broker, pair) -> None:
    a, _ = pair
    with pytest.raises(NotFoundError):
        broker.conversations.reply_message(
            connection_id=a.connection_id, message_id="msg_missing", text="?"
        )


def test_cannot_reply_across_conversations(broker, pair) -> None:
    a, b = pair
    c = bind(broker, "session-c")
    ab = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="A-B"
    )
    ac = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=c.account_id, text="A-C"
    )
    # B 不能回复 A-C 对话里的消息。
    with pytest.raises(NotAParticipantError):
        broker.conversations.reply_message(
            connection_id=b.connection_id,
            message_id=ac.message.message_id,
            text="越界回复",
        )
    # A 在自己参与的对话里回复是允许的（收件人固定为原发送者）。
    reply = broker.conversations.reply_message(
        connection_id=a.connection_id, message_id=ab.message.message_id, text="自问自答"
    )
    assert reply.message.recipient_account_id == b.account_id


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_repeated_send_with_same_key_returns_original(broker, pair) -> None:
    a, b = pair
    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="只发一次", idempotency_key="k1"
    )
    again = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="只发一次", idempotency_key="k1"
    )
    assert again.duplicate is True
    assert again.message.message_id == first.message.message_id
    assert again.delivery_id == first.delivery_id

    with broker.unit_of_work().transaction() as uow:
        page = uow.messages.list_for_conversation(first.conversation_id)
    assert len(page.items) == 1, "重试不得重复发信"


def test_idempotency_key_is_scoped_to_sender(broker, pair) -> None:
    a, b = pair
    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="A 的", idempotency_key="same"
    )
    second = broker.conversations.send_message(
        connection_id=b.connection_id,
        conversation_id=first.conversation_id,
        text="B 的",
        idempotency_key="same",
    )
    assert second.duplicate is False
    assert second.message.message_id != first.message.message_id


# ---------------------------------------------------------------------------
# 投递：派发、确认、去重
# ---------------------------------------------------------------------------


def test_dispatch_delivers_to_realtime_target(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="派发给我"
    )
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 1
    events = drain(broker, b)
    assert len(events) == 1
    payload = events[0].to_dict()
    assert events[0].type == "mailbox/message"
    assert payload["delivery_id"] == result.delivery_id
    assert payload["conversation_id"] == result.conversation_id
    assert payload["message_id"] == result.message.message_id
    assert payload["account_id"] == b.account_id
    assert "content" not in payload, "事件只能带最小路由信息，不得携带正文"


def test_dispatch_does_not_duplicate_in_flight_delivery(broker, pair) -> None:
    a, b = pair
    broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="一次派发"
    )
    first = broker.deliveries.dispatch_due()
    second = broker.deliveries.dispatch_due()
    assert first.dispatched == 1
    assert second.scanned == 0, "已 dispatched 的投递不再进入派发候选"


def test_acknowledge_marks_delivered_and_is_idempotent(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="确认我"
    )
    broker.deliveries.dispatch_due()
    delivered = broker.deliveries.acknowledge(
        connection_id=b.connection_id, delivery_id=result.delivery_id
    )
    assert delivered.state is DeliveryState.DELIVERED
    assert delivered.acked_at is not None
    again = broker.deliveries.acknowledge(
        connection_id=b.connection_id, delivery_id=result.delivery_id
    )
    assert again.state is DeliveryState.DELIVERED
    assert again.acked_at == delivered.acked_at, "重复确认不能改时间"


def test_acknowledge_rejects_wrong_account(broker, pair) -> None:
    a, b = pair
    c = bind(broker, "session-c")
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="给 B"
    )
    broker.deliveries.dispatch_due()
    with pytest.raises(IdentityMismatchError):
        broker.deliveries.acknowledge(connection_id=c.connection_id, delivery_id=result.delivery_id)


def test_acknowledge_rejects_superseded_connection(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="给 B"
    )
    broker.deliveries.dispatch_due()
    old = b.connection_id
    bind(broker, "session-b")  # 新代次接管
    with pytest.raises(IdentityMismatchError):
        broker.deliveries.acknowledge(connection_id=old, delivery_id=result.delivery_id)


def test_acknowledge_rejects_unknown_delivery(broker, pair) -> None:
    _, b = pair
    with pytest.raises(NotFoundError):
        broker.deliveries.acknowledge(connection_id=b.connection_id, delivery_id="del_missing")


# ---------------------------------------------------------------------------
# 离线补投
# ---------------------------------------------------------------------------


def test_offline_target_keeps_message_queued(broker, pair) -> None:
    a, b = pair
    broker.accounts.close_connection(b.connection_id)
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="等你回来"
    )
    report = broker.deliveries.dispatch_due()
    assert report.skipped_offline == 1
    assert report.dispatched == 0
    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED, "离线时保持排队"
    assert delivery.attempt == 0, "离线不算一次投递尝试"


def test_reconnect_triggers_catchup_delivery(broker, pair) -> None:
    a, b = pair
    broker.accounts.close_connection(b.connection_id)
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="补投给我"
    )
    broker.deliveries.dispatch_due()
    assert broker.events.receive(b.connection_id, 0.01) is None

    reconnect = bind(broker, "session-b")
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 1
    events = drain(broker, reconnect)
    assert [e.to_dict()["delivery_id"] for e in events] == [result.delivery_id]


def test_backlog_is_delivered_in_order(broker, pair) -> None:
    a, b = pair
    broker.accounts.close_connection(b.connection_id)
    ids = []
    for index in range(5):
        result = broker.conversations.start_conversation(
            connection_id=a.connection_id, to_account_id=b.account_id, text=f"第 {index} 条"
        )
        ids.append(result.delivery_id)
    assert broker.deliveries.dispatch_due().skipped_offline == 5

    reconnect = bind(broker, "session-b")
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 5
    delivered = [e.to_dict()["delivery_id"] for e in drain(broker, reconnect)]
    assert delivered == ids, "离线积压必须按入队顺序补投"


# ---------------------------------------------------------------------------
# 重试、死信、恢复
# ---------------------------------------------------------------------------


def test_failure_schedules_retry_with_backoff(broker, pair, clock) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="会失败"
    )
    broker.deliveries.dispatch_due()
    failed = broker.deliveries.fail(
        connection_id=b.connection_id, delivery_id=result.delivery_id, reason="注入失败"
    )
    assert failed.state is DeliveryState.FAILED
    assert failed.next_attempt_at is not None

    assert broker.deliveries.dispatch_due().scanned == 0, "退避未到不重试"
    clock.advance(2)
    assert broker.deliveries.dispatch_due().dispatched == 1, "退避到点后重试同一个 delivery_id"


def test_retry_reuses_same_delivery_id(broker, pair, clock) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="重试标识"
    )
    broker.deliveries.dispatch_due()
    broker.deliveries.fail(
        connection_id=b.connection_id, delivery_id=result.delivery_id, reason="瞬时故障"
    )
    clock.advance(5)
    broker.deliveries.dispatch_due()
    events = drain(broker, b)
    assert len(events) == 2
    assert {e.to_dict()["delivery_id"] for e in events} == {result.delivery_id}
    assert [e.to_dict()["attempt"] for e in events] == [1, 2]


def test_exhausted_attempts_become_dead_letter(broker, pair, clock) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="一直失败"
    )
    for attempt in range(3):
        broker.deliveries.dispatch_due()
        record = broker.deliveries.fail(
            connection_id=b.connection_id,
            delivery_id=result.delivery_id,
            reason=f"第 {attempt + 1} 次失败",
        )
        clock.advance(30)
    assert record.state is DeliveryState.DEAD_LETTER
    cancelled = [
        e for e in drain(broker, b) if e.type == "delivery/cancelled"
    ]
    assert cancelled, "进入死信必须广播 delivery/cancelled"
    assert broker.deliveries.dispatch_due().scanned == 0


def test_non_retryable_failure_goes_straight_to_dead_letter(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="不可重试"
    )
    broker.deliveries.dispatch_due()
    record = broker.deliveries.fail(
        connection_id=b.connection_id,
        delivery_id=result.delivery_id,
        reason="适配器不支持",
        retryable=False,
    )
    assert record.state is DeliveryState.DEAD_LETTER


def test_stalled_dispatch_is_requeued_after_lease(broker, pair, clock) -> None:
    """模拟"派发后、确认前断线"：适配器仍在续租，但一直没确认。

    超时后退回重试，且**必须复用同一个 delivery_id**，接收方才能按它去重。
    （连接本身保持健康：这里要验证的是"未确认"而非"离线"，离线有单独的用例。）
    """
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="确认前断线"
    )
    broker.deliveries.dispatch_due()
    first_attempt = [e.to_dict() for e in drain(broker, b)]
    assert [e["delivery_id"] for e in first_attempt] == [result.delivery_id]
    assert [e["attempt"] for e in first_attempt] == [1]

    clock.advance(40)
    broker.accounts.renew(b.connection_id)  # 适配器还活着，只是没确认
    requeued = broker.deliveries.requeue_stalled(older_than_seconds=30)
    assert requeued == 1
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 1
    second_attempt = [e.to_dict() for e in drain(broker, b)]
    assert [e["delivery_id"] for e in second_attempt] == [result.delivery_id]
    assert [e["attempt"] for e in second_attempt] == [2], "补投必须复用 delivery_id 并递增尝试次数"


def test_host_process_exit_stops_dispatch(broker, pair, clock) -> None:
    """托管进程退出后不再派发：即使消息还排在队列里，也不该往死连接上推。

    产品规则是"在线严格参照进程"，所以这里让 B 的托管进程消失（而不是等租约过期）。
    """
    a, b = pair
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(b.connection_id)
        assert connection is not None
        connection.host_pid = DEAD_PID
        connection.durable = False
        uow.connections.update(connection)

    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="对方进程已退出"
    )
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 0
    assert report.skipped_offline == 1
    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED, "消息必须保留待补投"


def test_lease_expiry_alone_stops_dispatch_without_process_info(broker, pair, clock) -> None:
    """没有进程可参照的适配器仍按租约：租约过期后不派发。"""
    a, b = pair
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(b.connection_id)
        assert connection is not None
        connection.host_pid = None
        connection.durable = False
        uow.connections.update(connection)

    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="租约会过期"
    )
    clock.advance(120)  # 超过 租约 60s + 宽限 10s
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 0
    assert report.skipped_offline == 1
    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
    assert delivery is not None and delivery.state is DeliveryState.QUEUED


def test_broker_restart_recovers_pending_delivery(settings, clock, tmp_path) -> None:
    """Broker 重启（进程崩溃）后：未完成投递仍在队列，在线状态全部重算。"""
    first = Broker(settings, clock=clock)
    a = bind(first, "session-a")
    b = bind(first, "session-b")
    first.accounts.close_connection(b.connection_id)
    result = first.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="崩溃前发的"
    )
    first.deliveries.dispatch_due()
    first.stop()

    second = Broker(settings, clock=clock)
    try:
        # 重启后连接不应被沿用：账号必须是离线，直到重新绑定。
        assert second.presence.for_account(b.account_id).state.value == "offline"
        reconnect = bind(second, "session-b")
        report = second.deliveries.dispatch_due()
        assert report.dispatched == 1
        events = [e.to_dict()["delivery_id"] for e in drain(second, reconnect)]
        assert events == [result.delivery_id]
    finally:
        second.stop()


# ---------------------------------------------------------------------------
# 处理状态
# ---------------------------------------------------------------------------


def test_recipient_sets_processing_states(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="处理我"
    )
    running = broker.conversations.set_message_status(
        connection_id=b.connection_id, message_id=result.message.message_id, processing="running"
    )
    assert running.state is ProcessingState.RUNNING
    completed = broker.conversations.set_message_status(
        connection_id=b.connection_id,
        message_id=result.message.message_id,
        processing="completed",
        result="已复核",
    )
    assert completed.state is ProcessingState.COMPLETED
    assert completed.result == "已复核"


def test_completed_cannot_return_to_running(broker, pair) -> None:
    from mcp_agent_mailbox.domain.errors import InvalidTransitionError

    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="完成就完了"
    )
    broker.conversations.set_message_status(
        connection_id=b.connection_id, message_id=result.message.message_id, processing="completed"
    )
    with pytest.raises(InvalidTransitionError):
        broker.conversations.set_message_status(
            connection_id=b.connection_id, message_id=result.message.message_id, processing="running"
        )


def test_sender_cannot_set_processing_status_of_own_message(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="我发的"
    )
    with pytest.raises(NotAParticipantError):
        broker.conversations.set_message_status(
            connection_id=a.connection_id, message_id=result.message.message_id, processing="completed"
        )


def test_invalid_processing_value_is_rejected(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="状态"
    )
    with pytest.raises(ValidationError):
        broker.conversations.set_message_status(
            connection_id=b.connection_id, message_id=result.message.message_id, processing="invented"
        )


def test_read_conversation_marks_seen_and_clears_unread(broker, pair) -> None:
    a, b = pair
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="读我"
    )
    page = broker.conversations.read_conversation(
        connection_id=b.connection_id, conversation_id=result.conversation_id
    )
    assert page.unread_count == 0
    assert page.messages[0].visibility.value == "seen"

    page_readonly = broker.conversations.read_conversation(
        connection_id=b.connection_id, conversation_id=result.conversation_id, mark_seen=False
    )
    assert len(page_readonly.messages) == 1


def test_read_pagination_is_stable(broker, pair) -> None:
    a, b = pair
    first = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="m0"
    )
    for index in range(1, 5):
        broker.conversations.send_message(
            connection_id=a.connection_id, conversation_id=first.conversation_id, text=f"m{index}"
        )
    page1 = broker.conversations.read_conversation(
        connection_id=b.connection_id, conversation_id=first.conversation_id, limit=2
    )
    assert [m.message.content for m in page1.messages] == ["m0", "m1"]
    assert page1.next_cursor is not None
    page2 = broker.conversations.read_conversation(
        connection_id=b.connection_id,
        conversation_id=first.conversation_id,
        after_message_id=page1.next_cursor,
        limit=10,
    )
    assert [m.message.content for m in page2.messages] == ["m2", "m3", "m4"]
    assert page2.next_cursor is None


# ---------------------------------------------------------------------------
# 速率限制与自动互聊硬限制
# ---------------------------------------------------------------------------


def test_send_rate_limit_per_account(tmp_path, clock) -> None:
    """账号级速率限制：超出后抛 RateLimitedError，且不再写入消息。"""
    limited = load_settings(
        data_dir=tmp_path / "limited",
        presence=PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10),
        rate_limits=RateLimits(max_sends_per_minute=3),
        delivery=DeliveryPolicy(),
    )
    with Broker(limited, clock=clock) as instance:
        a = bind(instance, "session-a")
        b = bind(instance, "session-b")
        started = instance.conversations.start_conversation(
            connection_id=a.connection_id, to_account_id=b.account_id, text="1"
        )
        instance.conversations.send_message(
            connection_id=a.connection_id, conversation_id=started.conversation_id, text="2"
        )
        instance.conversations.send_message(
            connection_id=a.connection_id, conversation_id=started.conversation_id, text="3"
        )
        with pytest.raises(RateLimitedError):
            instance.conversations.send_message(
                connection_id=a.connection_id, conversation_id=started.conversation_id, text="4"
            )
        with instance.unit_of_work().transaction() as uow:
            page = uow.messages.list_for_conversation(started.conversation_id)
        assert len(page.items) == 3, "被限流的消息不得落库"


def test_auto_turn_limit_blocks_conversation_and_keeps_messages(broker, settings, clock) -> None:
    """连续自动往返达到上限：停止唤醒、消息保留、对话标记阻塞并给出原因。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b")
    limit = settings.rate_limits.max_auto_turns_per_conversation
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="开始", wait_for_reply=True
    )
    conversation_id = started.conversation_id
    current = started
    # 每轮内容都不同，因此触发的一定是"轮数上限"而不是"重复内容"。
    with pytest.raises(LoopLimitExceededError):
        for step in range(limit + 2):
            clock.advance(1)
            sender = b if step % 2 == 0 else a
            current = broker.conversations.reply_message(
                connection_id=sender.connection_id,
                message_id=current.message.message_id,
                text=f"自动回复第 {step} 轮",
                wait_for_reply=True,
            )
    with broker.unit_of_work().transaction() as uow:
        conversation = uow.conversations.get(conversation_id)
        page = uow.messages.list_for_conversation(conversation_id)
    assert conversation is not None
    assert conversation.blocked is True
    assert "上限" in (conversation.blocked_reason or "")
    assert 1 < len(page.items) <= limit + 2, "超限前已发出的消息必须全部保留"


def test_blocked_conversation_keeps_stored_messages_readable(broker, settings, clock) -> None:
    """阻塞后消息仍然可读（保留而不删除），但不能继续发送。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b")
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="阻塞前", wait_for_reply=True
    )
    broker.conversations.block_conversation(started.conversation_id, reason="测试阻塞")
    page = broker.conversations.read_conversation(
        connection_id=b.connection_id, conversation_id=started.conversation_id
    )
    assert [m.message.content for m in page.messages] == ["阻塞前"]
    with pytest.raises(ConversationClosedError):
        broker.conversations.reply_message(
            connection_id=b.connection_id, message_id=started.message.message_id, text="继续"
        )


def test_repeated_content_is_detected_as_loop(broker, settings, clock) -> None:
    """同一正文出现次数达到阈值即判循环：在**本次发送**就被拒绝，并阻塞对话。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b")
    threshold = settings.rate_limits.repeated_content_limit
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="同一句话"
    )
    conversation_id = started.conversation_id
    current = started
    sent = 1  # 起始消息已经带上了这个正文
    with pytest.raises(LoopLimitExceededError):
        # threshold 为 3 时：起始 1 条 + 第 2、3 条放行，第 4 条被拒。
        for step in range(threshold + 2):
            clock.advance(1)
            sender = b if step % 2 == 0 else a
            current = broker.conversations.reply_message(
                connection_id=sender.connection_id,
                message_id=current.message.message_id,
                text="同一句话",
            )
            sent += 1
    with broker.unit_of_work().transaction() as uow:
        conversation = uow.conversations.get(conversation_id)
        page = uow.messages.list_for_conversation(conversation_id)
    assert conversation is not None
    assert conversation.blocked is True
    assert "循环" in (conversation.blocked_reason or "")
    assert len(page.items) == sent, "被拒绝的那条不得落库；此前消息必须保留"


def test_different_content_is_not_treated_as_loop(broker, settings, clock) -> None:
    """内容各不相同就不该被循环检测误伤。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b")
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="第 0 条"
    )
    current = started
    for step in range(1, 6):
        clock.advance(1)
        sender = b if step % 2 else a
        current = broker.conversations.reply_message(
            connection_id=sender.connection_id,
            message_id=current.message.message_id,
            text=f"第 {step} 条不同的内容",
        )
    with broker.unit_of_work().transaction() as uow:
        conversation = uow.conversations.get(started.conversation_id)
    assert conversation is not None and conversation.blocked is False


def test_blocked_conversation_rejects_new_messages(broker, pair) -> None:
    a, b = pair
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="马上阻塞"
    )
    broker.conversations.block_conversation(started.conversation_id, reason="手动暂停")
    events = [
        e.to_dict() for e in drain(broker, a) if e.type == "conversation/blocked"
    ]
    assert events
    with pytest.raises(ConversationClosedError):
        broker.conversations.send_message(
            connection_id=a.connection_id, conversation_id=started.conversation_id, text="还能发吗"
        )


def test_unblock_allows_conversation_again(broker, pair) -> None:
    a, b = pair
    started = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="恢复测试", wait_for_reply=True
    )
    broker.conversations.block_conversation(started.conversation_id, reason="暂停")
    broker.conversations.unblock_conversation(started.conversation_id)
    result = broker.conversations.send_message(
        connection_id=a.connection_id, conversation_id=started.conversation_id, text="继续"
    )
    assert result.message.content == "继续"


# ---------------------------------------------------------------------------
# Level 1 / Level 0 目标：绝不谎报送达
# ---------------------------------------------------------------------------


def test_tools_only_target_is_never_reported_delivered(broker) -> None:
    """Level 0 目标：消息保留在队列，投递绝不能变成 dispatched/delivered。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b", level=HostCapabilityLevel.TOOLS_ONLY)
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="你只能主动取信"
    )
    report = broker.deliveries.dispatch_due()
    assert report.dispatched == 0
    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
    assert delivery is not None
    assert delivery.state in (DeliveryState.QUEUED, DeliveryState.FAILED)
    assert delivery.state is not DeliveryState.DELIVERED


def test_notify_only_target_gets_event_but_no_delivered_state(broker) -> None:
    """Level 1 目标：可以收到通知事件，但投递不算送达。"""
    a = bind(broker, "session-a")
    b = bind(broker, "session-b", level=HostCapabilityLevel.NOTIFY)
    result = broker.conversations.start_conversation(
        connection_id=a.connection_id, to_account_id=b.account_id, text="只通知"
    )
    report = broker.deliveries.dispatch_due()
    assert report.notified_only == 1
    events = drain(broker, b)
    assert len(events) == 1
    assert events[0].to_dict()["mode"] == "notify"
    with broker.unit_of_work().transaction() as uow:
        delivery = uow.deliveries.get(result.delivery_id)
    assert delivery is not None and delivery.state is not DeliveryState.DELIVERED
    assert delivery.notified_at is not None
