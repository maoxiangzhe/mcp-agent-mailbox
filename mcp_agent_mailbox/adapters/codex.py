"""Codex 宿主适配器。

使用 Codex 的**公开 CLI 子命令**，不碰内部数据库、不做 UI 自动化：

    codex agents                     列出本地 app-server 上的会话（发现可寻址 thread）
    codex queue --thread <id> 
               --message <text>      向已有会话排队一条消息
    codex exec resume <id> <prompt>  非交互地把消息交给已有会话并启动一个回合

能力等级：**Level 2（wake）**，但 ``verified=False``。

为什么是"声明但未验证"：本机没有可用的 Codex 会话来跑端到端唤醒，而任务要求
"不得声称已实现未经确认的宿主能力"。因此：

- 代码按公开 CLI 契约实现，并由契约测试用**注入的假 CLI**验证调用形状（参数、
  顺序、错误处理），这部分是真实覆盖；
- 文档、``whoami`` 与能力矩阵都标成未在真实宿主验证；
- 一旦在真实 Codex 上跑通端到端（发信 → 目标会话被唤醒 → 回复 → 原会话收到），
  才可以把 ``verified`` 改成 True。

关于实时接收：Codex 不会主动回调邮箱，所以 ``can_receive_realtime=False``。
消息有两条投递路径：

1. 目标会话正在 app-server 上运行 → ``codex queue`` 把消息排进它的队列；
2. 目标会话不在运行 → ``codex exec resume`` 启动一个新回合把消息交给它。

两条路径都由邮箱侧主动发起，因此账号在线状态仍由连接健康度决定，不能宣称
"Codex 会主动推消息给你"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..domain.presence import HostCapabilityLevel
from ..ports.host_adapter import ExternalEnvelope, InjectionResult, ProbeResult, WakeResult
from .base import BaseAdapter, CommandRunner, SubprocessRunner, existing_program, now_utc

__all__ = ["CodexAdapter", "CodexSession", "probe_codex"]

#: 探测与调用的超时。唤醒是异步的，所以这里只等"命令被接受"。
_QUEUE_TIMEOUT = 30.0
_RESUME_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class CodexSession:
    """Codex 侧一个可寻址的会话。"""

    session_id: str
    name: str | None = None
    cwd: str | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "status": self.status,
        }


@dataclass(slots=True)
class CodexAdapter(BaseAdapter):
    """Codex 适配器。"""

    name: str = "codex-adapter"
    host_type: str = "codex"
    declared_level: HostCapabilityLevel = HostCapabilityLevel.WAKE
    #: 未在真实 Codex 上验证过端到端唤醒，因此严格为 False。
    verified: bool = False
    #: Codex 不会主动回调邮箱，消息投递由邮箱侧主动发起。
    realtime_receive: bool = False
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    cli: str = "codex"
    notes: tuple[str, ...] = (
        "Level 2：通过公开 CLI（codex queue / codex exec resume）注入并唤醒。",
        "can_receive_realtime=False：Codex 不会主动回调邮箱，投递由邮箱侧发起。",
        "verified=False：契约测试覆盖调用形状，但未在真实 Codex 上完成端到端验证。",
        "禁止修改 Codex 内部数据库或会话文件，禁止 UI 自动化。",
    )

    # -- 探测 -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        return probe_codex(self.runner, self.cli)

    def _resolve_cli(self) -> str | None:
        return existing_program(self.cli, self.runner)

    # -- 会话发现 ---------------------------------------------------------

    def list_sessions(self, *, cwd: str | None = None) -> list[CodexSession]:
        """列出本地 app-server 上的会话。

        ``codex agents --help`` 说明它是交互式浏览界面，因此这里**只做尽力解析**：
        能解析出 UUID/名称就用，解析不出来就返回空列表并保持诚实——不猜。
        """
        cli = self._resolve_cli()
        if cli is None:
            return []
        argv = [cli, "agents"]
        if cwd:
            argv += ["-C", cwd]
        result = self.runner.run(argv, timeout=_QUEUE_TIMEOUT)
        if not result.ok:
            return []
        sessions: list[CodexSession] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 只接受"能找到 UUID 形态"的行，避免把表头当会话。
            candidate = _first_uuid_like(stripped)
            if candidate is None:
                continue
            sessions.append(CodexSession(session_id=candidate, name=None, cwd=cwd))
        return sessions

    # -- 注入 -------------------------------------------------------------

    def inject(
        self,
        delivery: dict[str, object],
        envelope: ExternalEnvelope,
        content: str,
    ) -> InjectionResult:
        """把消息交给目标 Codex 会话。

        先尝试 ``queue``（会话在跑时最自然），失败再退回 ``exec resume``（冷会话）。
        两者都失败返回 ``failed``，让邮箱按退避重试；不会谎报 delivered。
        """
        cli = self._resolve_cli()
        if cli is None:
            return InjectionResult(
                outcome="unsupported",
                detail=f"未找到 codex 命令行（{self.cli}）；请安装 Codex CLI 或配置 --codex-cli",
            )

        session_id = str(delivery.get("native_session_id") or "").strip()
        if not session_id:
            return InjectionResult(
                outcome="failed",
                detail="delivery 缺少 native_session_id，无法定位 Codex 会话",
            )

        text = self.render(envelope, content)
        queued = self.runner.run(
            [cli, "queue", "--thread", session_id, "--message", text], timeout=_QUEUE_TIMEOUT
        )
        if queued.ok:
            return InjectionResult(
                outcome="injected",
                detail="已通过 codex queue 排入目标会话队列",
                injected_at=now_utc(),
                wake_requested=False,
            )

        resumed = self.runner.run(
            [cli, "exec", "resume", session_id, text], timeout=_RESUME_TIMEOUT
        )
        if resumed.ok:
            return InjectionResult(
                outcome="injected",
                detail="已通过 codex exec resume 交由目标会话处理（可能启动新回合）",
                injected_at=now_utc(),
                wake_requested=True,
            )
        return InjectionResult(
            outcome="failed",
            detail=(
                "codex queue 与 codex exec resume 均失败："
                f"queue rc={queued.returncode} err={queued.stderr.strip()[:200]}；"
                f"resume rc={resumed.returncode} err={resumed.stderr.strip()[:200]}"
            ),
        )

    # -- 唤醒 -------------------------------------------------------------

    def wake(self, connection_id: str) -> WakeResult:
        """请求为目标会话启动一个回合。

        真正的唤醒动作和注入是同一条命令（``exec resume`` 会启动回合），因此这里
        只做可用性确认：CLI 存在即返回"可以请求唤醒"，具体结果由注入路径汇报。
        """
        cli = self._resolve_cli()
        if cli is None:
            return self.unsupported_wake(
                f"未找到 codex 命令行（{self.cli}），无法唤醒会话"
            )
        del connection_id
        return WakeResult(
            requested=True,
            started=False,
            detail=(
                "已确认 Codex CLI 可用；唤醒在注入时通过 codex queue / "
                "codex exec resume 实际发生。"
            ),
        )


def probe_codex(runner: CommandRunner | None = None, cli: str = "codex") -> ProbeResult:
    """只读探测 Codex CLI 及其子命令。不创建会话、不发送任何消息。"""
    probe_runner = runner or SubprocessRunner()
    evidence: list[str] = []
    resolved = existing_program(cli, probe_runner)
    if resolved is None:
        return ProbeResult(
            supported=False,
            level=HostCapabilityLevel.TOOLS_ONLY,
            detail=(
                f"未找到 {cli} 命令行：无法注入或唤醒 Codex 会话，"
                "本宿主只能作为人工参与者使用邮箱工具。"
            ),
            evidence=(f"which({cli})=未找到",),
        )
    evidence.append(f"发现 codex：{resolved}")

    help_result = probe_runner.run([resolved, "--help"], timeout=20.0)
    surface = help_result.stdout + help_result.stderr
    has_queue = "queue" in surface
    has_resume = "resume" in surface
    has_exec = "exec" in surface
    evidence.append(f"codex --help 含 queue={has_queue} exec={has_exec} resume={has_resume}")

    if has_queue and has_exec and has_resume:
        return ProbeResult(
            supported=True,
            level=HostCapabilityLevel.WAKE,
            detail=(
                "Codex 提供 queue / exec resume 子命令，可用于把消息交给指定会话并"
                "启动回合（Level 2）。注意：本机的端到端唤醒尚未验证。"
            ),
            evidence=tuple(evidence),
        )
    return ProbeResult(
        supported=False,
        level=HostCapabilityLevel.TOOLS_ONLY,
        detail=(
            "Codex CLI 存在，但缺少 queue / exec resume 子命令，无法可靠注入消息；"
            "按 Level 0 处理。"
        ),
        evidence=tuple(evidence),
    )


def parse_session_listing(payload: str) -> list[CodexSession]:
    """把 ``codex agents``/会话列表的 JSON 输出解析为会话列表。

    同时接受 JSON 数组与"每行一个 JSON 对象"两种形态；解析不出来就返回空列表，
    不猜测、不构造假 ID。
    """
    text = payload.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    rows: list[object]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        candidate = data.get("sessions") or data.get("threads") or data.get("items")
        rows = candidate if isinstance(candidate, list) else []
    else:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    sessions: list[CodexSession] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = str(row.get("id") or row.get("session_id") or row.get("thread_id") or "").strip()
        if not session_id:
            continue
        sessions.append(
            CodexSession(
                session_id=session_id,
                name=(str(row["name"]) if row.get("name") else None),
                cwd=(str(row["cwd"]) if row.get("cwd") else None),
                status=(str(row["status"]) if row.get("status") else None),
            )
        )
    return sessions


_UUID_HEX = set("0123456789abcdef-")


def _first_uuid_like(text: str) -> str | None:
    """从一行文本里找出形如 UUID 的片段（36 字符、含连字符）。"""
    for token in text.replace("|", " ").replace("\t", " ").split():
        cleaned = token.strip(",()[]{}'\"")
        if len(cleaned) == 36 and set(cleaned.lower()) <= _UUID_HEX:
            return cleaned
    return None
