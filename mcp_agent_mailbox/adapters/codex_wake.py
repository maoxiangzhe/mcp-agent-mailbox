"""Wake an existing Codex app-server thread through official JSON-RPC.

Opt-in, persistent connection; never starts another app-server or overrides
thread permissions. Approval requests are left to the owning host client.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CodexWakeOutcome:
    started: bool
    detail: str
    unavailable: bool = False


class RpcError(RuntimeError):
    pass


class CodexAppServerWaker:
    host_type = "codex"
    channel_name = "codex_app_server"

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 10,
                 connect=None):
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Codex wake requires an explicit loopback ws:// endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Endpoint must not contain credentials, query or fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url, self.token, self.timeout = url, token, timeout
        self._connect = connect
        self._socket = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls):
        url = os.environ.get("MAILBOX_CODEX_APP_SERVER_URL", "").strip()
        if not url:
            return None
        return cls(url, token=os.environ.get("MAILBOX_CODEX_APP_SERVER_TOKEN"))

    def _rpc(self, method, params):
        request_id = str(uuid.uuid4())
        self._socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex RPC acknowledgement timed out")
            message = json.loads(self._socket.recv(timeout=remaining))
            # Notifications and host approval requests are not acceptance receipts.
            # Never approve requests or execute host tools on behalf of the user.
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise RpcError("Codex rejected " + method)
            result = message.get("result")
            if not isinstance(result, dict):
                raise RpcError("Malformed Codex response for " + method)
            return result

    def _ensure_connection(self):
        if self._socket is not None:
            return
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        headers = {"Authorization": "Bearer " + self.token} if self.token else None
        self._socket = connect(self.url, additional_headers=headers,
                               open_timeout=self.timeout, proxy=None)
        try:
            self._rpc("initialize", {"clientInfo": {"name": "mcp_agent_mailbox",
                      "title": "MCP Agent Mailbox", "version": "0.3.1"}})
            self._socket.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.close()
            raise

    def close(self):
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def channel_available(self):
        with self._lock:
            try:
                self._ensure_connection()
                return True
            except Exception:
                self.close()
                return False

    def channel_status(self):
        available = self.channel_available()
        return {"active": self.channel_name if available else None,
                "verified": False, "available": available,
                "detail": "Official app-server protocol; real-host test pending"}

    def wake(self, session_id: str, text: str):
        if not session_id.strip() or not text.strip():
            return CodexWakeOutcome(False, "Missing target thread ID or notice")
        with self._lock:
            try:
                self._ensure_connection()
            except Exception:
                return CodexWakeOutcome(False, "Codex app-server unavailable (endpoint/auth/dependency)", True)
            try:
                resumed = self._rpc("thread/resume", {"threadId": session_id})
                if resumed.get("thread", {}).get("id") != session_id:
                    raise RpcError("Codex returned a different thread")
                result = self._rpc("turn/start", {"threadId": session_id,
                    "input": [{"type": "text", "text": text}]})
                turn = result.get("turn", {})
                if not turn.get("id") or turn.get("status") not in ("inProgress", "completed"):
                    raise RpcError("Codex did not acknowledge a started turn")
                # Keep connection alive so disconnect doesn't abandon the host turn.
                return CodexWakeOutcome(True, "Codex acknowledged turn/start")
            except RpcError as exc:
                return CodexWakeOutcome(False, str(exc))
            except Exception:
                self.close()
                return CodexWakeOutcome(False, "Codex receipt unknown; retry may duplicate notice")
