"""宿主适配器契约测试。

契约来自设计文档 §15.3。这里**不依赖真实宿主**：用注入的假宿主 CLI 走真实代码路径，
验证"调用形状、去重、状态回传、权限不提升"这些能自动化验证的部分。

诚实性边界（很重要）：

- DSH 适配器：断言它**必须**如实返回 unsupported，不许伪造 Level 2。
- Codex 适配器：断言它用公开 CLI（queue / exec resume）完成注入，且
  ``verified=False``（未在真实 Codex 上端到端验证）。
- 这些测试证明"契约被遵守"，**不**证明"真实宿主已经能唤醒"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from mcp_agent_mailbox.adapters.base import CommandResult
from mcp_agent_mailbox.adapters.codex import (
    CodexAdapter,
    CodexSession,
    parse_session_listing,
    probe_codex,
)
from mcp_agent_mailbox.adapters.dsh import DshAdapter, level2_guidance, probe_dsh
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel
from mcp_agent_mailbox.ports.host_adapter import ExternalEnvelope, SessionBinding


@dataclass
class FakeRunner:
    """假宿主 CLI：记录每次调用，按脚本返回结果。

    用它可以验证"适配器到底调了什么命令"，而不会真的去唤醒宿主或改宿主配置。
    """

    responses: dict[str, CommandResult] = field(default_factory=dict)
    default: CommandResult = field(
        default_factory=lambda: CommandResult(("fake",), 0, "", "")
    )
    calls: list[tuple[str, ...]] = field(default_factory=list)
    programs: dict[str, str] = field(default_factory=dict)

    def run(self, argv, *, timeout: float = 30.0) -> CommandResult:
        self.calls.append(tuple(argv))
        key = " ".join(argv[:2])
        if key in self.responses:
            return self.responses[key]
        if argv and argv[0] in self.responses:
            return self.responses[argv[0]]
        return self.default

    def which(self, program: str) -> str | None:
        return self.programs.get(program)

    def called(self, *fragments: str) -> bool:
        return any(all(fragment in part for fragment in fragments) for part in self.calls)


def ok(argv=("fake",), stdout="") -> CommandResult:
    return CommandResult(tuple(argv), 0, stdout, "", False)


def fail(argv=("fake",), code=1, stderr="boom") -> CommandResult:
    return CommandResult(tuple(argv), code, "", stderr, False)


@pytest.fixture
def codex_cli() -> FakeRunner:
    runner = FakeRunner(programs={"codex": "C:/fake/codex.exe"})
    runner.responses["C:/fake/codex.exe --help"] = ok(
        stdout="Commands:\n  queue   Queue a message for an existing session\n"
        "  exec    Run Codex non-interactively\n  resume  Resume a previous session\n"
        "  agents  Browse agent sessions\n"
    )
    return runner


def envelope() -> ExternalEnvelope:
    return ExternalEnvelope(
        from_address="dsh:测试@desktop-default",
        conversation_id="conv_1",
        message_id="msg_1",
        delivery_id="del_1",
    )


# ---------------------------------------------------------------------------
# 契约 1：能恢复稳定账号（适配器不负责生成 ID，但必须能提供稳定的会话绑定）
# ---------------------------------------------------------------------------


def test_dsh_adapter_requires_injected_session_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILBOX_SESSION_ID", raising=False)
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    runner = FakeRunner()
    probe = probe_dsh(runner)
    assert probe.supported is False
    assert probe.level is HostCapabilityLevel.TOOLS_ONLY
    assert "MAILBOX_SESSION_ID" in probe.detail


def test_dsh_adapter_binding_available_when_env_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBOX_SESSION_ID", "session-abc")
    probe = probe_dsh(FakeRunner())
    assert probe.supported is True
    assert probe.level is HostCapabilityLevel.TOOLS_ONLY, "拿到会话 ID 也不等于能唤醒"


def test_codex_adapter_reports_wake_but_unverified(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    probe = adapter.probe()
    assert probe.supported is True
    assert probe.level is HostCapabilityLevel.WAKE
    assert adapter.capabilities.can_wake is True
    assert adapter.capabilities.verified is False, "未在真实 Codex 上验证，必须标为未验证"
    assert adapter.capabilities.can_receive_realtime is False


def test_codex_probe_degrades_when_cli_missing() -> None:
    runner = FakeRunner(programs={})
    probe = probe_codex(runner, "codex")
    assert probe.supported is False
    assert probe.level is HostCapabilityLevel.TOOLS_ONLY
    assert "未找到" in probe.detail


def test_codex_probe_degrades_when_subcommands_missing() -> None:
    runner = FakeRunner(programs={"codex": "codex"})
    runner.responses["codex --help"] = ok(stdout="Commands:\n  login\n  logout\n")
    probe = probe_codex(runner, "codex")
    assert probe.supported is False
    assert "queue" in probe.detail


# ---------------------------------------------------------------------------
# 契约 2/6：不提升权限、注入内容带来源信封
# ---------------------------------------------------------------------------


def test_injected_text_carries_trust_envelope(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    result = adapter.inject(
        {"native_session_id": "11111111-2222-3333-4444-555555555555"},
        envelope(),
        "正文内容",  # 故意写成像指令的正文
    )
    assert result.outcome == "injected"
    call = next(part for part in codex_cli.calls if "queue" in part)
    text = call[call.index("--message") + 1]
    assert text.startswith("[External agent message]")
    assert "From: dsh:测试@desktop-default" in text
    assert "Trust: untrusted peer content" in text
    assert "正文内容" in text
    # 信封里不得出现任何权限/沙箱声明。
    lowered = text.lower()
    for forbidden in ("sandbox_permissions", "danger-full-access", "approval", "bypass"):
        assert forbidden not in lowered, f"注入文本出现了权限相关字样：{forbidden}"


def test_inject_does_not_pass_sandbox_or_approval_flags(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    adapter.inject({"native_session_id": "session-1"}, envelope(), "hi")
    for call in codex_cli.calls:
        joined = " ".join(call)
        assert "--dangerously-bypass-approvals-and-sandbox" not in joined
        assert "--approve-for-me" not in joined
        assert "-s" not in call
        assert "--sandbox" not in call


# ---------------------------------------------------------------------------
# 契约 3/4：注入指定会话、唤醒空闲会话
# ---------------------------------------------------------------------------


def test_inject_targets_the_named_session(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    adapter.inject({"native_session_id": session_id}, envelope(), "hello")
    assert codex_cli.called("queue", session_id)


def test_cold_session_falls_back_to_exec_resume(codex_cli: FakeRunner) -> None:
    """queue 失败（会话不在运行）时退回 exec resume，这是冷会话的唤醒路径。"""
    codex_cli.responses["C:/fake/codex.exe queue"] = fail(stderr="no live session")
    adapter = CodexAdapter(runner=codex_cli)
    result = adapter.inject(
        {"native_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}, envelope(), "cold"
    )
    assert result.outcome == "injected"
    assert result.wake_requested is True
    assert codex_cli.called("exec", "resume")


def test_inject_without_session_id_fails(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    result = adapter.inject({}, envelope(), "no target")
    assert result.outcome == "failed"
    assert "native_session_id" in result.detail


def test_inject_reports_failure_when_both_paths_fail(codex_cli: FakeRunner) -> None:
    codex_cli.responses["C:/fake/codex.exe queue"] = fail(stderr="queue failed")
    codex_cli.responses["C:/fake/codex.exe exec"] = fail(stderr="resume failed")
    adapter = CodexAdapter(runner=codex_cli)
    result = adapter.inject({"native_session_id": "s"}, envelope(), "x")
    assert result.outcome == "failed"
    assert "queue failed" in result.detail
    assert result.outcome != "injected", "失败绝不能谎报为已投递"


# ---------------------------------------------------------------------------
# 契约 5：重复投递去重（由 delivery_id 语义保证）
# ---------------------------------------------------------------------------


def test_repeated_injection_is_left_to_delivery_id_dedup(codex_cli: FakeRunner) -> None:
    """适配器每次都用同一个 delivery_id 的语义由邮箱保证去重。

    这里固化"信封里必须带上 delivery_id"，接收方才能按它去重；
    同时确认重复注入不会因为适配器自己擦掉标识而失去去重能力。
    """
    adapter = CodexAdapter(runner=codex_cli)
    for _ in range(2):
        adapter.inject({"native_session_id": "s"}, envelope(), "同一条消息")
    calls = [part for part in codex_cli.calls if "queue" in part]
    assert len(calls) == 2
    # 两次注入的文本完全一致（含 delivery_id），接收方能据此判断是否已处理。
    texts = [call[call.index("--message") + 1] for call in calls]
    assert texts[0] == texts[1]
    assert "del_1" not in texts[0]  # 信封不暴露内部投递 ID 给模型正文
    assert "[External agent message]" in texts[0]


# ---------------------------------------------------------------------------
# 契约 7：取消、失败和完成状态准确回传
# ---------------------------------------------------------------------------


def test_codex_wake_reports_confirmed_cli_only(codex_cli: FakeRunner) -> None:
    adapter = CodexAdapter(runner=codex_cli)
    wake = adapter.wake("con_1")
    assert wake.requested is True
    assert wake.started is False, "只确认 CLI 可用，不能声称已经启动回合"
    assert "queue" in wake.detail


def test_codex_wake_without_cli_is_unsupported() -> None:
    adapter = CodexAdapter(runner=FakeRunner(programs={}))
    wake = adapter.wake("con_1")
    assert wake.requested is False
    assert wake.started is False
    assert wake.unsupported_reason


# ---------------------------------------------------------------------------
# DSH：必须如实降级，不许伪造 Level 2
# ---------------------------------------------------------------------------


def test_dsh_inject_is_unsupported_not_failed() -> None:
    """unsupported 与 failed 语义不同：前者不该消耗重试次数。"""
    adapter = DshAdapter(runner=FakeRunner())
    result = adapter.inject({"native_session_id": "s"}, envelope(), "hello")
    assert result.outcome == "unsupported"
    assert result.is_success is False
    assert "取信" in result.detail or "dsh_wake" in result.detail


def test_dsh_wake_points_at_the_direct_channel() -> None:
    """适配器事件通道上的 wake 仍然不支持，但要说清正确入口在哪。"""
    adapter = DshAdapter(runner=FakeRunner())
    wake = adapter.wake("con_1")
    assert wake.requested is False
    assert wake.started is False
    assert wake.unsupported_reason
    assert "DshWebWaker" in wake.unsupported_reason, "要给出真正能用的入口"


def test_dsh_declares_no_level2_without_credentials() -> None:
    """没有凭据就没有注入通道：适配器**不谎报** Level 2。

    注：真实等级由 ``probe()`` 按事实判定（有凭据 -> Level 2）。
    """
    adapter = DshAdapter(runner=FakeRunner())
    assert adapter.capabilities.level is HostCapabilityLevel.TOOLS_ONLY
    assert adapter.capabilities.can_wake is False
    assert adapter.capabilities.can_receive_realtime is False


def test_dsh_probe_never_probes_writable_host_state() -> None:
    """探测必须是只读的：命令执行器记录里不应出现任何写操作。"""
    runner = FakeRunner(programs={"dsh": "dsh"})
    probe_dsh(runner)
    for call in runner.calls:
        joined = " ".join(call)
        for forbidden in ("session.v4.jsonl.zstd", "session_projcache", "sessions", "delete"):
            assert forbidden not in joined
    assert runner.calls == [], "当前实现不应执行任何 dsh 命令"


def test_level2_guidance_names_the_implemented_path() -> None:
    guidance = level2_guidance()
    assert guidance["target_level"] == 2
    assert guidance["status"] == "direct_channel_implemented"
    assert "session/prompt" in guidance["path"], "要写清真正可用的接口"
    assert any("禁止" in item for item in guidance["requirements"])


# ---------------------------------------------------------------------------
# 会话列表解析：解析不出来就返回空，不猜
# ---------------------------------------------------------------------------


def test_parse_session_listing_accepts_json_array() -> None:
    payload = json.dumps(
        [{"id": "11111111-1111-1111-1111-111111111111", "name": "任务 A", "cwd": "E:/x"}]
    )
    sessions = parse_session_listing(payload)
    assert sessions == [
        CodexSession(
            session_id="11111111-1111-1111-1111-111111111111", name="任务 A", cwd="E:/x", status=None
        )
    ]


def test_parse_session_listing_accepts_wrapped_object() -> None:
    payload = json.dumps({"sessions": [{"session_id": "s-1", "status": "idle"}]})
    sessions = parse_session_listing(payload)
    assert [session.session_id for session in sessions] == ["s-1"]
    assert sessions[0].status == "idle"


def test_parse_session_listing_returns_empty_for_garbage() -> None:
    assert parse_session_listing("not json at all") == []
    assert parse_session_listing("") == []


def test_list_sessions_does_not_invent_ids(codex_cli: FakeRunner) -> None:
    codex_cli.responses["C:/fake/codex.exe agents"] = ok(stdout="No sessions found.\n")
    adapter = CodexAdapter(runner=codex_cli)
    assert adapter.list_sessions() == []


def test_list_sessions_parses_uuid_rows(codex_cli: FakeRunner) -> None:
    codex_cli.responses["C:/fake/codex.exe agents"] = ok(
        stdout="  id                                    name\n"
        "  11111111-2222-3333-4444-555555555555  demo\n"
    )
    adapter = CodexAdapter(runner=codex_cli)
    sessions = adapter.list_sessions()
    assert [session.session_id for session in sessions] == [
        "11111111-2222-3333-4444-555555555555"
    ]


# ---------------------------------------------------------------------------
# 绑定结构（SessionBinding 必须来自宿主，不得伪造）
# ---------------------------------------------------------------------------


def test_session_binding_requires_host_supplied_ids() -> None:
    binding = SessionBinding(
        host_type="codex",
        host_instance_id="desktop",
        native_session_id="11111111-2222-3333-4444-555555555555",
        display_name="Codex / demo",
    )
    assert binding.native_session_id != ""
    assert binding.workspace_hint is None
