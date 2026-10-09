"""只读解析 Electron asar 归档里指定包的文件（不落盘）。

用途：确认 DSH 的 MCP 服务器配置是否支持 ``env``（决定邮箱能不能用正规方式接入）。

用法：
    .venv/Scripts/python.exe -X utf8 tools/asar_peek.py \
        --asar <path-to-app.asar> --match dsh-mcp-client --grep "env" --limit 40
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


def load_header(path: Path) -> tuple[dict, int]:
    with path.open("rb") as handle:
        head = handle.read(16)
        # asar: u32=4, u32=header_pickle_size, u32=header_string_size, u32=json_size
        _u1, pickle_size, _str_size, json_size = struct.unpack("<IIII", head[:16])
        payload = handle.read(pickle_size)[:json_size]
    return json.loads(payload.decode("utf-8")), 8 + pickle_size


def walk(node: dict, prefix: str, out: list[tuple[str, dict]]) -> None:
    for name, entry in (node.get("files") or {}).items():
        full = f"{prefix}/{name}" if prefix else name
        if "files" in entry:
            walk(entry, full, out)
        else:
            out.append((full, entry))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取 asar 内指定包的文件内容")
    parser.add_argument("--asar", required=True)
    parser.add_argument("--match", default="", help="路径必须包含这个子串")
    parser.add_argument("--grep", default="", help="只打印包含该子串的行")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--list", action="store_true", help="只列文件，不打印内容")
    args = parser.parse_args(argv)

    archive = Path(args.asar)
    header, data_offset = load_header(archive)
    files: list[tuple[str, dict]] = []
    walk(header, "", files)

    selected = [item for item in files if args.match in item[0]]
    print(f"asar 内文件总数 {len(files)}，匹配 '{args.match}' 的有 {len(selected)} 个")
    if args.list:
        for name, _entry in selected[: args.limit]:
            print("  " + name)
        return 0

    printed = 0
    with archive.open("rb") as handle:
        for name, entry in selected:
            if not name.endswith((".md", ".js", ".ts", ".json")):
                continue
            size = int(entry.get("size") or 0)
            if size == 0 or size > 400_000:
                continue
            handle.seek(data_offset + int(entry["offset"]))
            text = handle.read(size).decode("utf-8", errors="replace")
            hits = [
                (index, line)
                for index, line in enumerate(text.splitlines(), 1)
                if not args.grep or args.grep in line
            ]
            if not hits:
                continue
            print(f"\n=== {name} ===")
            for index, line in hits[: args.limit]:
                print(f"  {index}: {line.strip()[:220]}")
            printed += 1
            if printed >= 8:
                break
    if printed == 0:
        print("（没有命中）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
