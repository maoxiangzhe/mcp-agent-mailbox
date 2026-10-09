"""把 DSH profile 从"profile 根写死会话 ID"迁移到"每会话插件注入身份"。

做两件事（一次原子写入）：

  1. **装载**：往 profile 的 `cordis.patch.yml` 加一条 `cordis:include`，指向本插件目录的
     `plugin.patch.yml`（必须绝对路径：include 的 `config.path` 相对 **profile 目录**解析，
     见 dsh-app-boot `lib/index.js:128`）。
  2. **移除**：删掉原来那条 profile 级 `mcp-mailbox` insert（`name: '@deepseek-ai/dsh-mcp-client'`），
     它写死了 `MAILBOX_SESSION_ID`，会让整个 profile 的所有会话共用同一个邮箱账号——这正是
     新插件要取代的东西。

为什么必须两件事一起做：只要 profile 里还留着写死的 `MAILBOX_SESSION_ID`，邮箱 MCP 子进程
启动时就会先抢占那个静态账号（`connection_context.py:163-165` 优先读该变量 + `mcp/server.py:236`
启动即绑定），真会话随后调用 `connect_mailbox` 会被拒。已有隔离环境实测复现。

安全保证：
  * 默认 dry-run，只有显式 `--apply` 才写盘；
  * 写盘前备份到 `<profile>/cordis.patch.yml.bak-<时间戳>`；
  * 只动"目标那一块"，文件里其它条目（GUI 设置等）与注释**逐字节保留**；
  * 写入前跑合成校验：无重复 id、无残留 `MAILBOX_SESSION_ID`、include 形态正确；
  * 幂等：已经是目标状态时不做任何修改。

用法：
    python migrate_profile.py                      # 预演（打印 diff，不写盘）
    python migrate_profile.py --apply              # 真正写入（先备份）
    python migrate_profile.py --profile <目录>      # 指定其它 profile
    python migrate_profile.py --check              # 只做校验，输出退出码
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from preflight import parse_patch  # noqa: E402  —— 复用同一套 patch 解析器

__all__ = ["build_migration", "main"]

DEFAULT_PROFILE = Path(r"C:\Users\mxz\.dsh\profiles\desktop")
PLUGIN_PATCH = Path(__file__).resolve().parent / "plugin.patch.yml"
INCLUDE_ID = "dsh-mailbox-per-session-include"
DIRECT_ROW_ID = "mailbox-per-session"
LEGACY_ROW_ID = "mcp-mailbox"
LEGACY_PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client"

_INSERT_RE = re.compile(r"^(?P<indent> *)- insert:")
_ROW_RE = re.compile(r"^(?P<indent> *)- id: *(?P<id>[^\s#]+)")
_ID_RE = re.compile(r"^ *- id: *(?P<id>[^\s#]+)")
_TOP_ID_RE = re.compile(r"^- id: *(?P<id>[^\s#]+)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_rows(entries: list) -> list[dict]:
    """深度优先收集所有"行"（带 id 的 mapping）。"""
    rows: list[dict] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                rows.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(entries)
    return rows


def _has_include(text: str) -> bool:
    return INCLUDE_ID in text


def _strip_legacy_block(text: str) -> tuple[str, bool]:
    """删掉包含 ``id: mcp-mailbox`` 的那条 ``- insert:`` 块；返回 (新文本, 是否删除过)。"""
    lines = text.splitlines(keepends=True)
    for start, raw in enumerate(lines):
        if not _INSERT_RE.match(raw.rstrip("\n")):
            continue
        base_indent = len(raw) - len(raw.lstrip(" "))
        # 块的结束：下一个缩进 <= base_indent 的非空非注释行
        end = len(lines)
        for index in range(start + 1, len(lines)):
            probe = lines[index].rstrip("\n")
            if not probe.strip() or probe.lstrip().startswith("#"):
                continue
            if (len(probe) - len(probe.lstrip(" "))) <= base_indent:
                end = index
                break
        block = "".join(lines[start:end])
        if LEGACY_ROW_ID not in block:
            continue
        if LEGACY_PLUGIN_NAME not in block:
            raise SystemExit(
                f"安全中止：块 {start + 1}-{end} 含 id={LEGACY_ROW_ID} 但插件名不是 "
                f"{LEGACY_PLUGIN_NAME}；不确定是否该删，请人工确认。"
            )
        # 连同块前的注释一起删：注释里通常写着"必须保留"之类的旧说明
        lead = start
        while lead > 0 and lines[lead - 1].lstrip().startswith("#"):
            lead -= 1
        while lead > 0 and not lines[lead - 1].strip():
            lead -= 1
        return "".join(lines[:lead] + lines[end:]), True
    return text, False


def _include_snippet() -> str:
    return (
        "# 每会话邮箱插件：把邮箱 MCP 挂到每个 Agent 的 agent.ctx，并注入\n"
        "# MAILBOX_SESSION_ID = agent.id（身份由宿主给出，模型无法指定）。\n"
        "# 这样才是『一个会话一个邮箱账号』；profile 根写死会话 ID 会让所有会话共用账号。\n"
        "#\n"
        "# config.path 必须写**绝对路径**：include 的路径相对 profile 目录解析\n"
        "# （dsh-app-boot lib/index.js:128）；而该 patch 内部以 './' 开头的 name 会锚定到\n"
        "# patch 文件旁（同文件 :3540），所以插件文件留在工作区即可。\n"
        "- insert:\n"
        f"    - id: {INCLUDE_ID}\n"
        "      name: cordis:include\n"
        "      config:\n"
        f"          path: {PLUGIN_PATCH}\n"
    )


def _direct_snippet() -> str:
    """直接把插件行写进 profile patch（不用 include）。

    为什么需要这条路：实测 `cordis:include` 在**全新启动**时没有被处理——
    重启后模块顶层探针 `module.evaluated` 一条都没有，说明插件文件根本没被装载
    （重启前看到的 `plugin.loaded` 是文件监听热重载造成的假象）。

    而 profile patch 里的行是确定会被加载的（原来那行 `@deepseek-ai/dsh-mcp-client`
    一直正常工作）。`name` 用**绝对路径**：Loader 会把绝对路径或 `./` 相对路径转成
    file URL（`dsh-app-boot/lib/index.js:3540`），绝对路径跨盘符也可用。
    """
    entry = str(Path(__file__).resolve().parent / "index.js")
    return (
        "# 每会话邮箱插件：把邮箱 MCP 挂到每个 Agent 的 agent.ctx，并注入\n"
        "# MAILBOX_SESSION_ID = agent.id（身份由宿主给出，模型无法指定）。\n"
        "# 这样才是『一个会话一个邮箱账号』；profile 根写死会话 ID 会让所有会话共用账号。\n"
        "#\n"
        "# 直接挂插件行（不用 cordis:include：实测全新启动时 include 未被处理）。\n"
        "- insert:\n"
        "    - id: mailbox-per-session\n"
        f"      name: {entry}\n"
        "      config:\n"
        "          command: 'E:\\zcz\\modle\\MCP\\mcp-agent-mailbox\\.venv\\Scripts\\python.exe'\n"
        "          args: [ '-m', 'mcp_agent_mailbox.cli', 'serve' ]\n"
        "          cwd: 'E:\\zcz\\modle\\MCP\\mcp-agent-mailbox'\n"
        "          serverName: mailbox\n"
        "          hostInstanceId: default\n"
        "          mailboxHome: 'C:\\Users\\mxz\\.board-mcp'\n"
        "          toolCallTimeoutMs: 60000\n"
        "          failOnStartupError: false\n"
    )


def build_migration(text: str, *, mode: str = "include") -> tuple[str, list[str]]:
    """返回 (迁移后文本, 变更说明)。已是目标状态时文本不变、说明为空。

    ``mode="include"`` 用 `cordis:include`；``mode="direct"`` 直接在 profile 里挂插件行。
    """
    notes: list[str] = []
    out = text

    # 顺序很重要：必须先删掉旧的 profile 级 insert，再插入新的。
    # 反过来的话，文本里会出现两个 `- insert:` 块，删除逻辑只会看到新的那个。
    out, removed = _strip_legacy_block(out)
    if removed:
        notes.append(
            f"移除：删除 profile 级 insert（id={LEGACY_ROW_ID}），其中写死的 "
            "MAILBOX_SESSION_ID 是全 profile 共用同一个账号的根因"
        )

    if mode == "direct":
        # 先把旧的 include 行去掉，再插入插件行
        out, dropped_include = _strip_include_block(out)
        if dropped_include:
            notes.append(f"移除：删除 include 条目 {INCLUDE_ID}（全新启动时它未被处理）")
        if "id: mailbox-per-session" not in out:
            out = _direct_snippet() + "\n" + out
            notes.append(
                "装载：在 profile patch 里直接插入插件行 id=mailbox-per-session"
                f"（name={Path(__file__).resolve().parent / 'index.js'}）"
            )
        return out, notes

    if not _has_include(out):
        out = _include_snippet() + "\n" + out
        notes.append(f"装载：新增 include 条目 {INCLUDE_ID} -> {PLUGIN_PATCH}")
    return out, notes


def _strip_include_block(text: str) -> tuple[str, bool]:
    """删掉包含 include id 的那条 ``- insert:`` 块。"""
    lines = text.splitlines(keepends=True)
    for start, raw in enumerate(lines):
        if not _INSERT_RE.match(raw.rstrip("\n")):
            continue
        base_indent = len(raw) - len(raw.lstrip(" "))
        end = len(lines)
        for index in range(start + 1, len(lines)):
            probe = lines[index].rstrip("\n")
            if not probe.strip() or probe.lstrip().startswith("#"):
                continue
            if (len(probe) - len(probe.lstrip(" "))) <= base_indent:
                end = index
                break
        block = "".join(lines[start:end])
        if INCLUDE_ID not in block:
            continue
        lead = start
        while lead > 0 and lines[lead - 1].lstrip().startswith("#"):
            lead -= 1
        while lead > 0 and not lines[lead - 1].strip():
            lead -= 1
        return "".join(lines[:lead] + lines[end:]), True
    return text, False


def validate_composition(text: str) -> list[str]:
    """合成校验：返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    try:
        entries = parse_patch(text)
    except ValueError as exc:
        return [f"patch 解析失败：{exc}"]

    # 收集所有 id（insert 行 + 顶层定向补丁行），检测重复
    ids: list[str] = []
    includes: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                ids.append(node["id"])
            if node.get("name") == "cordis:include":
                includes.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(entries)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(
            f"重复 id：{dupes}（DSH 的 applyEntryPatches 对重复 id 是『静默后者胜出』、"
            "无任何 warning，会让补丁静默失效）"
        )
    if LEGACY_ROW_ID in ids:
        problems.append(f"仍存在旧条目 id={LEGACY_ROW_ID}（未成功移除）")

    # 两种装载形态都接受，但必须**至少有一种**在，且各自形态正确
    if DIRECT_ROW_ID in ids:
        direct = [row for row in _iter_rows(entries) if row.get("id") == DIRECT_ROW_ID]
        for row in direct:
            name = str(row.get("name", ""))
            if not (Path(name).is_absolute() or name.startswith(("./", "../"))):
                problems.append(
                    f"插件行的 name 既不是绝对路径也不以 ./ 开头：{name!r}"
                    "（Loader 只对这两类做 file URL 转换）"
                )
            elif not Path(name).is_file():
                problems.append(f"插件行的 name 指向的文件不存在：{name}")
            config = row.get("config")
            if not isinstance(config, dict):
                problems.append(f"插件行 {DIRECT_ROW_ID} 缺少 config 映射")
            elif not str(config.get("command", "")).strip():
                problems.append(f"插件行 {DIRECT_ROW_ID} 的 config.command 为空")
    elif includes:
        for include in includes:
            config = include.get("config")
            path = config.get("path") if isinstance(config, dict) else None
            if not path:
                problems.append("include 条目缺少 config.path")
                continue
            if not Path(str(path)).is_absolute():
                problems.append(f"include 的 config.path 不是绝对路径：{path}（换机器/换目录必断）")
            elif not Path(str(path)).is_file():
                problems.append(f"include 指向的文件不存在：{path}")
    else:
        problems.append(
            f"既没有插件行 id={DIRECT_ROW_ID}，也没有 cordis:include 条目（没有任何装载形态）"
        )

    # 只查"赋值"形态：注释里提到这个变量名是正常的（迁移说明本身就要提它）。
    stray = [
        (index, line)
        for index, line in enumerate(text.splitlines(), 1)
        if re.match(r"^\s*MAILBOX_SESSION_ID\s*:", line)
    ]
    if stray:
        problems.append(
            "仍存在写死的会话身份赋值（必须清干净）："
            + "; ".join(f"第 {index} 行 {line.strip()}" for index, line in stray)
        )
    return problems

def _diff(before: str, after: str) -> str:
    import difflib

    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="cordis.patch.yml (before)",
            tofile="cordis.patch.yml (after)",
            lineterm="",
            n=2,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 DSH profile 迁移到每会话邮箱插件")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="profile 目录")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    parser.add_argument("--check", action="store_true", help="只校验当前文件，不迁移")
    parser.add_argument(
        "--mode",
        choices=["include", "direct"],
        default="direct",
        help="装载方式：direct=直接在 profile 里挂插件行（默认，实测可靠）；include=用 cordis:include",
    )
    args = parser.parse_args(argv)

    target = Path(args.profile) / "cordis.patch.yml"
    if not target.is_file():
        print(f"找不到 profile patch：{target}")
        return 2

    before = _read(target)

    if args.check:
        problems = validate_composition(before)
        for item in problems:
            print(f"  - {item}")
        print("校验结果：" + ("通过" if not problems else f"{len(problems)} 个问题"))
        return 0 if not problems else 1

    print(f"profile patch : {target}")
    print(f"插件 patch    : {PLUGIN_PATCH}（存在={PLUGIN_PATCH.is_file()}）")
    if not PLUGIN_PATCH.is_file():
        print("插件 patch 不存在，先跑 preflight.py 确认产物完整。")
        return 2

    after, notes = build_migration(before, mode=args.mode)

    if not notes:
        print("\n已经是目标状态，无需修改（幂等）。")
        return 0

    print("\n将要做的变更：")
    for note in notes:
        print(f"  * {note}")

    problems = validate_composition(after)
    print("\n合成校验：")
    if problems:
        for item in problems:
            print(f"  [FAIL] {item}")
        print("校验未通过，拒绝写入。")
        return 1
    print("  [PASS] 无重复 id、无残留静态会话 ID、装载形态与路径正确")

    print("\n--- diff ---")
    print(_diff(before, after))
    print("--- diff 结束 ---")

    if not args.apply:
        print("\n（dry-run：未写入。加 --apply 才会落盘，且会先备份。）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, backup)
    print(f"\n已备份：{backup}")

    tmp = target.with_name(f"{target.name}.new-{stamp}")
    tmp.write_text(after, encoding="utf-8")
    tmp.replace(target)
    print(f"已写入：{target}")

    final = _read(target)
    problems = validate_composition(final)
    print("写入后复校：" + ("通过" if not problems else f"{len(problems)} 个问题"))
    if problems:
        print("写入后校验失败，可用备份回滚：")
        print(f'  Copy-Item "{backup}" "{target}" -Force')
        return 1

    print("\n下一步：重载/重启 DSH 使 patch 生效。")
    print("  - 重启后每个会话会有自己的邮箱账号（MAILBOX_SESSION_ID = 该会话的 agent.id）")
    print(f"  - 回滚：Copy-Item \"{backup}\" \"{target}\" -Force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
