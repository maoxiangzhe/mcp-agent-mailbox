"""DSH 专用唤醒通道 B：把消息交给 **DSH 进程内的插件**，由插件注入目标会话并起一个回合。

为什么需要这条通道：DSH 的会话不是 HTTP 服务，唯一能"往某个会话里塞一条用户消息并
启动一个回合"的地方是 **DSH 进程内的 Agent 对象**（``Agent.followup``）。外部进程
做不到，所以要在 DSH 里装一个小插件（本仓库的 ``dsh-wake-plugin/``），它监听本机回环
上的一行入口；邮箱把"叫醒谁、说什么"POST 给它，它调用
``ctx.sessionController.resolveAgent(sessionId)`` + ``agent.followup(...)``。

与通道 A（``dsh_http_wake``：直连 DSH 自己的 ``/api/session/prompt`` + 自签 cookie）相比：

* **B 不需要任何凭据**，只要插件装着就可用 —— 所以 B 优先；
* A 需要 ``$DSH_HOME/.credentials.yaml`` 里的签名密钥 —— 所以 A 兜底；
* 两个都不可用 -> **明确报错**（投递保持 queued，并写清是哪两个通道、各自为什么不行），
  绝不谎报 delivered。

环境变量：

    MAILBOX_DSH_WAKE_URL     默认 http://127.0.0.1:8799/dsh-wake
    MAILBOX_DSH_WAKE_TOKEN   与插件共享的令牌（**必须**，没有它这条通道直接判定不可用）
    MAILBOX_DSH_WAKE_HEALTH  默认 http://127.0.0.1:8799/healthz
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .dsh_wake import WakeOutcome

__all__ = ["DshPluginWaker", "DEFAULT_WAKE_URL", "DEFAULT_HEALTH_URL"]

DEFAULT_WAKE_URL = "http://127.0.0.1:8799/dsh-wake"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8799/healthz"

#: 探测结果缓存秒数：presence 每次调用都会问"通道到底通不通"，不能每次都发 HTTP。
PROBE_TTL_SECONDS = 5.0


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 3.0,
) -> tuple[int, str]:
    """极简 HTTP 客户端：只用来打本机回环上的插件入口。"""
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", errors="replace")


class DshPluginWaker:
    """通道 B：调用 DSH 内插件的本地入口。"""

    #: 这个唤醒器只对 DSH 账号有效。
    host_type = "dsh"
    #: 给人类看的通道名（presence 的 wake_basis 用它）。
    channel_name = "plugin_channel"

    def __init__(
        self,
        wake_url: str,
        token: str,
        *,
        health_url: str | None = None,
        timeout: float = 3.0,
        request=None,
    ) -> None:
        self.wake_url = wake_url
        self.token = token
        self.health_url = health_url or DEFAULT_HEALTH_URL
        self._timeout = timeout
        self._request = request or _http_request
        self._probe_ok: bool | None = None
        self._probe_at: float = 0.0

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_environment(cls, *, request=None) -> "DshPluginWaker | None":
        """按环境变量组装；**没有令牌就返回 None**（不开一个无鉴权的注入入口）。"""
        token = (os.environ.get("MAILBOX_DSH_WAKE_TOKEN") or "").strip()
        if not token:
            return None
        wake_url = (os.environ.get("MAILBOX_DSH_WAKE_URL") or DEFAULT_WAKE_URL).strip()
        health_url = (
            os.environ.get("MAILBOX_DSH_WAKE_HEALTH") or DEFAULT_HEALTH_URL
        ).strip()
        return cls(wake_url, token, health_url=health_url, request=request)

    # -- 探测 -------------------------------------------------------------

    def channel_available(self, *, now: float | None = None) -> bool:
        """插件此刻能不能用（带缓存，避免 presence 每次都发请求）。"""
        moment = time.monotonic() if now is None else now
        if self._probe_ok is not None and moment - self._probe_at < PROBE_TTL_SECONDS:
            return self._probe_ok
        headers = {"x-dsh-wake-token": self.token}
        try:
            status, _ = self._request(
                self.health_url, method="GET", headers=headers, timeout=self._timeout
            )
            ok = status == 200
        except Exception:  # noqa: BLE001 - 连不上就是不可用
            ok = False
        self._probe_ok = ok
        self._probe_at = moment
        return ok

    def reason_unavailable(self) -> str:
        return (
            f"DSH 内唤醒插件没在跑或令牌不对（{self.wake_url}）："
            "需要在本 profile 里安装/启用 dsh-wake-plugin"
        )

    # -- 唤醒 -------------------------------------------------------------

    def wake(self, session_id: str, text: str) -> WakeOutcome:
        if not session_id:
            return WakeOutcome(False, "目标账号没有原生会话 ID，无法注入", unavailable=True)
        body = json.dumps(
            {"sessionId": session_id, "text": text}, ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-dsh-wake-token": self.token,
        }
        try:
            status, text_body = self._request(
                self.wake_url,
                method="POST",
                headers=headers,
                body=body,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 连不上 = 通道不可用，不算投递失败
            return WakeOutcome(
                False,
                f"通道B(DSH 内插件)：连不上 {self.wake_url}（{exc}）",
                unavailable=True,
            )
        self._probe_at = 0.0  # 下次重新探测，别把刚得到的结论缓存住
        if status in (401, 403):
            return WakeOutcome(
                False,
                f"通道B(DSH 内插件)：令牌被拒（HTTP {status}）",
                status=status,
                unavailable=True,
            )
        if status != 200:
            return WakeOutcome(
                False,
                f"通道B(DSH 内插件)：HTTP {status} {text_body.strip()[:160]}",
                status=status,
            )
        try:
            payload = json.loads(text_body)
        except json.JSONDecodeError:
            return WakeOutcome(False, f"通道B(DSH 内插件)：非 JSON 响应 {text_body[:160]}", status=status)
        if isinstance(payload, dict) and payload.get("accepted") is True:
            return WakeOutcome(
                True, f"通道B(DSH 内插件)：已注入会话 {session_id} 并启动回合", status=status
            )
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("detail") or payload)
        return WakeOutcome(
            False, f"通道B(DSH 内插件)：宿主未接受（{detail[:160]}）", status=status
        )
