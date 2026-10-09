"""Shell fallback when the current Codex chat has no mailbox MCP tools loaded.

Uses only this shell's CODEX_SESSION_ID and an existing host connection;
never registers, replaces the host connection, or accepts a sender account.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_agent_mailbox.daemon.broker import Broker
from mcp_agent_mailbox.config import load_settings
from mcp_agent_mailbox.domain.accounts import HostIdentity
from mcp_agent_mailbox.infrastructure.sqlite import Database
from mcp_agent_mailbox.mcp.connection_context import MailboxConnection, UnavailableSessionProvider
from mcp_agent_mailbox.mcp.tools import ToolRuntime, mailbox_inbox


def resolve_mailbox_settings(*, environ=None, config_path=None):
    """Resolve the configured Codex mailbox, never a guessed account or PID.

    An explicit directory is an operator override. Otherwise use the enabled
    mailbox's actual configuration, preferring its new registration name.
    Invalid/missing registration fails before opening any database.
    """
    source = os.environ if environ is None else environ
    for key in ('MAILBOX_HOME', 'BOARD_MCP_ROOT'):
        root = source.get(key, '').strip()
        if root:
            return load_settings(data_dir=Path(root).expanduser()), (
                source.get('MAILBOX_HOST_INSTANCE_ID', '').strip() or 'default')
    cfg = Path(config_path) if config_path is not None else (
        Path(source['CODEX_HOME']).expanduser() if source.get('CODEX_HOME', '').strip()
        else Path.home() / '.codex') / 'config.toml'
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        config = tomllib.loads(cfg.read_text(encoding='utf-8'))
    except (OSError, ValueError, ImportError) as exc:
        raise ValueError(f'Cannot read Codex mailbox configuration at {cfg}; '
                         'refusing to open the default database') from exc
    servers = config.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('Codex mcp_servers must be a configuration table')
    selected = None
    for name in ('mcp-agent-mailbox', 'board-mcp'):
        entry = servers.get(name)
        if not isinstance(entry, dict) or entry.get('enabled', True) is not True:
            continue
        args = entry.get('args', [])
        if (isinstance(args, list)
                and all(isinstance(arg, str) for arg in args)
                and any(args[i:i + 2] == ['-m', 'mcp_agent_mailbox.cli']
                        for i in range(len(args) - 1))
                and 'serve' in args):
            selected = entry
            break
    if selected is None:
        raise ValueError('No enabled Codex MCP registration runs mcp_agent_mailbox.cli; '
                         'refusing to open the default database')
    env = selected.get('env', {})
    if not isinstance(env, dict):
        raise ValueError('Codex mailbox env must be a configuration table')
    for key in ('MAILBOX_HOME', 'BOARD_MCP_ROOT', 'MAILBOX_HOST_INSTANCE_ID'):
        if key in env and not isinstance(env[key], str):
            raise ValueError(f'Codex mailbox {key} must be a string')
    root = env.get('MAILBOX_HOME', '').strip() or env.get('BOARD_MCP_ROOT', '').strip()
    # CLI flags override configured env in the native MCP server as well.
    flags = {}
    args = selected['args']
    for flag in ('--data-dir', '--database'):
        if flag in args:
            index = args.index(flag)
            if index + 1 >= len(args) or args[index + 1].startswith('--'):
                raise ValueError(f'Codex mailbox {flag} requires a path')
            flags[flag] = args[index + 1]
    root = flags.get('--data-dir', root)
    data_dir = Path(root).expanduser() if root else Path.home() / '.board-mcp'
    database = Path(flags['--database']).expanduser() if '--database' in flags else None
    # Paths relative to an explicit server cwd must not be interpreted relative
    # to this unrelated shell workspace.
    relative_paths = [path for path in (data_dir, database) if path is not None and not path.is_absolute()]
    if relative_paths:
        cwd = selected.get('cwd')
        if not isinstance(cwd, str) or not Path(cwd).expanduser().is_absolute():
            raise ValueError('Relative Codex mailbox paths require an explicit absolute server cwd')
        base = Path(cwd).expanduser()
        data_dir = data_dir if data_dir.is_absolute() else base / data_dir
        if database is not None and not database.is_absolute():
            database = base / database
    instance = source.get('MAILBOX_HOST_INSTANCE_ID', '').strip() or env.get(
        'MAILBOX_HOST_INSTANCE_ID', '').strip() or 'default'
    return load_settings(data_dir=data_dir, database_path=database), instance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inbox", "reply", "complete"])
    parser.add_argument("--message-id")
    parser.add_argument("--text")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cursor")
    args = parser.parse_args()
    session_id = os.environ.get("CODEX_SESSION_ID")
    if not session_id:
        parser.error("CODEX_SESSION_ID is required; identity is never guessed")
    try:
        settings, host_instance = resolve_mailbox_settings()
    except ValueError as exc:
        parser.error(str(exc))
    if not settings.database_path.is_file():
        parser.error(f'Configured mailbox database does not exist: {settings.database_path}; '
                     'this fallback never creates a database or registers an account')
    # Operate the existing schema; do not migrate or initialize a second store.
    broker = Broker(settings, database=Database(settings.database_path), auto_waker=False)
    try:
        with broker.unit_of_work().transaction() as uow:
            account = uow.accounts.find_by_identity(HostIdentity("codex", host_instance, session_id))
            connection = uow.connections.current_for_account(account.account_id) if account else None
        if connection is None or not broker.presence.for_account(account.account_id).is_online:
            parser.error("Current Codex chat has no online mailbox connection")
        if args.action == "inbox":
            binding = MailboxConnection(provider=UnavailableSessionProvider("Use existing connection only"))
            binding.account_id = account.account_id
            binding.connection_id = connection.connection_id
            binding.generation = connection.generation
            output = mailbox_inbox(
                ToolRuntime(broker, binding), limit=args.limit, cursor=args.cursor
            )
        else:
            if not args.message_id:
                parser.error("--message-id is required")
            if args.action == "reply":
                if not args.text:
                    parser.error("--text is required")
                output = broker.conversations.reply_message(connection_id=connection.connection_id,
                    message_id=args.message_id, text=args.text,
                    idempotency_key="codex-shell-reply:" + args.message_id + ":" + args.text).to_dict()
            else:
                output = broker.conversations.set_message_status(connection_id=connection.connection_id,
                    message_id=args.message_id, processing="completed").to_dict()
        print(json.dumps(output, ensure_ascii=False, default=str))
    finally:
        broker.stop()


if __name__ == "__main__":
    main()
