from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_installer():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("mailbox_installer", root / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_install_forwards_dynamic_session_identity(monkeypatch, tmp_path):
    install = _load_installer()
    assert "connect_mailbox" in install.NEW_TOOLS
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "keep-me"\n\n[features]\nmemories = true\n', encoding="utf-8")

    server = install.ServerSpec("C:\\mailbox\\python.exe", module_mode=True)
    assert install.update_codex(server, dry_run=False)

    text = config.read_text(encoding="utf-8")
    assert 'model = "keep-me"' in text
    assert "[features]" in text
    assert '[mcp_servers.mcp-agent-mailbox]' in text
    assert '[mcp_servers.board-mcp]' not in text
    assert f"cwd = '{install.HERE}'" in text
    assert "'--allow-adapter-registration'" in text
    assert install._read_toml(text)['mcp_servers'][install.CODEX_SERVER_NAME]['env_vars'] == [
        'CODEX_SESSION_ID', 'CODEX_THREAD_ID']
    assert "MAILBOX_HOST_TYPE = 'codex'" in text
    assert "MAILBOX_SESSION_ID" not in text


def test_codex_install_is_idempotent_and_keeps_env_forwarding(monkeypatch, tmp_path):
    install = _load_installer()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.board-mcp]\n'
        'type = "stdio"\n'
        "command = 'old-python'\n"
        "args = ['old.py']\n"
        '[mcp_servers.board-mcp.env]\n'
        "MAILBOX_HOST_TYPE = 'codex'\n"
        '\n[desktop]\nfollowUpQueueMode = "steer"\n',
        encoding="utf-8",
    )

    server = install.ServerSpec("C:\\mailbox\\python.exe", module_mode=True)
    assert install.update_codex(server, dry_run=False)
    first = config.read_text(encoding="utf-8")
    assert install.update_codex(server, dry_run=False)
    second = config.read_text(encoding="utf-8")

    assert first == second
    assert first.count("'--allow-adapter-registration'") == 1
    assert install._read_toml(first)['mcp_servers'][install.CODEX_SERVER_NAME]['env_vars'] == [
        'CODEX_SESSION_ID', 'CODEX_THREAD_ID']
    assert install._read_toml(first)['mcp_servers']['board-mcp']['command'] == 'old-python'
    assert '[desktop]\nfollowUpQueueMode = "steer"' in first
