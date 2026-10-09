"""把当前 DSH 会话注册为邮箱账号（一次性运维脚本）。

用法：
    .venv/Scripts/python.exe -X utf8 tools/register_dsh_session.py \
        --session-id <native-session-id> --display-name DSH-A [--host-instance default]

为什么要这个脚本：DSH 启动 MCP 子进程时会过滤掉所有 ``DSH_`` 变量，因此邮箱 MCP
进程拿不到"我是哪个会话"。在 DSH 提供正式注入手段（profile 的 env、或 in-process
插件）之前，会话身份必须由**人在本机显式传入**——而不是让模型自己填一个字符串。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_agent_mailbox.config import load_settings  # noqa: E402
from mcp_agent_mailbox.daemon.broker import Broker  # noqa: E402
from mcp_agent_mailbox.domain.accounts import HostIdentity  # noqa: E402
from mcp_agent_mailbox.domain.presence import HostCapabilityLevel  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把当前 DSH 会话注册/恢复为邮箱账号")
    parser.add_argument("--session-id", required=True, help="原生会话 ID（DSH_SESSION_ID）")
    parser.add_argument("--display-name", required=True, help="账号显示名，例如 DSH-A")
    parser.add_argument("--host-type", default="dsh")
    parser.add_argument("--host-instance", default="default")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--capability",
        type=int,
        choices=[0, 1, 2],
        default=0,
        help="已验证的能力等级；DSH 目前只能诚实声明 0（tools-only）",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 MAILBOX_HOME）")
    args = parser.parse_args(argv)

    settings = load_settings(data_dir=args.data_dir)
    broker = Broker(settings)
    try:
        bound = broker.accounts.bind_session(
            HostIdentity(args.host_type, args.host_instance, args.session_id),
            display_name=args.display_name,
            workspace_hint=args.workspace,
            capability_level=HostCapabilityLevel(args.capability),
            adapter_name=f"{args.host_type}-explicit-operator",
            metadata={"registered_by": "operator_cli"},
        )
        view = broker.presence.for_account(bound.account_id)
        print("已注册/恢复账号")
        print(f"  account_id     : {bound.account_id}")
        print(f"  display_name   : {bound.account.display_name}")
        print(f"  host           : {bound.account.host_type} / {bound.account.host_instance_id}")
        print(f"  native_session : {bound.account.native_session_id}")
        print(f"  connection_id  : {bound.connection_id}（代次 {bound.generation}）")
        print(f"  capability     : Level {int(bound.connection.capability_level)}"
              f" · {bound.connection.capability_level.slug}")
        print(f"  durable        : {bound.connection.durable}"
              "（True = 不按租约离线，进程活着即可寻址）")
        print(f"  presence       : {view.state.value} — {view.capability_slug}")
        print(f"  can_wake       : {view.capability_level >= 2}")
        print(f"  首次创建       : {bound.created}")
        return 0
    finally:
        broker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
