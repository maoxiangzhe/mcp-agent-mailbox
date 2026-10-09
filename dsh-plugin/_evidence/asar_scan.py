"""Read-only asar scanner for evidence gathering. Not a deliverable.

Searches every text-ish file inside app.asar for a literal substring and prints
matching lines. Unlike asar_peek.py this does not silently skip long lines.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


def load_header(path: Path):
    with path.open("rb") as handle:
        head = handle.read(16)
        _u1, pickle_size, _str_size, json_size = struct.unpack("<IIII", head[:16])
        payload = handle.read(pickle_size)[:json_size]
    return json.loads(payload.decode("utf-8")), 8 + pickle_size


def walk(node, prefix, out):
    for name, entry in (node.get("files") or {}).items():
        full = f"{prefix}/{name}" if prefix else name
        if "files" in entry:
            walk(entry, full, out)
        else:
            out.append((full, entry))


EXT = (".js", ".mjs", ".cjs", ".ts", ".json", ".yml", ".yaml", ".md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asar", required=True)
    parser.add_argument("--needle", required=True)
    parser.add_argument("--path-filter", default="")
    parser.add_argument("--max-files", type=int, default=12)
    parser.add_argument("--max-hits", type=int, default=6)
    args = parser.parse_args()

    archive = Path(args.asar)
    header, data_offset = load_header(archive)
    files = []
    walk(header, "", files)

    printed = 0
    with archive.open("rb") as handle:
        for name, entry in files:
            if args.path_filter not in name:
                continue
            if not name.endswith(EXT):
                continue
            size = int(entry.get("size") or 0)
            if size == 0 or size > 3_000_000:
                continue
            handle.seek(data_offset + int(entry["offset"]))
            try:
                text = handle.read(size).decode("utf-8")
            except UnicodeDecodeError:
                continue
            hits = [(i, line) for i, line in enumerate(text.splitlines(), 1) if args.needle in line]
            if not hits:
                continue
            print(f"\n=== {name} ({len(hits)} hits) ===")
            for i, line in hits[: args.max_hits]:
                print(f"  {i}: {line.strip()[:300]}")
            printed += 1
            if printed >= args.max_files:
                break
    if printed == 0:
        print("(no hits)")


if __name__ == "__main__":
    main()
