"""只读监控台的 HTTP 服务。

只用 Python 标准库：``http.server.ThreadingHTTPServer`` + 一个请求处理器。
不引入 Web 框架，也不引入前端构建链。

安全模型（对应设计文档 §4）：

- 启动时生成高熵令牌，只打印一次；令牌不写项目、不写数据库、不写日志文件；
- 除 ``/api/health`` 外，所有 ``/api/*`` 都要求 ``Authorization: Bearer <token>``，
  缺失或错误一律 401（且不透露资源是否存在）；
- ``/api/*`` 只允许 GET / HEAD / OPTIONS，其余方法返回 405；
- 不设置宽泛 CORS：默认不返回任何 ``Access-Control-Allow-Origin``；
- 严格 CSP：默认 ``'self'``，禁止外部脚本/样式/字体与 ``connect-src`` 外联；
- 错误响应只有稳定的机器码与中文提示，不含堆栈、SQL、绝对路径或消息正文。

服务本身没有任何写路径：路由表里全部是查询，业务表写入在只读连接上会被 SQLite 拒绝。
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..domain.timestamps import utc_now
from ..infrastructure.observability import get_logger
from ..infrastructure.sqlite.read_only import ReadOnlyDatabaseError
from .queries import CursorError, DashboardQueries, ResourceGoneError

__all__ = ["DashboardServer", "DashboardToken", "serve_dashboard"]

LOGGER = get_logger("dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 静态资源白名单：路径 -> (文件名, Content-Type)。
#: 用白名单而不是拼路径，避免任何路径穿越的可能。
STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/favicon.ico": ("favicon.svg", "image/svg+xml"),
}

_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

_READ_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass(slots=True)
class DashboardToken:
    """一次性高熵访问令牌。

    只用 ``secrets`` 生成；只存在内存里。``matches`` 用 ``compare_digest`` 做常数时间
    比较，避免通过响应时间逐字节猜令牌。
    """

    value: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def matches(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return secrets.compare_digest(self.value, candidate)

    @property
    def masked(self) -> str:
        """给日志/诊断用的脱敏形态。"""
        return f"{self.value[:4]}…{self.value[-4:]}"


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return socket.ip_address(host).is_loopback  # type: ignore[attr-defined]
    except (ValueError, AttributeError):
        pass
    # 退一步用解析结果判断（例如 "127.0.0.1" 之外的写法）。
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(info[4][0].startswith("127.") or info[4][0] == "::1" for info in infos)


class _Handler(BaseHTTPRequestHandler):
    """监控台请求处理器。

    只实现读方法：基类的 ``do_POST`` / ``do_PUT`` / ``do_PATCH`` / ``do_DELETE``
    一律被显式覆盖为 405，确保"只读"不是靠约定而是靠代码。
    """

    server_version = "mcp-agent-mailbox-dashboard/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- 由 serve 注入 ----------------------------------------------------

    queries: DashboardQueries
    token: DashboardToken

    # -- 基础设施 ---------------------------------------------------------

    def handle_one_request(self) -> None:
        """包装基类实现，把"客户端断开"从错误日志里过滤掉。

        ``socketserver`` 会把请求处理中的异常打到 stderr。HTTP 客户端提前关闭连接
        （浏览器取消请求、探针超时、curl 中断）是**正常现象**，不该在监控台控制台
        刷出堆栈——真实错误仍然会照常记录。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖签名
        """默认访问日志走 stderr；这里保持最简，不记录查询串（可能含游标）。"""
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        # 刻意不发 Access-Control-Allow-Origin：默认就不允许跨域读取。

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra=extra)

    def _ok(self, data: Any, meta: dict[str, Any] | None = None) -> None:
        payload = {
            "ok": True,
            "data": data,
            "meta": {
                "generated_at": utc_now().isoformat(),
                "next_cursor": None,
                "has_more": False,
            },
        }
        if meta:
            payload["meta"].update(meta)
        self._send_json(HTTPStatus.OK, payload)

    def _error(
        self, status: HTTPStatus, code: str, message: str, *, extra: dict[str, Any] | None = None
    ) -> None:
        """统一错误响应：稳定机器码 + 中文提示，不含堆栈/SQL/路径/正文。"""
        payload: dict[str, Any] = {"ok": False, "error": code, "message": message}
        if extra:
            payload.update(extra)
        self._send_json(status, payload)

    # -- 只读方法白名单 ---------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch(read_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(read_only=False)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch(read_only=False)

    def do_POST(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()

    def _reject_write(self) -> None:
        """监控台是只读的：任何写方法都明确 405。"""
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "read_only",
            "监控台是只读的：只支持 GET / HEAD / OPTIONS，不提供任何写操作。",
            extra={"allowed": list(_READ_METHODS)},
        )

    # -- 路由 -------------------------------------------------------------

    def _dispatch(self, *, read_only: bool) -> None:
        """路由入口：任何未预期异常都必须变成结构化 500，绝不能断开连接。

        直接让异常冒泡会让 ``BaseHTTPRequestHandler`` 关闭连接而不发任何响应，
        客户端只能看到 "Remote end closed connection"，既没有可读错误、也没有
        状态码。这里统一兜底，同时保证响应里不含堆栈、SQL 或路径。
        """
        try:
            self._dispatch_inner()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（浏览器取消、探针超时）：不是服务错误，不记日志。
            return
        except Exception:  # noqa: BLE001 - 顶层兜底是有意的
            LOGGER.exception(
                "监控台处理请求时发生未预期错误",
                extra={"event": "dashboard.request_failed", "context": {"path": self.path}},
            )
            try:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "服务内部错误。请查看控制台日志；响应里不会包含细节以避免泄露。",
                )
            except Exception:  # pragma: no cover - 连错误都发不出去时只能放弃
                return

    def _dispatch_inner(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if self.command == "OPTIONS":
            self._send_bytes(
                HTTPStatus.NO_CONTENT,
                b"",
                "text/plain; charset=utf-8",
                extra={"Allow": ", ".join(_READ_METHODS)},
            )
            return

        if path.startswith("/api"):
            self._dispatch_api(path, query)
            return
        self._dispatch_static(path)

    def _dispatch_api(self, path: str, query: dict[str, list[str]]) -> None:
        # 健康检查不需要令牌：它只返回服务自身状态，不含任何业务数据。
        if path == "/api/health":
            self._ok(self.queries.health())
            return

        if not self._authorized():
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "缺少或无效的访问令牌。请使用启动时打印的带令牌 URL 打开监控台。",
                extra={"www_authenticate": "Bearer"},
            )
            return

        try:
            self._route_api(path, query)
        except CursorError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_argument", str(exc))
        except ResourceGoneError as exc:
            self._error(
                HTTPStatus.NOT_FOUND,
                "resource_gone",
                f"{exc}（数据可能已被其他进程删除，请刷新列表）",
            )
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "database_busy",
                    "数据库正忙（可能正在被写入）。请稍后重试。",
                )
            else:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "database_error",
                    "数据库查询失败。请运行 doctor 命令查看详情。",
                )
        except ReadOnlyDatabaseError as exc:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "database_unavailable",
                f"无法只读访问数据库：{exc}",
            )

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        prefix = "bearer "
        if not header.lower().startswith(prefix):
            return False
        return self.token.matches(header[len(prefix) :].strip())

    def _route_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/overview":
            self._ok(self.queries.overview())
            return
        if path == "/api/diagnostics":
            self._ok(self.queries.diagnostics())
            return

        if path == "/api/accounts":
            page = self.queries.accounts(
                page_size=_int_param(query, "page_size"),
                cursor=_str_param(query, "cursor"),
                host_type=_str_param(query, "host_type"),
                presence=_str_param(query, "presence"),
            )
            self._ok(page.items, self._page_meta(page))
            return

        match = re.fullmatch(r"/api/accounts/([^/]+)", path)
        if match:
            account = self.queries.account(_unquote(match.group(1)))
            if account is None:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "账号不存在")
                return
            self._ok(account)
            return

        if path == "/api/conversations":
            page = self.queries.conversations(
                page_size=_int_param(query, "page_size"),
                cursor=_str_param(query, "cursor"),
                account_id=_str_param(query, "account_id"),
                unread_only=_bool_param(query, "unread_only"),
                blocked=_optional_bool_param(query, "blocked"),
            )
            self._ok(page.items, self._page_meta(page))
            return

        match = re.fullmatch(r"/api/conversations/([^/]+)/messages", path)
        if match:
            page = self.queries.messages(
                _unquote(match.group(1)),
                page_size=_int_param(query, "page_size"),
                after_message_id=_str_param(query, "after_message_id"),
                before_message_id=_str_param(query, "before_message_id"),
            )
            self._ok(page.items, self._page_meta(page))
            return

        match = re.fullmatch(r"/api/conversations/([^/]+)", path)
        if match:
            conversation = self.queries.conversation(_unquote(match.group(1)))
            if conversation is None:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "对话不存在")
                return
            self._ok(conversation)
            return

        if path == "/api/deliveries":
            page = self.queries.deliveries(
                page_size=_int_param(query, "page_size"),
                cursor=_str_param(query, "cursor"),
                state=_str_param(query, "state"),
                account_id=_str_param(query, "account_id"),
                conversation_id=_str_param(query, "conversation_id"),
            )
            self._ok(page.items, self._page_meta(page))
            return

        self._error(HTTPStatus.NOT_FOUND, "not_found", "未知的 API 路径")

    @staticmethod
    def _page_meta(page) -> dict[str, Any]:
        return page.to_meta()

    def _dispatch_static(self, path: str) -> None:
        route = STATIC_ROUTES.get(path)
        if route is None:
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                b"not found",
                "text/plain; charset=utf-8",
            )
            return
        filename, content_type = route
        target = STATIC_DIR / filename
        if not target.is_file():  # pragma: no cover - 打包缺失时才会发生
            self._send_bytes(
                HTTPStatus.NOT_FOUND, b"asset missing", "text/plain; charset=utf-8"
            )
            return
        self._send_bytes(HTTPStatus.OK, target.read_bytes(), content_type)


def _str_param(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _int_param(query: dict[str, list[str]], name: str) -> int | None:
    raw = _str_param(query, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise CursorError(f"{name} 必须是整数") from exc


def _bool_param(query: dict[str, list[str]], name: str) -> bool:
    value = _optional_bool_param(query, name)
    return bool(value)


def _optional_bool_param(query: dict[str, list[str]], name: str) -> bool | None:
    raw = _str_param(query, name)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise CursorError(f"{name} 必须是布尔值（1/0、true/false）")


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


class PortUnavailableError(RuntimeError):
    """端口不可用。刻意不自动换端口：静默换端口会让用户访问到错误的地址。"""


class DashboardServer:
    """把查询门面接到回环 HTTP 服务上。

    生命周期刻意简单：``start()`` 起线程、``stop()`` 关服务并 join 线程，
    两次连续启停不会留下线程、套接字或数据库连接。
    """

    def __init__(
        self,
        queries: DashboardQueries,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: DashboardToken | None = None,
        verbose: bool = False,
    ) -> None:
        self.queries = queries
        self.host = host
        self.port = port
        self.token = token or DashboardToken()
        self.verbose = verbose
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 地址 -------------------------------------------------------------

    @property
    def is_loopback(self) -> bool:
        return _is_loopback(self.host)

    @property
    def bound_port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self.port

    def url(self, *, include_token: bool = True) -> str:
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        suffix = f"/?token={self.token.value}" if include_token else "/"
        return f"http://{host}:{self.bound_port}{suffix}"

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "DashboardServer":
        """启动服务。端口冲突时抛出可操作的错误，不静默换端口。"""
        if self._httpd is not None:
            return self

        handler = type(
            "_BoundHandler",
            (_Handler,),
            {"queries": self.queries, "token": self.token},
        )

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False
            verbose = self.verbose

        try:
            self._httpd = _Server((self.host, self.port), handler)
        except OSError as exc:
            raise PortUnavailableError(
                f"无法在 {self.host}:{self.port} 启动监控台：{exc}。"
                "该端口可能已被占用；请用 --port 指定另一个端口。"
            ) from exc
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mailbox-dashboard", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止服务并释放线程与套接字。可重复调用。"""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def __enter__(self) -> "DashboardServer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        self.stop()
        return None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and bool(self._thread and self._thread.is_alive())


def serve_dashboard(
    database_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    verbose: bool = False,
    banner: Callable[[str], None] = print,
) -> int:
    """CLI 入口：启动并阻塞直到 Ctrl+C。"""
    import time

    queries = DashboardQueries(database_path)
    server = DashboardServer(queries, host=host, port=port, verbose=verbose)
    try:
        server.start()
    except PortUnavailableError as exc:
        banner(f"错误：{exc}")
        return 1

    def say(line: str = "") -> None:
        """输出一行并**立即 flush**。

        不 flush 的话，当 stdout 被重定向到管道或文件时（脚本、CI、自动化验收）横幅会
        留在缓冲区里，调用方一直读不到地址与令牌，表现为"服务起来了但脚本挂住"。
        """
        banner(line)
        flush = getattr(sys.stdout, "flush", None)
        if callable(flush):
            try:
                flush()
            except (ValueError, OSError):  # pragma: no cover - 已关闭的流
                pass

    say("MCP 智能体邮箱 · 只读监控台")
    say(f"  数据库（只读）：{queries.database_path.name}")
    say(f"  访问地址：{server.url()}")
    say(f"  令牌：{server.token.masked}（只在本次启动有效，不写入任何文件）")
    say("  安全：仅本机回环、Bearer 鉴权、严格 CSP、无任何写操作")
    if not server.is_loopback:
        say("")
        say("  [!!] 安全警告：你正在非回环地址上监听，同一网络的其他机器可能访问本服务。")
        say("       监控台包含消息正文等敏感数据，且令牌会出现在 URL 里。")
        say("       除非你明确知道后果，请改用 --host 127.0.0.1。")
        say("")
    say("  按 Ctrl+C 停止。")

    try:
        while server.is_running:
            time.sleep(0.3)
    except KeyboardInterrupt:
        say("\n正在停止监控台…")
    finally:
        server.stop()
    say("监控台已停止，HTTP 线程与数据库连接均已释放。")
    return 0
