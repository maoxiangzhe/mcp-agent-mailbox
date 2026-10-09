"""仓储行为测试：唯一约束、代次、连接回收、分页、抢占式派发。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from mcp_agent_mailbox.domain.accounts import Account, Connection, ConnectionState, HostIdentity
from mcp_agent_mailbox.domain.conversations import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    ParticipantRole,
    participant_key,
)
from mcp_agent_mailbox.domain.errors import NotFoundError, ValidationError
from mcp_agent_mailbox.domain.messages import (
    ContentType,
    DeliveryRecord,
    DeliveryState,
    Message,
    content_hash,
)
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresenceState
from mcp_agent_mailbox.domain.timestamps import utc_now

START = utc_now()

#: 不可能存在的 PID：用来表示"托管进程已经退出"。
DEAD_PID = 2_147_483_646


def make_account(account_id: str, *, session: str | None = None, host: str = "dsh", level: int = 2) -> Account:
    identity = HostIdentity(host_type=host, host_instance_id="desktop-default", native_session_id=session or account_id)
    return Account(
        account_id=account_id,
        identity=identity,
        display_name=f"会话 {account_id}",
        address=f"{host}:{account_id}@desktop-default",
        capability_level=HostCapabilityLevel(level),
        created_at=START,
        updated_at=START,
    )


def make_connection(
    account_id: str,
    connection_id: str,
    generation: int,
    *,
    current: bool = True,
    host_pid: int | None = None,
) -> Connection:
    """默认没有进程信息（= 离线）；要表示在线就传一个真活着的 PID。"""
    return Connection(
        connection_id=connection_id,
        account_id=account_id,
        generation=generation,
        state=ConnectionState.ACTIVE,
        is_current=current,
        capability_level=HostCapabilityLevel.WAKE,
        opened_at=START,
        lease_expires_at=START + timedelta(seconds=60),
        host_pid=host_pid,
    )


def seed_accounts(uow, *accounts: Account) -> None:
    with uow.transaction() as u:
        for account in accounts:
            u.accounts.add(account)


def seed_direct_conversation(uow, a: Account, b: Account, conversation_id: str = "conv_1") -> Conversation:
    conversation = Conversation(
        conversation_id=conversation_id,
        kind=ConversationKind.DIRECT,
        participant_key=participant_key(a.account_id, b.account_id),
        created_by=a.account_id,
        created_at=START,
        updated_at=START,
    )
    with uow.transaction() as u:
        u.conversations.add(conversation)
        u.conversations.add_participant(
            ConversationParticipant(conversation_id, a.account_id, ParticipantRole.INITIATOR, START)
        )
        u.conversations.add_participant(
            ConversationParticipant(conversation_id, b.account_id, ParticipantRole.PEER, START)
        )
    return conversation


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


def test_account_roundtrip_with_metadata(uow) -> None:
    account = make_account("acc_1")
    account.workspace_hint = r"E:\zcz"
    account.metadata = {"adapter": "dsh", "note": "测试"}
    seed_accounts(uow, account)
    with uow.transaction() as u:
        loaded = u.accounts.get("acc_1")
    assert loaded is not None
    assert loaded.identity == account.identity
    assert loaded.workspace_hint == r"E:\zcz"
    assert loaded.metadata == {"adapter": "dsh", "note": "测试"}
    assert loaded.capability_level is HostCapabilityLevel.WAKE


def test_find_by_identity_is_exact(uow) -> None:
    seed_accounts(uow, make_account("acc_1", session="session-a"))
    with uow.transaction() as u:
        found = u.accounts.find_by_identity(
            HostIdentity("dsh", "desktop-default", "session-a")
        )
        missing = u.accounts.find_by_identity(
            HostIdentity("dsh", "desktop-default", "session-b")
        )
        other_host = u.accounts.find_by_identity(
            HostIdentity("codex", "desktop-default", "session-a")
        )
    assert found is not None and found.account_id == "acc_1"
    assert missing is None
    assert other_host is None, "宿主类型不同必须是不同账号"


def test_duplicate_identity_is_rejected_by_database(uow) -> None:
    seed_accounts(uow, make_account("acc_1", session="session-a"))
    with pytest.raises(ValidationError):
        seed_accounts(uow, make_account("acc_2", session="session-a"))


def test_update_missing_account_raises(uow) -> None:
    with pytest.raises(NotFoundError):
        with uow.transaction() as u:
            u.accounts.update(make_account("acc_missing"))


def test_list_accounts_filters_host_type_and_presence(uow) -> None:
    online = make_account("acc_online")
    offline = make_account("acc_offline")
    codex = make_account("acc_codex", host="codex")
    seed_accounts(uow, online, offline, codex)
    # 在线只看托管进程：给 acc_online 一个真活着的 PID，其它账号没有进程信息。
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_online", "con_1", 1, host_pid=os.getpid()))
    with uow.transaction() as u:
        by_host = u.accounts.list_accounts(host_type="dsh")
        online_only = u.accounts.list_accounts(presence=PresenceState.CONNECTED)
        offline_only = u.accounts.list_accounts(presence=PresenceState.OFFLINE)
        everything = u.accounts.list_accounts()
    assert {a.account_id for a in by_host} == {"acc_online", "acc_offline"}
    assert {a.account_id for a in online_only} == {"acc_online"}
    assert {a.account_id for a in offline_only} == {"acc_offline", "acc_codex"}
    assert len(everything) == 3


# ---------------------------------------------------------------------------
# 连接与代次
# ---------------------------------------------------------------------------


def test_generation_increments_per_account(uow) -> None:
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        assert u.accounts.next_generation("acc_1") == 1
        u.connections.add(make_connection("acc_1", "con_1", 1))
    with uow.transaction() as u:
        assert u.accounts.next_generation("acc_1") == 2, "已有 con_1(generation=1) 后下一个是 2"
    with uow.transaction() as u:
        u.connections.demote_current("acc_1", state=ConnectionState.SUPERSEDED, at=START)
        u.connections.add(make_connection("acc_1", "con_2", 2))
    with uow.transaction() as u:
        current = u.connections.current_for_account("acc_1")
        old = u.connections.get("con_1")
    assert current is not None and current.connection_id == "con_2"
    assert old is not None and old.state is ConnectionState.SUPERSEDED and not old.is_current


def test_demote_current_returns_rowcount(uow) -> None:
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_1", "con_1", 1))
    with uow.transaction() as u:
        assert u.connections.demote_current("acc_1", state=ConnectionState.CLOSED, at=START) == 1
    with uow.transaction() as u:
        assert u.connections.demote_current("acc_1", state=ConnectionState.CLOSED, at=START) == 0
        assert u.connections.current_for_account("acc_1") is None


def test_mark_disconnected_recycles_dead_process(uow) -> None:
    """托管进程已退出 -> 回收并清掉 is_current。"""
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_1", "con_1", 1, host_pid=DEAD_PID))
    with uow.transaction() as u:
        expired = u.connections.mark_disconnected()
    assert expired == ["con_1"]
    with uow.transaction() as u:
        connection = u.connections.get("con_1")
        assert connection is not None
        assert connection.state is ConnectionState.EXPIRED
        assert connection.is_current is False
        assert u.connections.current_for_account("acc_1") is None


def test_mark_disconnected_keeps_live_process(uow) -> None:
    """进程还活着就不回收——租约过期也不回收（会话思考时不发心跳）。"""
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_1", "con_1", 1, host_pid=os.getpid()))
    with uow.transaction() as u:
        assert u.connections.mark_disconnected() == []
        current = u.connections.current_for_account("acc_1")
    assert current is not None and current.state is ConnectionState.ACTIVE


def test_mark_disconnected_recycles_connection_without_process_info(uow) -> None:
    """拿不到进程信息 = 离线：也要回收，不留着当"可能在线"。"""
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_1", "con_1", 1))
    with uow.transaction() as u:
        assert u.connections.mark_disconnected() == ["con_1"]


def test_second_current_connection_is_rejected(uow) -> None:
    seed_accounts(uow, make_account("acc_1"))
    with uow.transaction() as u:
        u.connections.add(make_connection("acc_1", "con_1", 1))
    with pytest.raises(ValidationError):
        with uow.transaction() as u:
            u.connections.add(make_connection("acc_1", "con_2", 2))


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


def test_conversation_participants_and_open_lookup(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        found = u.conversations.find_open_direct(participant_key("acc_a", "acc_b"))
        reverse = u.conversations.find_open_direct(participant_key("acc_b", "acc_a"))
        assert u.conversations.is_participant(conversation.conversation_id, "acc_a")
        assert not u.conversations.is_participant(conversation.conversation_id, "acc_stranger")
        roles = {p.account_id: p.role for p in u.conversations.participants(conversation.conversation_id)}
    assert found is not None and found.conversation_id == conversation.conversation_id
    assert reverse is not None, "参与者顺序不应影响查找"
    assert roles == {"acc_a": ParticipantRole.INITIATOR, "acc_b": ParticipantRole.PEER}


def test_unread_counter_and_mark_read(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        u.conversations.bump_unread(conversation.conversation_id, ["acc_b"], 3)
    with uow.transaction() as u:
        assert u.messages.unread_count(conversation.conversation_id, "acc_b") == 3
        u.conversations.mark_read(
            conversation.conversation_id, "acc_b", last_read_message_id="msg_1", unread_count=0
        )
    with uow.transaction() as u:
        assert u.messages.unread_count(conversation.conversation_id, "acc_b") == 0
        participant = u.conversations.participant(conversation.conversation_id, "acc_b")
    assert participant is not None and participant.last_read_message_id == "msg_1"


def test_auto_turn_counter_never_negative(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        assert u.conversations.bump_auto_turn(conversation.conversation_id, 1) == 1
        assert u.conversations.bump_auto_turn(conversation.conversation_id, 1) == 2
        assert u.conversations.bump_auto_turn(conversation.conversation_id, -5) == 0


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


def _message(message_id: str, conversation_id: str, sender: str, recipient: str, text: str, *, key: str | None = None, created=START) -> Message:
    return Message(
        message_id=message_id,
        conversation_id=conversation_id,
        sender_account_id=sender,
        recipient_account_id=recipient,
        content=text,
        content_type=ContentType.TEXT,
        content_hash=content_hash(text),
        idempotency_key=key,
        created_at=created,
    )


def test_message_pagination_with_stable_cursor(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        for index in range(5):
            u.messages.add(
                _message(
                    f"msg_{index:03d}",
                    conversation.conversation_id,
                    "acc_a",
                    "acc_b",
                    f"第 {index} 条",
                    created=START + timedelta(seconds=index),
                )
            )
    with uow.transaction() as u:
        page1 = u.messages.list_for_conversation(conversation.conversation_id, limit=2)
    assert [v.message.message_id for v in page1.items] == ["msg_000", "msg_001"]
    assert page1.next_cursor == "msg_001"
    with uow.transaction() as u:
        page2 = u.messages.list_for_conversation(
            conversation.conversation_id, after_message_id=page1.next_cursor, limit=2
        )
    assert [v.message.message_id for v in page2.items] == ["msg_002", "msg_003"]
    with uow.transaction() as u:
        page3 = u.messages.list_for_conversation(
            conversation.conversation_id, after_message_id=page2.next_cursor, limit=2
        )
    assert [v.message.message_id for v in page3.items] == ["msg_004"]
    assert page3.next_cursor is None


def test_unknown_message_cursor_rejected(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with pytest.raises(ValidationError):
        with uow.transaction() as u:
            u.messages.list_for_conversation(
                conversation.conversation_id, after_message_id="msg_nope"
            )


def test_idempotency_lookup_returns_original(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        u.messages.add(
            _message("msg_1", conversation.conversation_id, "acc_a", "acc_b", "只发一次", key="k1")
        )
    with uow.transaction() as u:
        found = u.messages.find_by_idempotency_key(sender_account_id="acc_a", idempotency_key="k1")
        other_sender = u.messages.find_by_idempotency_key(
            sender_account_id="acc_b", idempotency_key="k1"
        )
    assert found is not None and found.message_id == "msg_1"
    assert other_sender is None, "幂等键只在同一发送者内唯一"


def test_message_view_reports_three_dimensions(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        u.messages.add(_message("msg_1", conversation.conversation_id, "acc_a", "acc_b", "正文"))
        u.deliveries.add(
            DeliveryRecord(
                delivery_id="del_1",
                message_id="msg_1",
                account_id="acc_b",
                conversation_id=conversation.conversation_id,
                state=DeliveryState.DELIVERED,
                created_at=START,
                updated_at=START,
            )
        )
    with uow.transaction() as u:
        page = u.messages.list_for_conversation(conversation.conversation_id, viewer_account_id="acc_b")
    view = page.items[0]
    assert view.delivery is DeliveryState.DELIVERED
    assert view.visibility.value == "unread"
    assert view.processing.value == "pending"
    payload = view.to_dict()
    assert payload["delivery"] == "delivered" and payload["processing"] == "pending"


def test_reply_preserves_reply_to(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    first = _message("msg_1", conversation.conversation_id, "acc_a", "acc_b", "问题")
    second = _message("msg_2", conversation.conversation_id, "acc_b", "acc_a", "答复", created=START + timedelta(seconds=1))
    second.reply_to = "msg_1"
    with uow.transaction() as u:
        u.messages.add(first)
        u.messages.add(second)
    with uow.transaction() as u:
        page = u.messages.list_for_conversation(conversation.conversation_id)
    assert page.items[1].message.reply_to == "msg_1"


def test_recent_content_hashes_for_loop_detection(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        for index in range(3):
            u.messages.add(
                _message(
                    f"msg_{index}",
                    conversation.conversation_id,
                    "acc_a",
                    "acc_b",
                    "同样的内容",
                    created=START + timedelta(seconds=index),
                )
            )
    with uow.transaction() as u:
        hashes = u.messages.recent_content_hashes(conversation.conversation_id)
        turns = u.messages.count_auto_turns(conversation.conversation_id, since=START)
    assert len(set(hashes)) == 1, "同内容哈希相同"
    assert turns == 0, "非自动生成的消息不计入自动轮数"


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def _seed_delivery(uow, state: DeliveryState, *, delivery_id: str = "del_1", next_attempt_at=None, account_id="acc_b") -> None:
    with uow.transaction() as u:
        u.deliveries.add(
            DeliveryRecord(
                delivery_id=delivery_id,
                message_id="msg_1",
                account_id=account_id,
                conversation_id="conv_1",
                state=state,
                created_at=START,
                updated_at=START,
                next_attempt_at=next_attempt_at,
            )
        )


def _prepare_message(uow) -> None:
    a, b = make_account("acc_a"), make_account("acc_b")
    seed_accounts(uow, a, b)
    conversation = seed_direct_conversation(uow, a, b)
    with uow.transaction() as u:
        u.messages.add(_message("msg_1", conversation.conversation_id, "acc_a", "acc_b", "投递测试"))


def test_claim_for_dispatch_is_exclusive(uow) -> None:
    _prepare_message(uow)
    _seed_delivery(uow, DeliveryState.QUEUED)
    with uow.transaction() as u:
        assert u.deliveries.claim_for_dispatch("del_1", at=START) is True
    with uow.transaction() as u:
        assert u.deliveries.claim_for_dispatch("del_1", at=START) is False, "第二次抢占必须失败"
    with uow.transaction() as u:
        delivery = u.deliveries.get("del_1")
    assert delivery is not None
    assert delivery.state is DeliveryState.DISPATCHED
    assert delivery.attempt == 1


def test_due_for_dispatch_respects_backoff(uow) -> None:
    _prepare_message(uow)
    _seed_delivery(uow, DeliveryState.FAILED, next_attempt_at=START + timedelta(seconds=30))
    with uow.transaction() as u:
        assert u.deliveries.due_for_dispatch(now=START) == []
        assert len(u.deliveries.due_for_dispatch(now=START + timedelta(seconds=31))) == 1


@pytest.mark.parametrize("state", [DeliveryState.QUEUED, DeliveryState.FAILED])
def test_stale_dispatch_candidate_cannot_claim_before_backoff(uow, state) -> None:
    """另一派发器拿到旧候选后，也不能抢占已被延期的投递。"""
    _prepare_message(uow)
    _seed_delivery(uow, state, next_attempt_at=START + timedelta(seconds=30))
    with uow.transaction() as u:
        assert u.deliveries.claim_for_dispatch("del_1", at=START) is False
    with uow.transaction() as u:
        delivery = u.deliveries.get("del_1")
        assert delivery.state is state
        assert delivery.attempt == 0
        assert u.deliveries.claim_for_dispatch("del_1", at=START + timedelta(seconds=30)) is True


def test_delivery_unique_per_message_and_account(uow) -> None:
    _prepare_message(uow)
    _seed_delivery(uow, DeliveryState.QUEUED)
    with pytest.raises(ValidationError):
        _seed_delivery(uow, DeliveryState.QUEUED, delivery_id="del_2")


def test_pending_for_account_keeps_order(uow) -> None:
    _prepare_message(uow)
    _seed_delivery(uow, DeliveryState.QUEUED)
    with uow.transaction() as u:
        pending = u.deliveries.pending_for_account("acc_b")
    assert [d.delivery_id for d in pending] == ["del_1"]


# ---------------------------------------------------------------------------
# 事务与审计
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_error(uow) -> None:
    with pytest.raises(RuntimeError):
        with uow.transaction() as u:
            u.accounts.add(make_account("acc_1"))
            raise RuntimeError("业务失败")
    with uow.transaction() as u:
        assert u.accounts.get("acc_1") is None


def test_nested_transaction_uses_outer_commit(uow) -> None:
    with uow.transaction() as outer:
        outer.accounts.add(make_account("acc_1"))
        with uow.transaction() as inner:
            inner.accounts.add(make_account("acc_2"))
    with uow.transaction() as u:
        assert u.accounts.get("acc_1") is not None
        assert u.accounts.get("acc_2") is not None


def test_audit_events_are_append_only(uow) -> None:
    with uow.transaction() as u:
        u.audit.append(event_type="account.registered", created_at=START, account_id="acc_1")
        u.audit.append(
            event_type="delivery.dispatched",
            created_at=START,
            delivery_id="del_1",
            detail={"attempt": 1},
        )
    with uow.transaction() as u:
        rows = u.audit.recent(limit=10)
        dispatched = u.audit.recent(event_type="delivery.dispatched")
    assert len(rows) == 2
    assert len(dispatched) == 1
    assert dispatched[0]["delivery_id"] == "del_1"


def test_wal_allows_concurrent_readers(db_path) -> None:
    """WAL 下第二个连接可以并发读，写入后可见。"""
    from mcp_agent_mailbox.infrastructure.sqlite import Database, apply_migrations

    from conftest import UnitOfWorkFactory

    writer = Database(db_path)
    apply_migrations(writer.connection())
    reader = Database(db_path)
    reader.connection()  # 触发 pragma

    factory = UnitOfWorkFactory(writer)
    with factory.transaction() as u:
        u.accounts.add(make_account("acc_1"))
    # 读连接不阻塞，并且能看到已提交数据。
    assert reader.connection().execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    writer.close()
    reader.close()
