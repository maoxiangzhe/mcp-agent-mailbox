"""宿主适配器的公共部分。

适配器负责把"邮箱消息"翻译成"宿主认可的动作"。这里有两条不可越界的规则
（设计文档 §9.4/§9.5、任务书禁止项）：

1. **不得伪造能力等级。** 探测不到正式的注入/唤醒接口时，必须返回
   ``unsupported`` 并如实降级；禁止通过 UI 自动化或改写宿主内部会话文件/数据库
   来"实现"唤醒。
2. **注入必须带来源信封，且不得提升权限。** 消息正文是不可信数据；注入时以
   结构化信封标明来源，且不得因此放宽目标会话的沙箱或审批策略。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from ..domain.presence import HostCapabilityLevel
from ..ports.host_adapter import (
    AdapterCapabilities,
    ExternalEnvelope,
    InjectionResult,
    ProbeResult,
    WakeResult,
    render_envelope,
)

__all__ = [
    "BaseAdapter",
    "CommandRunner",
    "CommandResult",
    "SubprocessRunner",
    "adapter_registry",
    "capability_matrix",
]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """一次子进程调用的结果。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "ok": self.ok,
            "timed_out": self.timed_out,
            "stdout": self.stdout[-2000:],
            "stderr": self.stderr[-2000:],
        }


class CommandRunner:
    """执行外部命令的抽象。

    适配器只依赖这个协议，因此契约测试可以注入"假宿主 CLI"，既能验证真代码路径，
    又**不会真的去唤醒宿主、写宿主配置**。
    """

    def run(self, argv: Sequence[str], *, timeout: float = 30.0) -> CommandResult:
        raise NotImplementedError

    def which(self, program: str) -> str | None:
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    """真实子进程执行器。"""

    def run(self, argv: Sequence[str], *, timeout: float = 30.0) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            return CommandResult(tuple(argv), 127, "", f"命令不存在：{argv[0]}", False)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                tuple(argv),
                124,
                (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                f"命令超时（{timeout}s）",
                True,
            )
        return CommandResult(
            tuple(argv), completed.returncode, completed.stdout, completed.stderr, False
        )

    def which(self, program: str) -> str | None:
        return shutil.which(program)


@dataclass(slots=True)
class BaseAdapter:
    """适配器骨架：实现公共的能力声明与信封渲染，子类只实现宿主交互。"""

    name: str = "base"
    host_type: str = "unknown"
    #: 由子类在 ``probe()`` 里按实际证据填写，禁止无条件写 WAKE。
    declared_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY
    verified: bool = False
    #: 宿主是否会主动把事件推给邮箱侧。
    #:
    #: 默认按等级推断（Level>=1 即有实时通道），但"能唤醒"和"能接收推送"是两个
    #: 维度：Codex 可以被唤醒，却不会主动回调邮箱，因此必须显式置 False，
    #: 否则 presence 会把它算成 realtime，等于谎报实时能力。
    realtime_receive: bool | None = None
    notes: tuple[str, ...] = ()
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    # -- 能力 -------------------------------------------------------------

    @property
    def capabilities(self) -> AdapterCapabilities:
        level = self.declared_level
        realtime = (
            level.can_receive_realtime if self.realtime_receive is None else self.realtime_receive
        )
        return AdapterCapabilities(
            level=level,
            adapter_name=self.name,
            verified=self.verified,
            can_receive_realtime=realtime,
            can_wake=level.can_wake_session,
            notes=self.notes,
        )

    def probe(self) -> ProbeResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # -- 注入 -------------------------------------------------------------

    def render(self, envelope: ExternalEnvelope, content: str) -> str:
        """把消息渲染成注入文本（统一走信封，子类不要自己拼）。"""
        return render_envelope(envelope, content)

    def inject(
        self,
        delivery: dict[str, object],
        envelope: ExternalEnvelope,
        content: str,
    ) -> InjectionResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def wake(self, connection_id: str) -> WakeResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def unsupported_wake(self, reason: str) -> WakeResult:
        """统一的"不支持唤醒"结果。子类在能力不足时必须返回它。"""
        return WakeResult(requested=False, started=False, unsupported_reason=reason)


# ---------------------------------------------------------------------------
# 能力矩阵
# ---------------------------------------------------------------------------


def adapter_registry() -> dict[str, type[BaseAdapter]]:
    """已实现的适配器。延迟导入，避免 CLI 列能力矩阵时也要 import 全部依赖。"""
    from .codex import CodexAdapter
    from .dsh import DshAdapter

    return {"dsh": DshAdapter, "codex": CodexAdapter}


def capability_matrix() -> dict[str, object]:
    """各宿主的能力矩阵。

    ``verified`` 的含义必须严格：
        True  = 本机实测通过（有可复现的证据或自动化测试）；
        False = 按公开接口/文档声明，但**未在本机验证**。
    任何 Level 2 声明都必须在 ``evidence`` 里给出可执行依据，否则只是设计意图。
    """
    rows = [
        {
            "host_type": "dsh",
            "adapter": "dsh",
            "level": 2,
            "level_slug": "wake",
            "verified": False,
            "can_receive_realtime": False,
            "can_wake": True,
            "evidence": [
                "本地写入口：POST http://127.0.0.1:19387/api/session/prompt"
                "（信封 {type:client-request, method:session/prompt}，载荷 "
                "{sessionId, mode:'queue', content:[{type:'text',text}]}）；"
                "对冷会话会隐式 resume 再 followup，即真的启动一个模型回合。",
                "鉴权是签名 cookie：dsh-auth-<b64url(sha256(authority))> = "
                "v1.<body>.<b64url(HMAC-SHA256(secret, body))>，密钥来自 "
                "$DSH_HOME/.credentials.yaml 的 client-connection/browser-session 记录。",
                "密码学细节与 DSH 0.2.0 自身实现逐行对照（dsh-client-connection）；"
                "没有凭据时本适配器如实降级为 Level 0：消息保持 queued，绝不谎报 delivered。",
            ],
            "not_verified": [
                "端到端注入尚未在本机观察到（当前运行缺少 .credentials.yaml，密钥只在 DSH 进程内存里）",
                "DSH 重启重建凭据文件后的注入是否稳定",
                "目标会话正被其他写者持有时（session/writer-held）的错误语义",
            ],
            "path_to_level2": (
                "已实现（邮箱侧直连）：adapters/dsh_wake.py 自签 cookie 调 "
                "/api/session/prompt。备选实现是在 DSH 进程内写 Cordis 插件，用 "
                "ctx.sessionController.resolveAgent(sessionId) + agent.followup(...)，"
                "那条路不依赖凭据文件。"
            ),
            "forbidden": [
                "直接改写 session.v4.jsonl.zstd",
                "直接改写 session_projcache",
                "UI 点击 / 键盘模拟",
            ],
        },
        {
            "host_type": "codex",
            "adapter": "codex",
            "level": 2,
            "level_slug": "wake",
            "verified": False,
            "can_receive_realtime": False,
            "can_wake": True,
            "evidence": [
                "codex queue --thread <id> --message <text>：向共享 app-server 上已有的会话排队一条消息。",
                "codex exec resume <session-id> <prompt>：非交互地把消息交给已有会话并启动一个回合。",
                "codex agents：列出本地 app-server 上的会话，可用于发现可寻址的 thread id。",
            ],
            "not_verified": [
                "真实 Codex 安装上的端到端唤醒（当前测试使用注入的假 CLI 验证调用契约）",
                "queue 对冷会话（无运行中 app-server）的确切行为",
                "目标会话被归档或删除时的错误语义",
            ],
            "caveats": [
                "can_receive_realtime=False：适配器通过子进程主动投递，"
                "Codex 不会主动回调邮箱；账号在线状态仍由连接健康度决定，"
                "因此不能理解为「Codex 会主动把消息推给你」。",
                "唤醒使用宿主既有权限与审批策略，不会因为消息内容放宽沙箱。",
            ],
            "forbidden": [
                "修改 Codex 内部 sqlite / rollout 文件",
                "UI 自动化",
            ],
        },
    ]
    by_level: dict[str, int] = {}
    for row in rows:
        slug = str(row["level_slug"])
        by_level[slug] = by_level.get(slug, 0) + 1
    return {
        "contract_version": 1,
        "adapters": rows,
        "summary_by_level": by_level,
        "note": (
            "verified=False 表示已在代码与契约测试层面实现，但未在真实宿主上完成端到端验证；"
            "不得在文档或 whoami 里把它描述为已支持。"
        ),
    }


def envelope_from_payload(payload: dict[str, object]) -> ExternalEnvelope:
    """从服务层返回的信封字段还原 ``ExternalEnvelope``。"""
    envelope = payload.get("envelope") or {}
    if not isinstance(envelope, dict):
        raise ValueError("delivery payload 缺少 envelope")
    return ExternalEnvelope(
        from_address=str(envelope.get("from_address", "")),
        conversation_id=str(envelope.get("conversation_id", "")),
        message_id=str(envelope.get("message_id", "")),
        delivery_id=str(envelope.get("delivery_id", "")),
        reply_to=(str(envelope["reply_to"]) if envelope.get("reply_to") else None),
        content_type=str(envelope.get("content_type", "text/plain")),
    )


def now_utc() -> datetime:
    from ..domain.timestamps import utc_now

    return utc_now()


def existing_program(candidate: str | None, runner: CommandRunner) -> str | None:
    """判断命令行是否可用（找不到就返回 ``None``，由调用方降级）。"""
    if not candidate:
        return None
    if Path(candidate).exists():
        return candidate
    return runner.which(candidate)
