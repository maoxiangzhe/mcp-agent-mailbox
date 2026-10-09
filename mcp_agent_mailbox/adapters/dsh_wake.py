"""DSH 唤醒通道：把一条消息**送进目标 DSH 会话并让它开始一个回合**。

产品规则要的是"在线账号可以直接接收并开工"（像给子代理发一条消息那样）。DSH 0.2.0
提供了一个本地写入口：

    POST <DSH_WEB_URL>/api/session/prompt

它接受一条用户消息（``mode="queue"``）并对**冷会话隐式 resume**，然后在会话里
``followup`` —— 也就是真的启动一个模型回合，而不只是写日志。

鉴权不是 token header，而是**签名 cookie**（逐字节对照 DSH 自身实现实现）：

    cookie 名  = "dsh-auth-" + base64url(sha256(utf8(authority)))
    cookie 值  = "v1." + body + "." + base64url(hmac_sha256(secret, body))
    body      = base64url(utf8(json({"version":1,"authority":…,"issuedAt":毫秒,"expiresAt":毫秒})))
    签名密钥    = $DSH_HOME/.credentials.yaml 里 ``client-connection/browser-session``
                 记录的 ``payload.secret``（base64url，32 字节）

诚实边界（很重要，不允许含糊）：

* 拿不到密钥或够不到 URL -> 通道**不可用**：投递保持 ``queued``，绝不谎报 ``delivered``；
* 宿主返回 ``accepted`` -> 它已经把这条 prompt 排进目标会话 -> 才允许 ``delivered``；
* 本模块**不写 DSH 的任何文件**、不做 UI 自动化、不改会话日志。

环境变量（DSH 会过滤掉 ``DSH_*``，所以给 MCP 子进程配了 ``MAILBOX_`` 前缀的等价项）：

    MAILBOX_DSH_WEB_URL / DSH_WEB_URL   例如 http://127.0.0.1:19387
    MAILBOX_DSH_HOME    / DSH_HOME      默认 ~/.dsh
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "DshWebWaker",
    "WakeOutcome",
    "build_cookie",
    "cookie_name_for",
    "credentials_path",
    "read_browser_session_secret",
]

#: 记录键：credentialKey("client-connection", "browser-session")。
SECRET_RECORD_KEY = "client-connection/browser-session"
SECRET_BYTES = 32
COOKIE_PREFIX = "dsh-auth-"
DEFAULT_WEB_URL = "http://127.0.0.1:19387"

_SECRET_LINE = re.compile(r"\bsecret\s*:\s*[\"']?([A-Za-z0-9_-]{20,})[\"']?")


@dataclass(frozen=True, slots=True)
class WakeOutcome:
    """一次唤醒尝试的结果。"""

    started: bool
    detail: str
    status: int | None = None
    #: 通道根本不可用时为 True：失败重试没有意义，消息继续排队等目标自己取信。
    unavailable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "started": self.started,
            "detail": self.detail,
            "status": self.status,
            "unavailable": self.unavailable,
        }


# ---------------------------------------------------------------------------
# 凭据与 cookie
# ---------------------------------------------------------------------------


def dsh_home() -> Path:
    raw = (
        os.environ.get("MAILBOX_DSH_HOME")
        or os.environ.get("DSH_HOME")
        or ""
    ).strip()
    return Path(raw) if raw else Path.home() / ".dsh"


def credentials_path() -> Path:
    return dsh_home() / ".credentials.yaml"


def _base64url_decode(value: str) -> bytes | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value) or len(value) % 4 == 1:
        return None
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:  # noqa: BLE001 - 任何解码失败都当作"没有密钥"
        return None


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def read_browser_session_secret(path: Path | None = None) -> bytes | None:
    """从 ``$DSH_HOME/.credentials.yaml`` 读签名密钥（32 字节），读不到返回 ``None``。

    只做**针对性的行扫描**（本仓库不引入 YAML 依赖）：定位 ``records:`` 下键为
    ``client-connection/browser-session`` 的那一块，再在这一块里找 ``secret:``。
    真正的守门人是"解出来必须正好 32 字节"——取错值过不了这一关。
    """
    target = path or credentials_path()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip().strip("\"'")
        if not stripped.startswith(SECRET_RECORD_KEY):
            continue
        key_indent = len(line) - len(line.lstrip())
        block: list[str] = []
        for follow in lines[index:]:
            if follow is lines[index]:
                block.append(follow)
                continue
            indent = len(follow) - len(follow.lstrip())
            if follow.strip() and indent <= key_indent:
                break
            block.append(follow)
        match = _SECRET_LINE.search("\n".join(block))
        if match is None:
            continue
        decoded = _base64url_decode(match.group(1))
        if decoded is not None and len(decoded) == SECRET_BYTES:
            return decoded
    return None


def cookie_name_for(authority: str) -> str:
    digest = hashlib.sha256(authority.encode("utf-8")).digest()
    return COOKIE_PREFIX + _base64url_encode(digest)


def build_cookie(
    secret: bytes,
    authority: str,
    *,
    ttl_seconds: int = 600,
    now: datetime | None = None,
) -> str:
    """按 DSH 的格式签一个浏览器会话 cookie。"""
    moment = now or datetime.now(timezone.utc)
    issued_at = int(moment.timestamp() * 1000)
    payload = {
        "version": 1,
        "authority": authority,
        "issuedAt": issued_at,
        "expiresAt": issued_at + ttl_seconds * 1000,
    }
    body = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).digest()
    return f"v1.{body}.{_base64url_encode(signature)}"


# ---------------------------------------------------------------------------
# 唤醒客户端
# ---------------------------------------------------------------------------


def _http_post(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定本地回环地址
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:  # 4xx/5xx：把状态码与正文交回调用方判断
        return int(error.code), error.read().decode("utf-8", errors="replace")


class DshWebWaker:
    """通道 A：把消息投进 DSH 会话并启动回合（直连 DSH 本地接口）。"""

    #: 这个唤醒器只对 DSH 账号有效。
    host_type = "dsh"
    #: 给人类看的通道名（presence 的 wake_basis 用它）。
    channel_name = "http_channel"

    def __init__(
        self,
        base_url: str,
        secret: bytes,
        *,
        timeout: float = 5.0,
        ttl_seconds: int = 600,
        post=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self._timeout = timeout
        self._ttl_seconds = ttl_seconds
        self._post = post or _http_post

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_environment(cls, *, post=None) -> "DshWebWaker | None":
        """按环境变量组装；缺少 URL 或密钥时返回 ``None``（= 没有唤醒通道）。"""
        url = (
            os.environ.get("MAILBOX_DSH_WEB_URL")
            or os.environ.get("DSH_WEB_URL")
            or DEFAULT_WEB_URL
        ).strip()
        if not url:
            return None
        secret = read_browser_session_secret()
        if secret is None:
            return None
        return cls(url, secret, post=post)

    @property
    def authority(self) -> str:
        """cookie 的签名受众 = 请求 Host（如 ``127.0.0.1:19387``）。"""
        without_scheme = self.base_url.split("://", 1)[-1]
        return without_scheme.split("/", 1)[0]

    def reason_unavailable(self) -> str:
        """通道不可用的原因（用于给人类看的诚实说明）。"""
        if read_browser_session_secret() is None:
            return (
                f"读不到 DSH 浏览器会话密钥（{credentials_path()}）："
                "DSH 只在进程内存里持有它时无法从外部签名，因此不能注入"
            )
        return "未配置 DSH_WEB_URL"

    # -- 唤醒 -------------------------------------------------------------

    def wake(self, session_id: str, text: str) -> WakeOutcome:
        """把 ``text`` 作为一条用户消息送进 ``session_id`` 并启动回合。"""
        if not session_id:
            return WakeOutcome(False, "目标账号没有原生会话 ID，无法注入", unavailable=True)
        authority = self.authority
        cookie = build_cookie(self._secret, authority, ttl_seconds=self._ttl_seconds)
        body = json.dumps(
            {
                "type": "client-request",
                "rpcId": str(uuid.uuid4()),
                "method": "session/prompt",
                # 注意这层 args：DSH 的 Typert 网关要求"payload 里恰好有一个 plain-object
                # 的 args 字段"，而 `session/prompt` 的 args 只接受一个名为 request 的字段
                # （参数描述符：missing "request" / unexpected "sessionId"…）。所以业务
                # 参数要放进 args.request，而不是直接放 payload 或 args。
                "payload": {
                    "args": {
                        "request": {
                            "requestId": str(uuid.uuid4()),
                            "sessionId": session_id,
                            "mode": "queue",
                            "content": [{"type": "text", "text": text}],
                        }
                    }
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "cookie": f"{cookie_name_for(authority)}={cookie}",
        }
        try:
            status, text_body = self._post(
                f"{self.base_url}/api/session/prompt", headers, body, self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - 连不上就是连不上，别把异常抛给投递循环
            # 连不上 = 通道这一刻用不了：投递保持 queued，等目标自己取信，不算投递失败。
            return WakeOutcome(
                False,
                f"连不上 DSH 本地接口（{self.base_url}）：{exc}",
                unavailable=True,
            )
        if status in (401, 403):
            # 鉴权/信任围栏拒绝：凭据过期或换了密钥 -> 同样是"通道用不了"。
            return WakeOutcome(
                False,
                f"DSH 拒绝鉴权（HTTP {status}）：凭据不可用或已轮换，消息留在队列等目标取信",
                status=status,
                unavailable=True,
            )
        if status != 200:
            return WakeOutcome(
                False,
                f"DSH 拒绝唤醒（HTTP {status}）：{text_body.strip()[:200]}",
                status=status,
            )
        try:
            envelope = json.loads(text_body)
        except json.JSONDecodeError:
            return WakeOutcome(False, f"DSH 返回了非 JSON 响应：{text_body[:200]}", status=status)
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if isinstance(result, dict) and result.get("ok") is True:
            value = result.get("value")
            accepted = bool(value.get("accepted")) if isinstance(value, dict) else False
            if accepted:
                return WakeOutcome(True, f"DSH 已接收并已排入会话 {session_id}", status=status)
            return WakeOutcome(False, f"DSH 未接受该消息：{json.dumps(value, ensure_ascii=False)[:200]}", status=status)
        error = result.get("error") if isinstance(result, dict) else envelope
        return WakeOutcome(
            False, f"DSH 报错：{json.dumps(error, ensure_ascii=False)[:200]}", status=status
        )
