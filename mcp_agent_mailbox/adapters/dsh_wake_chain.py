"""DSH 注入通道链：**B 优先 → A 兜底 → 两个都不行就明确报错**。

    B（plugin_channel）  DSH 进程内插件：不需要任何凭据，装上就可用
    A（http_channel）    DSH 自己的本地接口 /api/session/prompt：需要 .credentials.yaml 里的签名密钥
    报错                 两个都不可用/都失败 -> 投递**保持 queued**并写清原因（绝不谎报 delivered）

为什么 B 排第一：它能无条件工作（插件在进程内直接 ``agent.followup``），而 A 依赖一个
DSH 现在不一定写盘的凭据文件。A 留着当兜底，是因为插件没装/被停用时它仍然可能可用。

"报错"的具体形态（不吞掉）：
* 两个通道**都不可用** -> ``unavailable=True``：投递保持 ``queued``，``last_error`` 写清
  "通道B 为什么不行；通道A 为什么不行"，并记审计事件；
* 通道能调但**这次注入失败** -> 不算 unavailable：投递进 ``failed``（可重试，重试用尽进死信）；
* ``channel_status()`` 给 ``whoami``/``doctor``/前端用来显示"现在到底有哪条通道"。
"""

from __future__ import annotations

from .dsh_plugin_wake import DshPluginWaker
from .dsh_wake import DshWebWaker, WakeOutcome

__all__ = ["DshWakeChain"]

_PLUGIN_LABEL = "通道B(DSH 内插件)"
_HTTP_LABEL = "通道A(DSH 本地接口)"


class DshWakeChain:
    """把"注入"抽象成一条链：先试 B，再试 A，都不行就报错。"""

    host_type = "dsh"

    def __init__(self, plugin: DshPluginWaker | None, http: DshWebWaker | None) -> None:
        self._plugin = plugin
        self._http = http

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_environment(cls, *, request=None) -> "DshWakeChain | None":
        """按环境装配：B 和 A 哪一个能装出来就装哪一个；都装不出来返回 ``None``。"""
        plugin = DshPluginWaker.from_environment(request=request)
        http = DshWebWaker.from_environment()
        if plugin is None and http is None:
            return None
        return cls(plugin, http)

    # -- 诊断 -------------------------------------------------------------

    @staticmethod
    def _ready(channel) -> bool:
        """这条通道**此刻**可用吗？

        有 ``channel_available()`` 的通道自己说了算（B 通道会去问插件 healthz）；
        没有探测能力的通道（A 通道：能不能用在建构造时就按凭据定死了）只要能装配出来
        就算可用。
        """
        if channel is None:
            return False
        probe = getattr(channel, "channel_available", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # noqa: BLE001 - 探测炸了就当作不可用
                return False
        return True

    @property
    def channel_name(self) -> str | None:
        """当前**可用**的通道名（B 优先）。没有可用通道返回 ``None``。"""
        if self._ready(self._plugin):
            return "plugin_channel"
        if self._ready(self._http):
            return "http_channel"
        return None

    def channel_available(self) -> bool:
        """有没有任何一条通道可用（presence 用它，避免谎报 can_wake）。"""
        return self.channel_name is not None

    def channel_status(self) -> dict[str, object]:
        """给人和诊断命令看的通道现状。"""
        plugin_ok = self._ready(self._plugin)
        http_ok = self._ready(self._http)
        return {
            "plugin_channel": {
                "installed": self._plugin is not None,
                "available": plugin_ok,
                "detail": (
                    "DSH 内插件已就绪"
                    if plugin_ok
                    else (self._plugin.reason_unavailable() if self._plugin else
                          "未配置 MAILBOX_DSH_WAKE_TOKEN（或插件没装）")
                ),
            },
            "http_channel": {
                "configured": self._http is not None,
                "available": http_ok,
                "detail": (
                    "DSH 本地接口凭据可用"
                    if http_ok
                    else "读不到 DSH 浏览器会话密钥（$DSH_HOME/.credentials.yaml）"
                ),
            },
            "active": self.channel_name,
            "order": ["plugin_channel", "http_channel"],
        }

    # -- 唤醒 -------------------------------------------------------------

    def wake(self, session_id: str, text: str) -> WakeOutcome:
        reasons: list[str] = []
        unavailable_everywhere = True

        if self._plugin is not None:
            outcome = self._plugin.wake(session_id, text)
            if outcome.started:
                return outcome
            reasons.append(outcome.detail)
            unavailable_everywhere = unavailable_everywhere and outcome.unavailable

        if self._http is not None:
            outcome = self._http.wake(session_id, text)
            if outcome.started:
                return WakeOutcome(
                    True, f"{_HTTP_LABEL}：{outcome.detail}", status=outcome.status
                )
            reasons.append(f"{_HTTP_LABEL}：{outcome.detail}")
            unavailable_everywhere = unavailable_everywhere and outcome.unavailable

        if not reasons:
            # 连通道都没配出来（例如没令牌也没凭据）——这就是"报错3"。
            return WakeOutcome(
                False,
                "两个 DSH 注入通道都没有配置：B 需要 MAILBOX_DSH_WAKE_TOKEN + 装插件；"
                "A 需要 $DSH_HOME/.credentials.yaml。消息保留在队列里等目标取信。",
                unavailable=True,
            )

        headline = (
            "两个 DSH 注入通道都不可用"
            if unavailable_everywhere
            else "两个 DSH 注入通道都没能把消息送进目标会话"
        )
        return WakeOutcome(
            False,
            f"{headline}：" + "；".join(reasons),
            unavailable=unavailable_everywhere,
        )
