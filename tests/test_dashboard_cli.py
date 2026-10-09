"""监控台 CLI 接线测试。

只验证参数解析与"命令确实存在、默认只绑回环、不提供任何修改类参数"。
真正的 HTTP 行为由 ``test_dashboard_api.py`` 覆盖。
"""

from __future__ import annotations

import pytest

from mcp_agent_mailbox.cli import build_parser


def test_dashboard_subcommand_exists_with_loopback_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.verbose is False
    assert callable(args.func)


def test_dashboard_accepts_explicit_host_and_port() -> None:
    parser = build_parser()
    args = parser.parse_args(["dashboard", "--host", "0.0.0.0", "--port", "9000"])
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_dashboard_common_options_are_present() -> None:
    parser = build_parser()
    args = parser.parse_args(["dashboard", "--data-dir", "X", "--database", "Y.sqlite3"])
    assert args.data_dir == "X"
    assert args.database == "Y.sqlite3"


@pytest.mark.parametrize(
    "flag",
    ["--delete", "--reset", "--write", "--migrate", "--fix", "--repair", "--force"],
)
def test_dashboard_rejects_modifying_flags(flag: str) -> None:
    """监控台没有、也不接受任何"顺手改一下"的参数。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["dashboard", flag])


def test_existing_commands_still_parse() -> None:
    """新增子命令不得影响既有 CLI 行为。"""
    parser = build_parser()
    for argv in (
        ["migrate"],
        ["doctor"],
        ["accounts"],
        ["conversations", "acc_x"],
        ["deliveries", "acc_x"],
        ["tools"],
        ["adapters"],
        ["install", "--check"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func), argv


def test_dashboard_reports_missing_database(tmp_path, capsys) -> None:
    from mcp_agent_mailbox.cli import cmd_dashboard

    class _Args:
        data_dir = str(tmp_path / "empty")
        database = None
        host = "127.0.0.1"
        port = 8765
        verbose = False

    code = cmd_dashboard(_Args())
    assert code == 1
    captured = capsys.readouterr()
    assert "migrate" in captured.err
