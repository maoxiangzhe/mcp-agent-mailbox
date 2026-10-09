import json

import pytest

from mcp_agent_mailbox.adapters.codex_wake import CodexAppServerWaker
from mcp_agent_mailbox.adapters.wake_router import WakeRouter
from mcp_agent_mailbox.application.delivery_service import _Target
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel, PresenceState


class Socket:
    def __init__(self, error=None, status="inProgress", wrong_thread=False):
        self.sent, self.responses = [], []
        self.error, self.status, self.wrong_thread = error, status, wrong_thread
        self.closed = False

    def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        if "id" not in request:
            return
        method = request["method"]
        self.responses.append(json.dumps({"method": "item/started", "params": {}}))
        if method == self.error:
            reply = {"error": {"code": -32000, "message": "rejected"}}
        elif method == "thread/resume":
            reply = {"result": {"thread": {"id": "wrong" if self.wrong_thread else request["params"]["threadId"]}}}
        elif method == "turn/start":
            reply = {"result": {"turn": {"id": "turn-1", "status": self.status}}}
        else:
            reply = {"result": {}}
        self.responses.append(json.dumps({"id": request["id"], **reply}))

    def recv(self, timeout):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def waker(socket):
    return CodexAppServerWaker("ws://127.0.0.1:4500", connect=lambda *a, **kw: socket)


def test_official_rpc_and_persistent_connection():
    socket = Socket()
    channel = waker(socket)
    assert channel.wake("thread-1", "notice").started
    assert channel.wake("thread-1", "notice2").started
    assert [r["method"] for r in socket.sent] == ["initialize", "initialized",
        "thread/resume", "turn/start", "thread/resume", "turn/start"]
    assert socket.sent[3]["params"] == {"threadId": "thread-1",
        "input": [{"type": "text", "text": "notice"}]}
    assert not socket.closed
    channel.close()
    assert socket.closed


@pytest.mark.parametrize("method", ["thread/resume", "turn/start"])
def test_rejection_never_delivered(method):
    outcome = waker(Socket(error=method)).wake("thread-1", "notice")
    assert not outcome.started and not outcome.unavailable


def test_wrong_thread_never_started():
    socket = Socket(wrong_thread=True)
    assert not waker(socket).wake("thread-1", "notice").started
    assert "turn/start" not in [r["method"] for r in socket.sent]


@pytest.mark.parametrize("status", ["failed", "interrupted", "unknown"])
def test_failed_turn_never_delivered(status):
    assert not waker(Socket(status=status)).wake("thread-1", "notice").started


def test_unavailable():
    def refuse(*args, **kwargs):
        raise OSError("refused")
    channel = CodexAppServerWaker("ws://localhost:4500", connect=refuse)
    assert channel.wake("thread-1", "notice").unavailable
    assert not channel.channel_available()


@pytest.mark.parametrize("url", ["ws://example.com:4500", "http://localhost", "ws://user:pass@localhost", "ws://localhost/?token=x"])
def test_invalid_endpoint(url):
    with pytest.raises(ValueError):
        CodexAppServerWaker(url)


def test_router_selects_host_and_preserves_offline_rule():
    channel = waker(Socket())
    router = WakeRouter([channel])
    assert router.for_host("codex") is channel
    assert router.for_host("dsh") is None
    target = _Target("connection", 1, HostCapabilityLevel.TOOLS_ONLY,
                     PresenceState.CONNECTED, "codex", "thread-1")
    assert target.injection_mode(router) == "direct"
    offline = _Target("connection", 1, HostCapabilityLevel.WAKE,
                      PresenceState.OFFLINE, "codex", "thread-1")
    assert offline.injection_mode(router) is None
