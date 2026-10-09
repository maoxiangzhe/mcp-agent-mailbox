"""真实端到端验收脚本（任务书要求的人工验收步骤，可重复执行）。

做四件事，全部基于**真实进程与真实 HTTP**：

1. 用真实数据种子建库（覆盖多账号、多连接、过期租约、五种投递状态、分页数据）；
2. 浏览前对全部业务表做内容快照；
3. 用 ``python -m mcp_agent_mailbox.cli dashboard`` 真启动一次子进程服务（真实端口），
   用 urllib 发真实 HTTP 请求走完全部 API 与静态资源，并验证鉴权、405、安全头；
4. 停止服务后再取一次快照，逐表比对，证明监控台没有写入任何业务状态。

用法：
    .venv/Scripts/python.exe -X utf8 tools/dashboard_smoke.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dashboard_support import seed, snapshot  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def request(url: str, *, token: str | None = None, method: str = "GET"):
    call = urllib.request.Request(url, method=method)
    if token:
        call.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(call, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dashboard-smoke-"))
    print(f"工作目录：{workdir}")

    print("\n[1/4] 建立种子数据")
    seeded = seed(workdir, message_count=40, extra_accounts=35)
    database = seeded.database_path
    before = snapshot(database)
    print(f"  数据库：{database}")
    print(f"  业务表快照：{len(before)} 项（含 __all__ 哈希 {before['__all__'][:16]}…）")

    port = free_port()
    print(f"\n[2/4] 启动监控台（真实子进程，端口 {port}）")
    env = dict(os.environ)
    env["MAILBOX_HOME"] = str(workdir / "mailbox")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    process = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "mcp_agent_mailbox.cli",
            "dashboard",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # 先读启动横幅：用户就是这样拿到带令牌的访问地址的。
        banner = _drain_banner(process)
        match = re.search(r"\?token=([A-Za-z0-9_\-]+)", banner)
        check("启动输出包含带令牌的本地访问地址", match is not None, banner[-200:])
        if match is None:
            return 1
        token = match.group(1)
        check("启动输出声明只读", "只读" in banner)
        check("启动输出声明仅本机回环", "回环" in banner or "127.0.0.1" in banner)
        check("启动输出不含任何写操作提示", "发送" not in banner and "重试" not in banner)

        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            try:
                status, _headers, _body = request(f"{base}/api/health")
                if status == 200:
                    ready = True
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.3)
        check("服务在真实端口上响应 /api/health", ready)

        print("\n[3/4] 真实 HTTP 浏览")
        status, _headers, _body = request(f"{base}/api/health")
        check("/api/health 无需令牌即可访问", status == 200)

        for path in ("/api/overview", "/api/accounts", "/api/conversations", "/api/deliveries", "/api/diagnostics"):
            status, _headers, body = request(f"{base}{path}", token=None)
            check(f"{path} 无令牌返回 401", status == 401, f"实际 {status}")

        for path in ("/api/overview", "/api/accounts", "/api/conversations", "/api/deliveries", "/api/diagnostics"):
            status, headers, body = request(f"{base}{path}", token=token)
            check(f"{path} 带令牌返回 200", status == 200, f"实际 {status}")
            check(f"{path} 带严格 CSP", "default-src 'none'" in headers.get("Content-Security-Policy", ""))
            check(f"{path} 带 nosniff", headers.get("X-Content-Type-Options") == "nosniff")

        status, _headers, body = request(f"{base}/api/overview", token=token)
        counts = json.loads(body)["data"]["counts"]
        check("总览给出真实账号总数", counts["accounts"] >= 40, str(counts))
        check("总览给出真实对话总数", counts["conversations"] == 3, str(counts))
        check("总览给出真实消息总数", counts["messages"] >= 40, str(counts))

        status, _headers, body = request(f"{base}/api/accounts?page_size=10", token=token)
        page = json.loads(body)
        check("账号分页返回 10 条", len(page["data"]) == 10)
        check("账号 total 是全局聚合（>当前页）", page["meta"]["total"] > 10,
              str(page["meta"]["total"]))
        check("账号分页给出游标", bool(page["meta"]["next_cursor"]))

        cursor = page["meta"]["next_cursor"]
        status, _headers, body = request(
            f"{base}/api/accounts?page_size=10&cursor={urllib.parse.quote(cursor)}", token=token
        )
        page2 = json.loads(body)
        check("第二页与第一页不重复",
              not ({r["account_id"] for r in page["data"]} &
                   {r["account_id"] for r in page2["data"]}))

        status, _headers, body = request(f"{base}/api/deliveries?state=dead_letter", token=token)
        check("dead_letter 过滤可用", json.loads(body)["meta"]["total"] >= 1)

        status, _headers, body = request(f"{base}/api/conversations/{seeded.conversations['ab']}/messages?page_size=5", token=token)
        messages = json.loads(body)
        check("消息分页返回 5 条", len(messages["data"]) == 5)
        check("消息带三个独立状态",
              all({"delivery", "visibility", "processing"} <= set(m) for m in messages["data"]))
        check("消息正文含恶意标签且原样返回",
              any("<script>" in (m["content"] or "") for m in messages["data"]))

        status, _headers, _body = request(f"{base}/api/overview", token=token, method="POST")
        check("POST 返回 405", status == 405, f"实际 {status}")
        status, _headers, _body = request(f"{base}/api/overview", token=token, method="DELETE")
        check("DELETE 返回 405", status == 405, f"实际 {status}")

        status, _headers, _body = request(f"{base}/api/accounts?cursor=bogus", token=token)
        check("非法游标返回 400", status == 400, f"实际 {status}")
        status, _headers, _body = request(f"{base}/api/accounts/acc_missing", token=token)
        check("不存在的账号返回 404", status == 404, f"实际 {status}")

        for path, expected in (("/", "text/html"), ("/app.css", "text/css"), ("/app.js", "application/javascript")):
            status, headers, body = request(f"{base}{path}")
            check(f"{path} 返回 {expected}", status == 200 and headers.get("Content-Type", "").startswith(expected))
            check(f"{path} 不含令牌", token not in body.decode("utf-8", "replace"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    try:
        request(f"{base}/api/health")
        check("停止后连接被拒绝", False, "端口仍可访问")
    except (urllib.error.URLError, ConnectionError):
        check("停止后连接被拒绝", True)

    print("\n[4/4] 只读验证：浏览前后业务表必须逐行一致")
    after = snapshot(database)
    if after == before:
        check("全部业务表内容与浏览前完全一致", True)
    else:
        differing = [key for key in before if before[key] != after.get(key)]
        check("全部业务表内容与浏览前完全一致", False, f"变化：{differing}")

    seeded.broker.stop()

    print("\n===== 验收汇总 =====")
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    return 0 if not FAILED else 1


def _drain_banner(process: subprocess.Popen, timeout: float = 15.0) -> str:
    """收集子进程启动横幅（读到"按 Ctrl+C 停止"即可）。"""
    lines: list[str] = []
    deadline = time.time() + timeout
    assert process.stdout is not None
    while time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        lines.append(line)
        if "Ctrl+C" in line:
            break
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
