"""验证"接入即注册 + 在线严格参照进程"这条产品规则。

Windows 注意：`.venv\\Scripts\\python.exe` 是 **launcher**，它会再拉起真正的解释器。
所以：
  * 库里的 `host_pid` 应当是**服务进程自己**（真解释器）的 PID；
  * 杀进程时必须杀**整棵进程树**，否则只杀掉 launcher、服务仍在跑，
    会把"账号离线"的结论测错。

用法：
    .venv/Scripts/python.exe -X utf8 tools/verify_process_presence.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
WORK = Path(r"E:\zcz\.tmp-presence-check")
SESSION_ID = "session-presence-check"
WAIT_SECONDS = 90


def cli(env: dict[str, str], *args: str) -> dict:
    result = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "mcp_agent_mailbox.cli", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    text = result.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_stderr": result.stderr.strip(), "_exit": result.returncode}


def account_of(payload: dict, session_id: str) -> dict | None:
    for row in payload.get("accounts", []):
        if row.get("native_session_id") == session_id:
            return row
    return None


def process_alive(pid: int) -> bool:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}",
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "yes"


def kill_tree(pid: int) -> None:
    """杀整棵进程树：只杀 launcher 会留下还在跑的服务进程。"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
    )


def kill_pid(pid: int) -> None:
    """直接杀掉指定 PID。

    这一步是关键：`.venv\\Scripts\\python.exe` 是 **launcher**，库里的 `host_pid`
    是它拉起的**真解释器**。只对 launcher 做 taskkill /T 在 Windows 上会失败
    （launcher 可能已经退出、或权限不足），于是"杀了进程"其实是空动作，
    测试就会把"进程还活着所以显示在线"误判成代码缺陷。
    所以这里必须按库里记录的 host_pid 精确杀。
    """
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue",
        ],
        capture_output=True,
        text=True,
    )


def kill_all_servers(env: dict[str, str]) -> None:
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
            "Where-Object { $_.CommandLine -match 'mcp_agent_mailbox.cli serve' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
        text=True,
    )


def start_server(env: dict[str, str]) -> subprocess.Popen:
    """启动一个真实 serve 进程。

    stdin 必须是**保持打开**的管道：MCP stdio 一旦读到 EOF 就会正常退出，
    用 DEVNULL 会让服务立刻自己结束，从而把"进程死了"误当成代码缺陷。
    """
    return subprocess.Popen(
        [PYTHON, "-X", "utf8", "-m", "mcp_agent_mailbox.cli", "serve"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    kill_all_servers({})
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "MAILBOX_HOME": str(WORK / "home"),
            "MAILBOX_HOST_TYPE": "dsh",
            "MAILBOX_HOST_INSTANCE_ID": "presence-check",
            "MAILBOX_SESSION_ID": SESSION_ID,
            "MAILBOX_CAPABILITY_LEVEL": "0",
        }
    )

    print("[1/6] migrate")
    print("    ", json.dumps(cli(env, "migrate"), ensure_ascii=False)[:150])

    print("\n[2/6] 启动真实 serve（注入会话身份，stdin 保持打开）")
    launcher = start_server(env)
    print(f"    Popen（launcher）PID = {launcher.pid}")
    time.sleep(6)

    print("\n[3/6] 进程活着时查账号")
    first = account_of(cli(env, "accounts"), SESSION_ID)
    if first is None:
        print("    [FAIL] 账号未自动注册")
        kill_all_servers(env)
        return 1
    host_pid = first["host_pid"]
    print(f"    account_id={first['account_id']}")
    print(f"    presence={first['presence']}  host_pid={host_pid}")
    alive = process_alive(host_pid) if host_pid else False
    print(f"    记录的 host_pid 对应进程是否存活: {alive}")
    step3 = first["presence"] == "connected" and alive
    print(f"    [{'OK' if step3 else 'FAIL'}] 进程活着 => connected，且 host_pid 指向一个真活着的进程")

    print(f"\n[4/6] 等待 {WAIT_SECONDS} 秒（远超 60 秒租约）后复查")
    time.sleep(WAIT_SECONDS)
    still = account_of(cli(env, "accounts"), SESSION_ID)
    assert still is not None
    step4 = still["presence"] == "connected"
    print(f"    presence={still['presence']}  host_pid={still['host_pid']}")
    print(f"    [{'OK' if step4 else 'FAIL'}] 进程仍活着 => 仍然在线（租约不参与判定）")

    print("\n[5/6] 杀掉**托管进程本身**（库里记录的 host_pid）后复查")
    kill_pid(host_pid)
    kill_all_servers(env)
    launcher.poll()
    # 在线只看进程，所以进程一死就应该立刻判离线：轮询等它翻转，并记录耗时。
    began = time.time()
    dead = None
    while time.time() - began < 60:
        dead = account_of(cli(env, "accounts"), SESSION_ID)
        if dead is not None and dead["presence"] == "offline":
            break
        time.sleep(2)
    assert dead is not None
    step5 = dead["presence"] == "offline"
    print(f"    presence={dead['presence']}  host_pid={dead['host_pid']}  "
          f"（翻转耗时 {time.time() - began:.1f}s）")
    print(f"    [{'OK' if step5 else 'FAIL'}] 进程退出 => offline")

    print("\n[6/6] 重启同一会话 ID：必须恢复同一账号，并跟随新进程")
    before = account_of(cli(env, "accounts"), SESSION_ID)
    restarted = start_server(env)
    time.sleep(6)
    after = account_of(cli(env, "accounts"), SESSION_ID)
    assert after is not None and before is not None
    step6 = (
        after["account_id"] == before["account_id"]
        and after["presence"] == "connected"
        and after["host_pid"] != before["host_pid"]
    )
    print(f"    账号 {before['account_id']} -> {after['account_id']}")
    print(f"    presence={after['presence']}  host_pid={after['host_pid']}")
    print(f"    [{'OK' if step6 else 'FAIL'}] 同一账号被恢复，在线状态跟随新进程")
    kill_all_servers(env)
    restarted.poll()

    print("\n===== 结论 =====")
    ok = step3 and step4 and step5 and step6
    print("全部通过：接入即注册；在线严格参照进程；进程退出即离线。" if ok else "存在失败项，见上。")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
