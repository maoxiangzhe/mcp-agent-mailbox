"""命令行入口：``python -m mcp_agent_mailbox.cli <命令>``。

命令一览：
    serve        以 stdio 运行 MCP 服务器（客户端注册用）
    migrate      应用数据库迁移（幂等）
    doctor       诊断：数据库完整性、账号与在线状态、待投递、死信、设置
    accounts     列出账号与在线状态
    conversations 列出对话（按账号）
    deliveries   列出某账号待投递
    tools        列出本进程会暴露的 MCP 工具
    adapters     宿主机适配器能力矩阵（如实标注已验证/未验证）
    dashboard    启动只读 Web 监控台（仅本机回环 + Bearer 鉴权）
    install      把 MCP 服务器注册进本机已安装的客户端

输出统一为 UTF-8，Windows 控制台也不会因编码崩掉。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

__all__ = ["main"]


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _harden_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - 老式控制台
        pass


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_serve(args) -> int:
    from .mcp.server import serve_stdio

    forwarded: list[str] = []
    for flag, value in (
        ("--data-dir", args.data_dir),
        ("--database", args.database),
        ("--host-type", args.host_type),
    ):
        if value:
            forwarded += [flag, value]
    if args.capability is not None:
        forwarded += ["--capability", str(args.capability)]
    if args.allow_adapter_registration:
        forwarded.append("--allow-adapter-registration")
    if args.no_adapter_tools:
        forwarded.append("--no-adapter-tools")
    return serve_stdio(forwarded)


def cmd_migrate(args) -> int:
    from .config import load_settings
    from .infrastructure.sqlite import apply_migrations, initialize, schema_version

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    database = initialize(settings.database_path)
    try:
        applied = apply_migrations(database.connection())
        _print_json(
            {
                "ok": True,
                "database_path": str(settings.database_path),
                "schema_version": schema_version(database.connection()),
                "applied": applied,
                "note": "迁移幂等：重复执行不会重复应用；失败会整体回滚该迁移。",
            }
        )
    finally:
        database.close()
    return 0


def cmd_doctor(args) -> int:
    from .daemon.broker import Broker
    from .config import load_settings

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    broker = Broker(settings)
    try:
        report = broker.diagnose()
        report["ok"] = report.get("integrity_check") == "ok"
    finally:
        broker.stop()
    _print_json(report)
    return 0 if report.get("ok") else 1


def cmd_accounts(args) -> int:
    from .config import load_settings
    from .daemon.broker import Broker

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    broker = Broker(settings)
    try:
        with broker.unit_of_work().transaction() as uow:
            accounts = uow.accounts.list_accounts(
                host_type=args.host_type, limit=args.limit
            )
        presence = broker.presence.for_accounts(accounts)
        rows = []
        for account in accounts:
            view = presence[account.account_id]
            rows.append({**account.to_dict(), **view.to_dict()})
        _print_json({"ok": True, "count": len(rows), "accounts": rows})
    finally:
        broker.stop()
    return 0


def cmd_conversations(args) -> int:
    from .config import load_settings
    from .daemon.broker import Broker

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    broker = Broker(settings)
    try:
        with broker.unit_of_work().transaction() as uow:
            summaries, next_cursor = uow.conversations.list_for_account(
                args.account_id, limit=args.limit
            )
        _print_json(
            {
                "ok": True,
                "account_id": args.account_id,
                "count": len(summaries),
                "next_cursor": next_cursor,
                "conversations": [
                    {
                        **summary.conversation.to_dict(),
                        "counterpart_account_id": summary.counterpart_account_id,
                        "unread_count": summary.unread_count,
                        "last_message_id": summary.last_message_id,
                    }
                    for summary in summaries
                ],
            }
        )
    finally:
        broker.stop()
    return 0


def cmd_deliveries(args) -> int:
    from .config import load_settings
    from .daemon.broker import Broker

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    broker = Broker(settings)
    try:
        pending = broker.deliveries.pending_for_account(args.account_id, limit=args.limit)
        _print_json(
            {
                "ok": True,
                "account_id": args.account_id,
                "count": len(pending),
                "deliveries": [item.to_dict() for item in pending],
                "note": "queued 表示目标离线时仍在队列里等待补投。",
            }
        )
    finally:
        broker.stop()
    return 0


def cmd_tools(args) -> int:
    import asyncio

    from .mcp.server import build_server

    server = build_server(
        allow_adapter_registration=args.allow_adapter_registration,
        include_adapter_tools=not args.no_adapter_tools,
    )
    try:
        listing = asyncio.run(server.mcp.list_tools())
        _print_json(
            {
                "ok": True,
                "count": len(listing),
                "tools": [
                    {
                        "name": tool.name,
                        "description": (tool.description or "").strip().splitlines()[0]
                        if tool.description
                        else "",
                        "parameters": sorted((tool.inputSchema or {}).get("properties", {})),
                        "required": (tool.inputSchema or {}).get("required", []),
                    }
                    for tool in listing
                ],
                "note": "发送类工具不含 from/agent 参数：发送者由连接上下文决定。",
            }
        )
    finally:
        server.stop()
    return 0


def cmd_adapters(args) -> int:
    from .adapters import capability_matrix

    _print_json({"ok": True, **capability_matrix()})
    return 0


def cmd_dashboard(args) -> int:
    """启动只读 Web 监控台。

    刻意不提供任何修改类参数：监控台没有"顺手改一下"的入口。
    """
    from .config import load_settings
    from .dashboard.server import serve_dashboard

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    if not settings.database_path.exists():
        print(
            f"错误：数据库不存在：{settings.database_path}\n"
            "请先运行：uv run python -m mcp_agent_mailbox.cli migrate",
            file=sys.stderr,
        )
        return 1
    return serve_dashboard(
        settings.database_path,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
    )


def cmd_inbox(args) -> int:
    """收件侧开工入口（CLI 版）：列出某账号的待办消息，并给出可直接开工的上下文。

    只读：不改变投递状态、不推进已读位点、不发送任何东西。
    """
    from .config import load_settings
    from .daemon.broker import Broker

    settings = load_settings(data_dir=args.data_dir, database_path=args.database)
    broker = Broker(settings)
    try:
        with broker.unit_of_work().transaction() as uow:
            account = uow.accounts.get(args.account_id)
            if account is None:
                print(f"错误：账号不存在：{args.account_id}", file=sys.stderr)
                return 1
            summaries, _cursor = uow.conversations.list_for_account(
                account.account_id, unread_only=True, limit=100
            )
            messages: list[dict[str, Any]] = []
            for summary in summaries:
                page = uow.messages.list_for_conversation(
                    summary.conversation.conversation_id,
                    limit=100,
                    viewer_account_id=account.account_id,
                )
                for view in page.items:
                    message = view.message
                    if message.sender_account_id == account.account_id:
                        continue
                    if view.visibility.value != "unread":
                        continue
                    sender = uow.accounts.get(message.sender_account_id)
                    messages.append(
                        {
                            "conversation_id": message.conversation_id,
                            "message_id": message.message_id,
                            "from": sender.display_name if sender else message.sender_account_id,
                            "sent_at": message.created_at.isoformat(),
                            "content": message.content,
                            "reply_with": (
                                f"reply_message(message_id='{message.message_id}', text='…')"
                            ),
                            "mark_done_with": (
                                f"set_message_status(message_id='{message.message_id}', "
                                "processing='completed')"
                            ),
                        }
                    )
            presence = broker.presence.for_account(account.account_id)
        _print_json(
            {
                "ok": True,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "presence": presence.state.value,
                "host_pid": presence.host_pid,
                "host_process_alive": presence.state.is_online,
                "pending_total": len(messages),
                "messages": messages,
                "note": (
                    "本命令只读。要开工：按 reply_with 回结论，按 mark_done_with 回执。"
                    "回执不等于回复，结论必须用 reply_message 发出去。"
                ),
            }
        )
    finally:
        broker.stop()
    return 0


def cmd_install(args) -> int:
    from pathlib import Path

    import install as legacy_installer

    argv = ["install.py"]
    if args.target:
        argv += ["--target", args.target]
    if args.check:
        argv.append("--check")
    if args.dry_run:
        argv.append("--dry-run")
    argv += ["--module", "mcp_agent_mailbox"]
    return legacy_installer.main(argv)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 MAILBOX_HOME）")
    parser.add_argument("--database", default=None, help="数据库文件路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-agent-mailbox",
        description="会话寻址的 MCP 智能体邮箱：一台 Broker，一个会话一个账号。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="以 stdio 运行 MCP 服务器")
    _add_common(serve)
    serve.add_argument("--host-type", default=None)
    serve.add_argument("--capability", type=int, choices=[0, 1, 2], default=None)
    serve.add_argument("--allow-adapter-registration", action="store_true")
    serve.add_argument("--no-adapter-tools", action="store_true")
    serve.set_defaults(func=cmd_serve)

    migrate = sub.add_parser("migrate", help="应用数据库迁移")
    _add_common(migrate)
    migrate.set_defaults(func=cmd_migrate)

    doctor = sub.add_parser("doctor", help="诊断数据库与投递状态")
    _add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    accounts = sub.add_parser("accounts", help="列出账号与在线状态")
    _add_common(accounts)
    accounts.add_argument("--host-type", default=None)
    accounts.add_argument("--limit", type=int, default=200)
    accounts.set_defaults(func=cmd_accounts)

    inbox = sub.add_parser("inbox", help="收件侧开工入口：列出某账号的待办消息（只读）")
    _add_common(inbox)
    inbox.add_argument("account_id")
    inbox.set_defaults(func=cmd_inbox)

    conversations = sub.add_parser("conversations", help="列出某账号参与的对话")
    _add_common(conversations)
    conversations.add_argument("account_id")
    conversations.add_argument("--limit", type=int, default=50)
    conversations.set_defaults(func=cmd_conversations)

    deliveries = sub.add_parser("deliveries", help="列出某账号待投递消息")
    _add_common(deliveries)
    deliveries.add_argument("account_id")
    deliveries.add_argument("--limit", type=int, default=100)
    deliveries.set_defaults(func=cmd_deliveries)

    tools = sub.add_parser("tools", help="列出本进程暴露的 MCP 工具")
    tools.add_argument("--allow-adapter-registration", action="store_true")
    tools.add_argument("--no-adapter-tools", action="store_true")
    tools.set_defaults(func=cmd_tools)

    adapters = sub.add_parser("adapters", help="宿主适配器能力矩阵")
    adapters.set_defaults(func=cmd_adapters)

    dashboard = sub.add_parser(
        "dashboard", help="启动只读 Web 监控台（默认 127.0.0.1:8765）"
    )
    _add_common(dashboard)
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址；默认只绑回环。非回环地址会输出安全警告",
    )
    dashboard.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    dashboard.add_argument(
        "--verbose", action="store_true", help="输出 HTTP 访问日志（默认关闭）"
    )
    dashboard.set_defaults(func=cmd_dashboard)

    installer = sub.add_parser("install", help="把 MCP 服务器注册进本机客户端")
    installer.add_argument("--target", default="auto")
    installer.add_argument("--check", action="store_true")
    installer.add_argument("--dry-run", action="store_true")
    installer.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    _harden_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一报错，不打印栈给用户
        print(f"错误：{exc}", file=sys.stderr)
        if "--debug" in (argv if argv is not None else sys.argv):
            raise
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
