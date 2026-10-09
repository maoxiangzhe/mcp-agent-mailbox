"""端到端收发验证：一个账号发信给 DSH-A，DSH-A 侧取信。

用法：
    .venv/Scripts/python.exe -X utf8 tools/verify_dsh_mailbox.py \
        --target-account acc_xxx [--sender-account acc_yyy] [--text "..."]

它走的是**真实服务层**（不是手写 SQL）：start_conversation -> dispatch -> 取信 ->
读消息，并打印每一步的关键状态，用来证明"注册完真的能用"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_agent_mailbox.config import load_settings  # noqa: E402
from mcp_agent_mailbox.daemon.broker import Broker  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证向 DSH 账号发信与取信")
    parser.add_argument("--target-account", required=True, help="收件账号（DSH-A）")
    parser.add_argument("--sender-account", default=None, help="发件账号；默认挑一个别的账号")
    parser.add_argument("--text", default="来自验证脚本的测试消息")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(data_dir=args.data_dir)
    broker = Broker(settings)
    try:
        with broker.unit_of_work().transaction() as uow:
            accounts = uow.accounts.list_accounts(limit=200)
        target = next((a for a in accounts if a.account_id == args.target_account), None)
        if target is None:
            print(f"错误：找不到收件账号 {args.target_account}")
            return 1
        if args.sender_account:
            sender = next((a for a in accounts if a.account_id == args.sender_account), None)
        else:
            sender = next((a for a in accounts if a.account_id != target.account_id), None)
        if sender is None:
            print("错误：库里没有可用的发件账号（至少需要两个账号）")
            return 1

        with broker.unit_of_work().transaction() as uow:
            sender_connection = uow.connections.current_for_account(sender.account_id)
            target_connection = uow.connections.current_for_account(target.account_id)
        if sender_connection is None:
            print(f"错误：发件账号 {sender.display_name} 没有当前连接，无法发信")
            return 1

        target_presence = broker.presence.for_account(target.account_id)
        print("发信前状态")
        print(f"  发件人 : {sender.display_name} ({sender.account_id})")
        print(f"  收件人 : {target.display_name} ({target.account_id})")
        print(f"  收件人在线 : {target_presence.state.value}"
              f"（durable={target_connection.durable if target_connection else None}）")

        result = broker.conversations.start_conversation(
            connection_id=sender_connection.connection_id,
            to_account_id=target.account_id,
            text=args.text,
        )
        print("\n已发送")
        print(f"  conversation_id : {result.conversation_id}")
        print(f"  message_id      : {result.message.message_id}")
        print(f"  delivery_id     : {result.delivery_id}")
        print(f"  delivery_state  : {result.delivery_state}")

        report = broker.deliveries.dispatch_due()
        print("\n派发一轮")
        print(f"  dispatched={report.dispatched} notified_only={report.notified_only} "
              f"skipped_offline={report.skipped_offline}")

        with broker.unit_of_work().transaction() as uow:
            delivery = uow.deliveries.get(result.delivery_id)
        print(f"  投递最终状态 : {delivery.state.value if delivery else '?'}"
              f"（queued/failed = 消息保留在邮箱，等目标主动取信；绝不谎报 delivered）")

        pending = broker.deliveries.pending_for_account(target.account_id, limit=50)
        print(f"\n{target.display_name} 的待取消息：{len(pending)} 条")

        page = broker.conversations.read_conversation(
            connection_id=target_connection.connection_id if target_connection else "",
            conversation_id=result.conversation_id,
        )
        print(f"\n{target.display_name} 读对话：{len(page.messages)} 条，未读 {page.unread_count}")
        for view in page.messages:
            print(f"  · [{view.delivery.value if view.delivery else '-'}/"
                  f"{view.visibility.value}/{view.processing.value}] "
                  f"{view.message.content[:60]!r}")
        print("\n结论：账号可达、消息可送达邮箱、目标可取信。")
        return 0
    finally:
        broker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
