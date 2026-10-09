"""装载前预检：确认这个插件目录**真的能被 DSH 的 Loader 接受**。

为什么需要它：安装（`plugin_manager` 的 `install_bundle`）要用户批准、且失败一次就要重来。
预检把"能被接受"这件事在本机先证明掉，避免拿安装当试错。

预检项（对应 DSH Loader 的实际解析规则）：

  P1 `package.json` 是合法 JSON，且 `dsh.bundle.patch` 指向一个存在的文件
     —— `bundleManifest()` 就是读这个字段（dsh-plugin-manager `lib/index.js:226-229`），
        没有它会被当成"普通依赖"而不是 profile layer（同文件 `:250-252` 的警告）。
  P2 `plugin.patch.yml` 的顶层是 patch 条目数组，每项是 mapping
     —— `parsePatchList()`（dsh-app-boot `lib/index.js:3559-3570`）。
  P3 `insert` 条目的 `id` / `name` / `config` 三件套齐全，且 `name` 以 `./` 开头
     —— 以 `./` 开头才会被锚定到 patch 文件旁（`anchorInsertedPluginNames`，
        dsh-app-boot `lib/index.js:3536-3541`）。
  P4 `name` 指向的文件真实存在，且是 ESM（含 `export`）
  P5 `config.command` 指向的可执行文件存在（否则每个会话的邮箱子进程都起不来）
  P6 清单里不残留 `node_modules/` 替身包
     —— 否则 Node 会从导入文件向上先命中替身，真机装载会 import 到假包。

用法：
    python preflight.py [插件目录，默认本文件所在目录]
退出码：0 = 全部通过；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["main"]


def _declared_inject(body: str) -> set[str]:
    """从入口源码里读 `export const inject = [...]` 声明的服务名。"""
    match = re.search(r"export\s+const\s+inject\s*=\s*\[(?P<items>[^\]]*)\]", body)
    if match is None:
        return set()
    return {
        item.strip().strip("'\"")
        for item in match.group("items").split(",")
        if item.strip()
    }

# 极小的 YAML 子集解析：只覆盖本 patch 用到的结构（顶层数组 + 缩进 mapping + 标量），
# 不引入 PyYAML 依赖（项目 pyproject.toml 里没有 yaml）。遇到无法识别的行就如实报错，
# 不猜。
_SCALAR = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<value>.*)$")
_ITEM = re.compile(r"^[ \t]*-[ \t]*(?P<rest>.*)$")


def _strip_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_patch(text: str) -> list[dict]:
    """把 patch 解析成嵌套 dict/list；只支持本项目用到的 Loader patch 方言。

    结构（靠**缩进**决定归属）：
        - insert:                 # 缩进 0：顶层条目，insert -> 列表
            - id: x               # 缩进 4：列表项（一个 mapping）
              name: './a.js'      # 缩进 6：属于该列表项
              config:             # 缩进 6：值为空，类型待定
                key: value        # 缩进 8 -> config 是 mapping
    """
    entries: list[dict] = []
    root: dict | None = None

    # 活动容器栈：每项 (容器自身的缩进, 容器)。栈顶是最近打开的容器。
    stack: list[tuple[int, object]] = []
    # "值为空"的键：(所属 mapping, 键名, 该键所在行的缩进)
    pending: tuple[dict, str, int] | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        token = line.strip()

        if indent == 0:
            item = _ITEM.match(token)
            if item is None:
                raise ValueError(f"第 {lineno} 行：顶层必须是数组（每项以 '-' 开头）：{raw!r}")
            head = _SCALAR.match(item.group("rest"))
            if head is None:
                raise ValueError(f"第 {lineno} 行：顶层项必须是 'key: ...' 形式：{raw!r}")
            root = {}
            entries.append(root)
            stack = [(0, root)]
            pending = None
            key, value = head.group("key"), head.group("value").strip()
            if value:
                root[key] = _strip_scalar(value)
            else:
                pending = (root, key, indent)
            continue

        if root is None:
            raise ValueError(f"第 {lineno} 行：顶层必须是数组：{raw!r}")

        item = _ITEM.match(token)
        # 落实上一个"空值键"：更深的下一行决定它是列表还是 mapping
        if pending is not None:
            owner, key, key_indent = pending
            if indent > key_indent:
                if item is not None:
                    owner[key] = []
                else:
                    child: dict = {}
                    owner[key] = child
                    # 关键：把新建的 mapping 压栈，后续更深的行才会归属到它
                    stack.append((key_indent, child))
            else:
                owner[key] = None
            pending = None

        # 退栈：只保留缩进严格更小的容器
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        owner_map = _nearest_mapping(stack)
        if owner_map is None:
            raise ValueError(f"第 {lineno} 行：找不到可归属的容器：{raw!r}")

        if item is not None:
            list_key = _last_list_key(owner_map)
            if list_key is None:
                raise ValueError(f"第 {lineno} 行：列表项没有可归属的列表父键：{raw!r}")
            head = _SCALAR.match(item.group("rest"))
            if head is None:
                owner_map[list_key].append(_strip_scalar(item.group("rest")))
                continue
            sub: dict = {}
            owner_map[list_key].append(sub)
            stack.append((indent, sub))
            key, value = head.group("key"), head.group("value").strip()
            if value:
                sub[key] = _strip_scalar(value)
            else:
                pending = (sub, key, indent)
            continue

        head = _SCALAR.match(token)
        if head is None:
            raise ValueError(f"第 {lineno} 行：无法解析的映射行：{raw!r}")
        key, value = head.group("key"), head.group("value").strip()
        if value:
            owner_map[key] = _strip_scalar(value)
        else:
            owner_map[key] = {}
            stack.append((indent, owner_map[key]))
            pending = (owner_map, key, indent)
    return entries


def _nearest_mapping(stack: list[tuple[int, object]]) -> dict | None:
    for _, container in reversed(stack):
        if isinstance(container, dict):
            return container
    return None


def _last_list_key(mapping: dict) -> str | None:
    """返回 mapping 中"值类型为 list"的键；本方言里每个 mapping 至多一个。"""
    found = None
    for key, value in mapping.items():
        if isinstance(value, list):
            found = key
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent
    results: list[tuple[str, bool, str]] = []

    def check(cid: str, ok: bool, note: str) -> None:
        results.append((cid, bool(ok), note))
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {note}")

    print(f"预检目录：{root}\n")

    # -- P1 package.json 与 dsh.bundle.patch --
    pkg_path = root / "package.json"
    manifest: dict = {}
    if not pkg_path.is_file():
        check("P1.package_json", False, f"缺少 {pkg_path}")
    else:
        try:
            manifest = json.loads(pkg_path.read_text(encoding="utf-8"))
            check("P1.package_json", True, "package.json 是合法 JSON")
        except Exception as exc:  # noqa: BLE001
            check("P1.package_json", False, f"JSON 解析失败：{exc}")
    patch_rel = (manifest.get("dsh") or {}).get("bundle", {}).get("patch")
    check(
        "P1.bundle_patch_declared",
        bool(patch_rel),
        f"dsh.bundle.patch = {patch_rel!r}（缺这个字段会被当普通依赖，不是 profile layer）",
    )

    patch_path = root / str(patch_rel) if patch_rel else None
    check(
        "P1.bundle_patch_exists",
        bool(patch_path and patch_path.is_file()),
        f"patch 文件存在：{patch_path}",
    )
    if not (patch_path and patch_path.is_file()):
        return _summary(results)

    # -- P2/P3 patch 结构 --
    text = patch_path.read_text(encoding="utf-8")
    try:
        entries = parse_patch(text)
        check("P2.array_of_mappings", bool(entries), f"解析出 {len(entries)} 个顶层条目")
    except ValueError as exc:
        check("P2.array_of_mappings", False, f"解析失败：{exc}")
        return _summary(results)

    inserts = [e for e in entries if "insert" in e]
    check("P3.has_insert", len(inserts) == 1, f"insert 条目数 = {len(inserts)}（期望 1）")
    rows = inserts[0]["insert"] if inserts and isinstance(inserts[0]["insert"], list) else []
    check("P3.insert_rows", bool(rows), f"insert 下有 {len(rows)} 行")
    if rows:
        row = rows[0]
        check("P3.row_is_mapping", isinstance(row, dict), f"插件行是映射：{type(row).__name__}")
        if isinstance(row, dict):
            check("P3.row_id", bool(row.get("id")), f"id = {row.get('id')!r}")
            name = str(row.get("name", ""))
            check("P3.row_name_relative", name.startswith("./"),
                  f"name = {name!r}（必须以 './' 开头才会锚定到 patch 文件旁）")
            check("P3.row_config", isinstance(row.get("config"), dict),
                  f"config 是映射：{type(row.get('config')).__name__}")

    # -- P4 入口文件存在且是 ESM --
    if rows and isinstance(rows[0], dict):
        entry = root / str(rows[0].get("name", ""))
        exists = entry.is_file()
        check("P4.entry_exists", exists, f"入口文件：{entry}")
        if exists:
            body = entry.read_text(encoding="utf-8", errors="replace")
            check("P4.entry_is_esm", "export " in body,
                  "入口含 ESM 导出（Loader 以 file URL 装载，需要 ESM）")
            for token in ("agent/created", "MAILBOX_SESSION_ID", "agent.ctx"):
                check(f"P4.entry_has[{token}]", token in body, f"入口含 {token!r}")

            # 真语法检查：只做文本匹配会漏掉模块级语法错误（例如 apply 非 async 却用
            # await），而那种错误会让整个模块装载失败、插件完全不起作用。
            node = shutil.which("node") or shutil.which("node.exe")
            if node is None:
                check("P4.node_syntax_check", True, "找不到 node，跳过真语法检查（仅文本匹配）")
            else:
                proc = subprocess.run(
                    [node, "--check", str(entry)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                check(
                    "P4.node_syntax_check",
                    proc.returncode == 0,
                    f"node --check {entry.name} 退出码 {proc.returncode}"
                    + (f"；{detail[-1][:160]}" if proc.returncode != 0 and detail else ""),
                )

            # inject 声明必须覆盖插件真正读取的服务：cordis 读未注入的服务名会**抛异常**
            # （`?.` 拦不住），而且发生在事件监听器里会让**会话创建失败**。
            used_services = {
                "loader": "ctx.get('loader')" in body
                or 'ctx.get("loader")' in body
                or "ctx.loader" in body,
                "agents": "ctx.get('agents')" in body
                or 'ctx.get("agents")' in body
                or "ctx.agents" in body
                or "ctx?.agents" in body,
            }
            declared = _declared_inject(body)
            for service, used in used_services.items():
                if used:
                    check(
                        f"P4.inject_declares[{service}]",
                        service in declared,
                        f"插件读取了 {service} 服务 -> inject 必须声明它（当前 inject={sorted(declared)}）",
                    )

    # -- P5 command 可执行文件存在 --
    config = rows[0].get("config") if rows and isinstance(rows[0], dict) else None
    if isinstance(config, dict):
        cmd = str(config.get("command", ""))
        check("P5.command_exists", bool(cmd) and Path(cmd).is_file(),
              f"command = {cmd!r}（必须指向真实存在的可执行文件）")

    # -- P6 不残留替身包 --
    stub = root / "node_modules"
    check("P6.no_stub_node_modules", not stub.exists(),
          "插件目录下没有 node_modules 替身包（否则真机装载会 import 到假包）")

    return _summary(results)


def _summary(results: list[tuple[str, bool, str]]) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 70)
    print(f"装载前预检：{passed} PASS / {failed} FAIL（共 {len(results)}）")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
