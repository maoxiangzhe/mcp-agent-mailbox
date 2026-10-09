"""DSH 宿主适配器。

**能力等级取决于"邮箱侧有没有真正可用的注入通道"**，不靠声明：

* ``$DSH_HOME/.credentials.yaml`` 里能读到 ``client-connection/browser-session`` 密钥
  -> 走 DSH 的本地接口 ``POST /api/session/prompt``（签名 cookie）把消息注入目标会话
  并启动一个回合，此即 **Level 2（wake）**；
* 读不到密钥（DSH 只在进程内存里持有它、或文件被移走）-> 通道不可用，等价 **Level 0**：
  消息照样发得出去、会持久化排队，等目标自己取信，**绝不谎报 delivered**。

已确认的事实（对照运行中的 DSH 0.2.0 实现逐行核实）：

1. DSH 有会话 inbox，但它是会话日志 ``agent/inbox/spliced`` 事件的**进程内投影**，
   写入必须调用进程内 ``Agent`` 对象（``followup`` / ``steer`` / ``inject``）；
2. DSH 启动 MCP 子进程时会过滤 ``DSH_*`` 环境变量，因此 MCP 服务器默认拿不到
   ``DSH_SESSION_ID``——除非 profile 里为该服务器静态注入 ``MAILBOX_SESSION_ID``；
3. 外部进程唯一可用的写入口是 GUI 的本地 HTTP 路由 ``POST /api/session/prompt``，
   鉴权是**签名 cookie**（密钥见上）。这条通道对冷会话也会隐式 resume 再 followup，
   也就是真的会启动一个模型回合；
4. 因此本适配器**只走这条通道**，不做 UI 自动化、不改会话日志文件。

相关环境变量（DSH 会过滤 ``DSH_*``，所以给 MCP 子进程配 ``MAILBOX_`` 前缀的等价项）：

    MAILBOX_DSH_WEB_URL / DSH_WEB_URL   默认 http://127.0.0.1:19387
    MAILBOX_DSH_HOME    / DSH_HOME      默认 ~/.dsh
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..domain.presence import HostCapabilityLevel
from ..ports.host_adapter import ExternalEnvelope, InjectionResult, ProbeResult, WakeResult
from .base import BaseAdapter, CommandRunner, SubprocessRunner, existing_program
from .dsh_wake import DshWebWaker, credentials_path

__all__ = ["DshAdapter", "probe_dsh"]

_NO_CREDENTIALS_REASON = (
    f"读不到 DSH 浏览器会话密钥（{credentials_path()}）：DSH 只在进程内存里持有它时，"
    "外部进程无法签名，因此不能把消息注入会话；消息会持久化排队，等目标自己取信。"
)


@dataclass(slots=True)
class DshAdapter(BaseAdapter):
    """DSH 适配器：有凭据时 Level 2（可注入开工），否则 Level 0（只能排队等取信）。"""

    name: str = "dsh-adapter"
    host_type: str = "dsh"
    declared_level: HostCapabilityLevel = HostCapabilityLevel.TOOLS_ONLY
    verified: bool = False
    #: DSH 不会主动回调邮箱；投递由邮箱侧发起（直连注入或等目标取信）。
    realtime_receive: bool = False
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    notes: tuple[str, ...] = (
        "在线严格参照托管进程：进程活着=在线，进程没了=离线。",
        "有凭据时用本地接口把消息注入目标会话并启动回合（Level 2）；无凭据时排队等取信。",
        "禁止直接改写 session.v4.jsonl.zstd / session_projcache，禁止 UI 自动化。",
    )

    # -- 探测 -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        return probe_dsh(self.runner)

    # -- 注入与唤醒 -------------------------------------------------------

    def inject(
        self,
        delivery: dict[str, object],
        envelope: ExternalEnvelope,
        content: str,
    ) -> InjectionResult:
        """适配器事件通道上的注入：DSH 不会主动回调，所以这里如实返回 unsupported。

        真正的注入走 :class:`DshWebWaker`（邮箱侧直连调用 DSH 本地接口）。
        """
        del delivery, content
        return InjectionResult(
            outcome="unsupported",
            detail=(
                "DSH 不会主动接收邮箱事件；注入由邮箱侧通过 DSH 本地接口完成"
                "（见 adapters/dsh_wake.py）或由目标自己取信。"
            ),
            wake_requested=False,
        )

    def wake(self, connection_id: str) -> WakeResult:
        del connection_id
        return self.unsupported_wake(
            "唤醒必须给出目标原生会话 ID，请使用 DshWebWaker.wake(session_id, text)。"
        )


def probe_dsh(runner: CommandRunner | None = None) -> ProbeResult:
    """只读探测：身份能不能拿到、注入通道到底通不通。

    探测过程**不写任何 DSH 状态**。
    """
    probe_runner = runner or SubprocessRunner()
    evidence: list[str] = []

    env_session = os.environ.get("MAILBOX_SESSION_ID", "").strip()
    dsh_session = os.environ.get("DSH_SESSION_ID", "").strip()
    if env_session:
        evidence.append("MAILBOX_SESSION_ID 存在（适配器可完成账号绑定）")
    elif dsh_session:
        evidence.append("DSH_SESSION_ID 存在（注意：MCP 子进程通常拿不到它）")
    else:
        evidence.append("未发现 MAILBOX_SESSION_ID / DSH_SESSION_ID：无法绑定会话")

    credentials = credentials_path()
    waker = DshWebWaker.from_environment()
    if waker is not None:
        evidence.append(f"读到了 {credentials} 里的签名密钥：可以用本地接口注入")
        evidence.append(f"DSH 本地接口：{waker.base_url}/api/session/prompt")
    else:
        evidence.append(f"读不到 {credentials} 里的签名密钥：没有注入通道")

    cli = existing_program(os.environ.get("DSH_CLI", "dsh"), probe_runner)
    if cli:
        evidence.append(f"发现 dsh 命令：{cli}（但它没有「给会话发消息」的子命令）")
    else:
        evidence.append("未发现 dsh 命令行")

    identity_available = bool(env_session or dsh_session)
    if waker is not None:
        return ProbeResult(
            supported=True,
            level=HostCapabilityLevel.WAKE,
            detail=(
                "DSH 适配器为 Level 2（可注入）：邮箱可以通过 DSH 本地接口把消息送进目标会话"
                "并启动一个回合，宿主确认接收后投递才是 delivered。"
            ),
            evidence=tuple(evidence),
        )
    detail = (
        "DSH 适配器当前为 Level 0（只能排队等取信）："
        + ("已能取得会话身份，账号绑定可用。" if identity_available else
           "尚未取得会话身份，账号绑定不可用（请在 profile 中为邮箱 MCP 服务器注入 "
           "MAILBOX_SESSION_ID）。")
        + _NO_CREDENTIALS_REASON
    )
    return ProbeResult(
        supported=identity_available,
        level=HostCapabilityLevel.TOOLS_ONLY,
        detail=detail,
        evidence=tuple(evidence),
    )


def level2_guidance() -> dict[str, object]:
    """达到 Level 2 的两条路径（一条已实现，一条属于 DSH 侧新代码）。"""
    return {
        "current_level": "有凭据时 2 / 否则 0（由 probe 如实判定）",
        "target_level": 2,
        "path": (
            "首选：让邮箱读到 DSH 的浏览器会话密钥（$DSH_HOME/.credentials.yaml 的 "
            "client-connection/browser-session），邮箱侧直连 POST /api/session/prompt 注入。"
            "备选：在 DSH 进程内实现 Cordis 插件，用 "
            "ctx.sessionController.resolveAgent(sessionId) + agent.followup(...) 注入，"
            "再由插件与邮箱 Broker 通信。"
        ),
        "requirements": [
            "凭据文件必须存在且可读（DSH 重启后会重建它）",
            "注入必须带来源信息，且不得提升目标会话的权限或放宽审批策略",
            "禁止改写 session.v4.jsonl.zstd / session_projcache，禁止 UI 自动化",
        ],
        "status": "direct_channel_implemented",
    }
