"""安装验收：仅迁移本项目旧注册，保留用户配置，重复安装不改变结果。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def installer(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("mailbox_acceptance_installer", root / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    return module


@pytest.mark.parametrize("legacy", [False, True])
def test_new_and_legacy_servers_have_distinct_install_names(installer, legacy):
    spec = installer.ServerSpec("python", module_mode=not legacy)
    for target in installer.TARGETS:
        expected = ("board-mcp" if target == "codex" else "board") if legacy else "mcp-agent-mailbox"
        assert spec.server_name(target) == expected


def test_codex_migration_keeps_user_options_and_is_repeatable(installer, tmp_path):
    tomllib = pytest.importorskip("tomllib")
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('''model = "keep-model"
[mcp_servers.other]
command = "other"
[mcp_servers.board-mcp]
command = "old-python"
args = ["-m", "mcp_agent_mailbox.cli", "serve"]
enabled = false
startup_timeout_sec = 41
tool_timeout_sec = 53
env_vars = ["USER_TOKEN"]
[mcp_servers.board-mcp.env]
CUSTOM = "保留"
MAILBOX_HOME = "isolated-existing-mailbox"
MAILBOX_SESSION_ID = "explicit-old-session"
[desktop]
followUpQueueMode = "steer"
''', encoding="utf-8")
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    spec = installer.ServerSpec("new-python", module_mode=True)
    assert installer.update_codex(spec, False)
    first = config.read_text(encoding="utf-8")
    installed = tomllib.loads(first)
    assert "board-mcp" not in installed["mcp_servers"]
    entry = installed["mcp_servers"]["mcp-agent-mailbox"]
    assert entry["enabled"] is False
    assert entry["startup_timeout_sec"] == 41 and entry["tool_timeout_sec"] == 53
    assert set(entry["env_vars"]) == {"USER_TOKEN", "CODEX_SESSION_ID", "CODEX_THREAD_ID"}
    assert entry["env"]["CUSTOM"] == "保留"
    assert entry["env"]["MAILBOX_HOME"] == "isolated-existing-mailbox"
    assert entry["env"]["MAILBOX_SESSION_ID"] == "explicit-old-session"
    assert entry["command"] == "new-python"
    assert installed["mcp_servers"]["other"] == original["mcp_servers"]["other"]
    assert installed["desktop"] == original["desktop"]
    assert installed["model"] == original["model"]
    assert installer.update_codex(spec, False)
    assert config.read_text(encoding="utf-8") == first


def test_codex_unrelated_legacy_registration_survives_new_install(installer, tmp_path):
    tomllib = pytest.importorskip("tomllib")
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('''[mcp_servers.board-mcp]
command = "legacy-python"
args = ["legacy/server.py"]
[mcp_servers.board-mcp.env]
LEGACY_OPTION = "kept"
''', encoding="utf-8")
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    assert installer.update_codex(installer.ServerSpec("new-python", module_mode=True), False)
    installed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert installed["mcp_servers"]["board-mcp"] == original["mcp_servers"]["board-mcp"]
    assert installed["mcp_servers"]["mcp-agent-mailbox"]["command"] == "new-python"


def test_codex_existing_new_registration_takes_precedence_over_old_mailbox(installer, tmp_path):
    tomllib = pytest.importorskip("tomllib")
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('''[mcp_servers.board-mcp]
command = "old-python"
args = ["-m", "mcp_agent_mailbox.cli", "serve"]
enabled = true
[mcp_servers.board-mcp.env]
CUSTOM = "old"
MAILBOX_HOME = "old-home"
[mcp_servers.mcp-agent-mailbox]
command = "current-python"
args = ["-m", "mcp_agent_mailbox.cli", "serve"]
enabled = false
tool_timeout_sec = 73
[mcp_servers.mcp-agent-mailbox.env]
CUSTOM = "new"
MAILBOX_HOME = "new-home"
''', encoding="utf-8")
    assert installer.update_codex(installer.ServerSpec("installed-python", module_mode=True), False)
    servers = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]
    assert "board-mcp" not in servers
    assert servers["mcp-agent-mailbox"]["enabled"] is False
    assert servers["mcp-agent-mailbox"]["tool_timeout_sec"] == 73
    assert servers["mcp-agent-mailbox"]["env"]["CUSTOM"] == "new"
    assert servers["mcp-agent-mailbox"]["env"]["MAILBOX_HOME"] == "new-home"


@pytest.mark.parametrize("target,key,env_key", [("opencode", "mcp", "environment"), ("trae", "mcpServers", "env")])
@pytest.mark.parametrize("old_mailbox", [False, True])
def test_json_install_preserves_other_servers_and_migrates_only_mailbox(installer, tmp_path, target, key, env_key, old_mailbox):
    if target == "opencode":
        path = tmp_path / ".config" / "opencode" / "opencode.json"
        old_entry = {"type": "local", "command": ["old-python", "-m", "mcp_agent_mailbox.cli", "serve"] if old_mailbox else ["old-python", "legacy.py"], "enabled": False, env_key: {"CUSTOM": "保留"}}
    else:
        path = tmp_path / "appdata" / "Trae CN" / "User" / "mcp.json"
        old_entry = {"command": "old-python", "args": ["-m", "mcp_agent_mailbox.cli", "serve"] if old_mailbox else ["legacy.py"], "disabled": True, env_key: {"CUSTOM": "保留"}}
    path.parent.mkdir(parents=True)
    original = {"editor_setting": {"keep": True}, key: {"board": old_entry, "other": {"command": "unrelated", "custom": 12}}}
    path.write_text(json.dumps(original), encoding="utf-8")
    registration = installer.register_opencode if target == "opencode" else installer.register_trae
    spec = installer.ServerSpec("new-python", module_mode=True)
    assert registration(spec, False)
    first = path.read_text(encoding="utf-8")
    installed = json.loads(first)
    assert installed["editor_setting"] == original["editor_setting"]
    assert installed[key]["other"] == original[key]["other"]
    assert "mcp-agent-mailbox" in installed[key]
    if old_mailbox:
        assert "board" not in installed[key]
        assert installed[key]["mcp-agent-mailbox"][env_key]["CUSTOM"] == "保留"
        option = "enabled" if target == "opencode" else "disabled"
        assert installed[key]["mcp-agent-mailbox"][option] == old_entry[option]
    else:
        assert installed[key]["board"] == old_entry
    assert registration(spec, False)
    assert path.read_text(encoding="utf-8") == first


def test_codex_dry_run_does_not_mutate_config(installer, tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = b'[features]\nmemories = true\n'
    config.write_bytes(original)
    assert installer.update_codex(installer.ServerSpec("python", module_mode=True), True)
    assert config.read_bytes() == original
