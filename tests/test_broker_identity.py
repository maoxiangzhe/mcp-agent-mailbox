"""阶段 2 集成测试：注册、重连、代次、在线租约、身份绑定。

全部使用隔离临时目录，绝不写真实 ``~/.dsh`` / ``~/.codex`` / 真实邮箱数据库。
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from mcp_agent_mailbox.config import DeliveryPolicy, RateLimits, Settings, load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import ConnectionState, HostIdentity
from mcp_agent_mailbox.domain.errors import IdentityMismatchError, NotFoundError
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresencePolicy, PresenceState
from mcp_agent_mailbox.ports.clock import FrozenClock
from mcp_agent_mailbox.ports.event_transport import Event


@pytest.fixture
def settings(tmp_path) -> Settings:
    return load_settings(
        data_dir=tmp_path / "mailbox",
        presence=PresencePolicy(heartbeat_interval_seconds=20, lease_seconds=60, grace_seconds=10),
        rate_limits=RateLimits(),
        delivery=DeliveryPolicy(),
    )


@pytest.fixture
def clock() -> FrozenClock:
    from mcp_agent_mailbox.domain.timestamps import utc_now

    return FrozenClock(utc_now())


@pytest.fixture
def broker(settings, clock) -> Broker:
    instance = Broker(settings, clock=clock)
    try:
        yield instance
    finally:
        instance.stop()


def bind(broker: Broker, *, session: str, host: str = "dsh", name: str | None = None, level=HostCapabilityLevel.WAKE):
    return broker.accounts.bind_session(
        HostIdentity(host, "desktop-default", session),
        display_name=name or f"{host} / {session}",
        capability_level=level,
        adapter_name=f"{host}-adapter",
    )


def bind_with_host_pid(
    broker: Broker, *, session: str, host_pid: int, level=HostCapabilityLevel.WAKE
):
    """绑定连接并显式声明托管进程 PID。

    产品规则是"在线严格参照进程"，所以测试要能明确表达"托管进程还在"（用本进程 PID）
    或"进程已退出"（用一个不可能存在的 PID），而不是依赖运行时巧合。
    """
    bound = bind(broker, session=session, level=level)
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(bound.connection_id)
        assert connection is not None
        connection.host_pid = host_pid
        uow.connections.update(connection)
    return bound


DEAD_PID = 2_147_483_646  # 不可能存在的 PID，用来表示"托管进程已退出"


# ---------------------------------------------------------------------------
# 注册与重连
# ---------------------------------------------------------------------------


def test_bind_session_registers_account(broker) -> None:
    session = bind(broker, session="session-a")
    assert session.created is True
    assert session.account.account_id.startswith("acc_")
    assert session.generation == 1
    assert session.account.host_type == "dsh"
    assert session.account.native_session_id == "session-a"
    assert session.account.address == "dsh:dsh / session-a@desktop-default"


def test_reconnect_restores_same_account_and_bumps_generation(broker) -> None:
    first = bind(broker, session="session-a")
    second = bind(broker, session="session-a")
    assert second.account.account_id == first.account.account_id, "重连必须恢复原账号"
    assert second.created is False
    assert second.generation == 2, "重连产生新代次"

    with broker.unit_of_work().transaction() as uow:
        old = uow.connections.get(first.connection_id)
        current = uow.connections.current_for_account(first.account_id)
    assert old is not None
    assert old.state is ConnectionState.SUPERSEDED
    assert old.is_current is False
    assert current is not None and current.connection_id == second.connection_id


def test_same_session_id_in_different_hosts_are_distinct_accounts(broker) -> None:
    dsh = bind(broker, session="session-1", host="dsh")
    codex = bind(broker, session="session-1", host="codex")
    assert dsh.account_id != codex.account_id


def test_register_is_idempotent_for_account_row(broker) -> None:
    for _ in range(3):
        bind(broker, session="session-a")
    with broker.unit_of_work().transaction() as uow:
        accounts = uow.accounts.list_accounts()
        connections = uow.connections.list_for_account(accounts[0].account_id, include_closed=True)
    assert len(accounts) == 1
    assert len(connections) == 3
    assert [c.generation for c in connections] == [3, 2, 1]


def test_register_updates_display_name_and_keeps_account(broker) -> None:
    first = bind(broker, session="session-a", name="旧名字")
    second = bind(broker, session="session-a", name="新名字")
    assert second.account_id == first.account_id
    assert second.account.display_name == "新名字"
    with broker.unit_of_work().transaction() as uow:
        renamed = uow.audit.recent(event_type="account.renamed")
        registrations = uow.audit.recent(event_type="account.registered")
    assert len(renamed) == 1
    assert renamed[0]["account_id"] == first.account_id
    renamed_detail = json.loads(renamed[0]["detail_json"])
    assert renamed_detail["from"] == "旧名字"
    assert renamed_detail["to"] == "新名字"
    assert renamed_detail["native_session_id"] == "session-a"
    assert json.loads(registrations[0]["detail_json"])["registration"] == "restored"
    assert json.loads(registrations[-1]["detail_json"])["registration"] == "new"


def test_offline_sweep_preserves_contact_and_reconnect_restores_backlog(broker) -> None:
    from mcp_agent_mailbox.domain.messages import DeliveryState

    sender = bind_with_host_pid(broker, session="sender", host_pid=os.getpid())
    recipient = bind_with_host_pid(broker, session="recipient", host_pid=DEAD_PID)
    assert broker.accounts.sweep_disconnected_connections() == [recipient.connection_id]
    contacts = broker.conversations.list_contacts(connection_id=sender.connection_id)
    assert recipient.account_id in {contact.account_id for contact in contacts}
    sent = broker.conversations.start_conversation(
        connection_id=sender.connection_id, to_account_id=recipient.account_id,
        text="离线期间的来信",
    )
    assert broker.deliveries.dispatch_due().skipped_offline == 1
    restored = bind_with_host_pid(broker, session="recipient", host_pid=os.getpid())
    assert restored.account_id == recipient.account_id
    assert restored.generation == recipient.generation + 1
    page = broker.conversations.read_conversation(
        connection_id=restored.connection_id, conversation_id=sent.conversation_id,
        mark_seen=False,
    )
    assert [view.message.message_id for view in page.messages] == [sent.message.message_id]
    assert broker.deliveries.dispatch_due().dispatched == 1
    with broker.unit_of_work().transaction() as uow:
        accounts = uow.accounts.list_accounts()
        delivery = uow.deliveries.get(sent.delivery_id)
    assert len(accounts) == 2
    assert delivery.state is DeliveryState.DISPATCHED


def test_register_rejects_blank_identity(broker) -> None:
    with pytest.raises(ValueError):
        HostIdentity("dsh", "desktop-default", "  ")


# ---------------------------------------------------------------------------
# 在线状态：首要依据是"托管进程是否活着"
# ---------------------------------------------------------------------------


def test_account_online_while_host_process_alive(broker) -> None:
    """产品规则：托管进程活着，账号就在线——与租约无关。"""
    session = bind_with_host_pid(
        broker, session="session-a", host_pid=os.getpid(), level=HostCapabilityLevel.TOOLS_ONLY
    )
    view = broker.presence.for_account(session.account_id)
    assert view.state is PresenceState.CONNECTED
    assert view.host_pid == os.getpid()


def test_account_offline_once_host_process_exits(broker) -> None:
    """托管进程退出，账号立即离线（不看租约剩多少）。"""
    session = bind_with_host_pid(broker, session="session-a", host_pid=DEAD_PID)
    view = broker.presence.for_account(session.account_id)
    assert view.state is PresenceState.OFFLINE
    assert view.host_pid == DEAD_PID


def test_long_thinking_session_stays_online_past_lease(broker, clock) -> None:
    """会话思考 10 分钟没发心跳，只要进程还活着就必须一直在线。"""
    session = bind_with_host_pid(
        broker, session="session-a", host_pid=os.getpid(), level=HostCapabilityLevel.TOOLS_ONLY
    )
    clock.advance(600)
    assert broker.presence.for_account(session.account_id).state is PresenceState.CONNECTED


def test_level2_connection_is_online_and_wakeable(broker) -> None:
    """Level 2 只影响"能不能直接投进去开工"，不影响在线与否。"""
    session = bind_with_host_pid(
        broker, session="session-a", host_pid=os.getpid(), level=HostCapabilityLevel.WAKE
    )
    view = broker.presence.for_account(session.account_id)
    assert view.state is PresenceState.CONNECTED
    assert view.is_online is True
    assert view.can_wake is True


@pytest.mark.parametrize("level", [HostCapabilityLevel.TOOLS_ONLY, HostCapabilityLevel.NOTIFY])
def test_below_level2_is_online_but_not_wakeable(broker, level) -> None:
    """MCP 连上了不代表可以被唤醒：在线（进程活着），但不能被注入。"""
    session = bind_with_host_pid(
        broker, session="session-a", host_pid=os.getpid(), level=level
    )
    view = broker.presence.for_account(session.account_id)
    assert view.state is PresenceState.CONNECTED
    assert view.can_receive is True
    assert view.can_wake is False, "级别不够就不能启动回合"


def test_presence_without_process_info_is_offline(broker, clock) -> None:
    """拿不到进程信息 = 离线：不再退回租约去猜。"""
    session = bind(broker, session="session-a")
    _drop_process_and_durability(broker, session.connection_id)
    assert broker.presence.for_account(session.account_id).state is PresenceState.OFFLINE
    clock.advance(10_000)
    assert broker.presence.for_account(session.account_id).state is PresenceState.OFFLINE


def _drop_process_and_durability(broker: Broker, connection_id: str) -> None:
    """把连接改成"既没有进程信息、也不持久可达"，用来单独验证租约判定路径。"""
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(connection_id)
        assert connection is not None
        connection.host_pid = None
        connection.durable = False
        uow.connections.update(connection)


def test_renew_keeps_connection_active(broker, clock) -> None:
    session = bind(broker, session="session-a")
    clock.advance(50)
    broker.accounts.renew(session.connection_id)
    clock.advance(30)
    # 在线只看进程：续租不会把连接变成"更在线"，进程活着就一直是在线。
    assert broker.presence.for_account(session.account_id).state is PresenceState.CONNECTED


def test_renew_rejects_closed_connection(broker) -> None:
    session = bind(broker, session="session-a")
    broker.accounts.close_connection(session.connection_id)
    with pytest.raises(IdentityMismatchError):
        broker.accounts.renew(session.connection_id)


def test_sweep_recycles_only_dead_host_processes(broker, clock) -> None:
    alive = bind_with_host_pid(
        broker, session="session-alive", host_pid=os.getpid(), level=HostCapabilityLevel.TOOLS_ONLY
    )
    dead = bind_with_host_pid(
        broker, session="session-dead", host_pid=DEAD_PID, level=HostCapabilityLevel.TOOLS_ONLY
    )
    clock.advance(600)

    expired = broker.accounts.sweep_disconnected_connections()

    assert expired == [dead.connection_id], "只回收托管进程已退出的连接"
    with broker.unit_of_work().transaction() as uow:
        alive_row = uow.connections.get(alive.connection_id)
        dead_row = uow.connections.get(dead.connection_id)
    assert alive_row is not None and alive_row.state is ConnectionState.ACTIVE
    assert dead_row is not None and dead_row.state is ConnectionState.EXPIRED
    assert broker.presence.for_account(alive.account_id).state is PresenceState.CONNECTED
    assert broker.presence.for_account(dead.account_id).state is PresenceState.OFFLINE


def test_sweep_recycles_connection_without_process_info(broker, clock) -> None:
    """没有进程信息就等于离线：即使租约看着还没过期也照样回收。"""
    session = bind(broker, session="session-a")
    _drop_process_and_durability(broker, session.connection_id)
    clock.advance(1)
    expired = broker.accounts.sweep_disconnected_connections()
    assert expired == [session.connection_id]
    with broker.unit_of_work().transaction() as uow:
        connection = uow.connections.get(session.connection_id)
    assert connection is not None and connection.state is ConnectionState.EXPIRED
    assert broker.presence.for_account(session.account_id).state is PresenceState.OFFLINE


def test_maintenance_recycles_dead_host_process(broker, clock) -> None:
    bind_with_host_pid(broker, session="session-a", host_pid=DEAD_PID)
    clock.advance(120)
    report = broker.maintenance_once()
    assert report.expired_connections == 1
    assert report.rounds == 1


def test_close_connection_makes_account_offline(broker) -> None:
    session = bind(broker, session="session-a")
    broker.accounts.close_connection(session.connection_id)
    assert broker.presence.for_account(session.account_id).state is PresenceState.OFFLINE


def test_close_connection_is_idempotent(broker) -> None:
    session = bind(broker, session="session-a")
    broker.accounts.close_connection(session.connection_id)
    again = broker.accounts.close_connection(session.connection_id)
    assert again.state is ConnectionState.CLOSED


# ---------------------------------------------------------------------------
# 身份绑定与安全边界
# ---------------------------------------------------------------------------


def test_account_for_connection_resolves_sender(broker) -> None:
    session = bind(broker, session="session-a")
    with broker.unit_of_work().transaction() as uow:
        account = broker.accounts.account_for_connection(uow, session.connection_id)
    assert account.account_id == session.account_id


def test_old_generation_cannot_act_as_current(broker) -> None:
    first = bind(broker, session="session-a")
    second = bind(broker, session="session-a")
    with broker.unit_of_work().transaction() as uow:
        with pytest.raises(IdentityMismatchError):
            broker.accounts.require_current_connection(uow, first.connection_id)
        current = broker.accounts.require_current_connection(uow, second.connection_id)
    assert current.connection_id == second.connection_id


def test_unknown_connection_is_not_found(broker) -> None:
    with broker.unit_of_work().transaction() as uow:
        with pytest.raises(NotFoundError):
            broker.accounts.account_for_connection(uow, "con_missing")


def test_blocked_account_cannot_send(broker) -> None:
    from mcp_agent_mailbox.domain.errors import ValidationError

    session = bind(broker, session="session-a")
    with broker.unit_of_work().transaction() as uow:
        account = uow.accounts.get(session.account_id)
        assert account is not None
        account.blocked = True
        uow.accounts.update(account)
    with broker.unit_of_work().transaction() as uow:
        with pytest.raises(ValidationError):
            broker.accounts.account_for_connection(uow, session.connection_id)


# ---------------------------------------------------------------------------
# 事件通道
# ---------------------------------------------------------------------------


def test_bind_session_registers_event_subscription(broker) -> None:
    session = bind(broker, session="session-a")
    subscribers = broker.events.subscribers()
    assert len(subscribers) == 1
    assert subscribers[0].connection_id == session.connection_id
    assert subscribers[0].can_wake is True


def test_event_reaches_current_connection_only(broker) -> None:
    first = bind(broker, session="session-a")
    second = bind(broker, session="session-a")
    broker.events.publish(
        Event(
            type="presence/changed",
            account_id=second.account_id,
            created_at=broker.clock.now(),
            payload={"presence": "realtime"},
        )
    )
    assert broker.events.receive(first.connection_id, 0.01) is None
    received = broker.events.receive(second.connection_id, 0.01)
    assert received is not None and received.type == "presence/changed"


def test_stale_generation_event_is_dropped(broker) -> None:
    session = bind(broker, session="session-a")
    broker.events.publish(
        Event(
            type="mailbox/message",
            account_id=session.account_id,
            created_at=broker.clock.now(),
            generation=session.generation + 5,
            payload={"delivery_id": "del_x"},
        )
    )
    assert broker.events.receive(session.connection_id, 0.01) is None


def test_close_connection_unregisters_subscription(broker) -> None:
    session = bind(broker, session="session-a")
    broker.accounts.close_connection(session.connection_id)
    assert broker.events.subscribers() == []


def test_publish_without_subscriber_is_safe(broker) -> None:
    broker.events.publish(
        Event(
            type="presence/changed",
            account_id="acc_nobody",
            created_at=broker.clock.now(),
        )
    )


# ---------------------------------------------------------------------------
# 联系人策略
# ---------------------------------------------------------------------------


def test_contact_policy_blocks_and_allows(broker) -> None:
    from mcp_agent_mailbox.domain.errors import RecipientBlockedError

    a = bind(broker, session="session-a")
    b = bind(broker, session="session-b")
    broker.accounts.set_contact_policy(a.account_id, b.account_id, "block", note="别烦我")
    with broker.unit_of_work().transaction() as uow:
        with pytest.raises(RecipientBlockedError):
            broker.accounts.assert_contact_allowed(uow, a.account_id, b.account_id)
        with pytest.raises(RecipientBlockedError):
            broker.accounts.assert_contact_allowed(uow, b.account_id, a.account_id)

    broker.accounts.set_contact_policy(a.account_id, b.account_id, "allow")
    with broker.unit_of_work().transaction() as uow:
        broker.accounts.assert_contact_allowed(uow, a.account_id, b.account_id)


def test_contact_policy_requires_existing_accounts(broker) -> None:
    a = bind(broker, session="session-a")
    with pytest.raises(NotFoundError):
        broker.accounts.set_contact_policy(a.account_id, "acc_missing", "allow")


def test_contact_policy_rejects_self_and_bad_value(broker) -> None:
    from mcp_agent_mailbox.domain.errors import ValidationError

    a = bind(broker, session="session-a")
    b = bind(broker, session="session-b")
    with pytest.raises(ValidationError):
        broker.accounts.set_contact_policy(a.account_id, a.account_id, "allow")
    with pytest.raises(ValidationError):
        broker.accounts.set_contact_policy(a.account_id, b.account_id, "maybe")
