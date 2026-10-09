"""Bind a Codex mailbox to the real host PID and run delivery until it exits.

Operator-supplied session/PID; does not infer an identity from a global MCP.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_agent_mailbox.config import load_settings
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.domain.accounts import HostIdentity
from mcp_agent_mailbox.domain.process import is_process_alive, process_started_at


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--host-pid", type=int, required=True)
    parser.add_argument("--account-name", default="codex-A")
    parser.add_argument("--host-instance", default="default")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    started = process_started_at(args.host_pid)
    if started is None or not is_process_alive(args.host_pid):
        parser.error("The supplied host process is not alive or cannot be identified")
    broker = Broker(load_settings(data_dir=args.data_dir))
    bound = broker.accounts.bind_session(
        HostIdentity("codex", args.host_instance, args.session_id),
        display_name=args.account_name, host_pid=args.host_pid,
        adapter_name="codex-desktop-host", metadata={"host_started_at": str(started)})
    print(json.dumps({"account_id": bound.account_id, "host_pid": args.host_pid,
                      "presence": broker.presence.for_account(bound.account_id).to_dict()},
                     ensure_ascii=False), flush=True)
    broker.start()
    try:
        while is_process_alive(args.host_pid) and process_started_at(args.host_pid) == started:
            with broker.unit_of_work().transaction() as uow:
                current = uow.connections.current_for_account(bound.account_id)
            if current is None or current.connection_id != bound.connection_id:
                break  # A new owner/reconnect superseded us; do not fight it.
            broker.accounts.renew(bound.connection_id)
            time.sleep(10)
    finally:
        broker.accounts.close_connection(bound.connection_id, reason="host_bridge_stopped")
        broker.stop()


if __name__ == "__main__":
    main()
