"""Read-only helper: extract selected files from app.asar into _evidence/ for inspection.

This does NOT modify the asar; it only reads it. Not part of the deliverable.
Mirrors tools/asar_peek.py header parsing.
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asar", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--match", action="append", required=True)
    parser.add_argument("--line-start", type=int, default=1)
    parser.add_argument("--line-end", type=int, default=10**9)
    parser.add_argument("--ranges", default="", help="extra ranges as lo-hi,lo-hi")
    args = parser.parse_args()

    spans = [(args.line_start, args.line_end)]
    for part in args.ranges.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        spans.append((int(lo), int(hi) if hi else int(lo)))

    archive = Path(args.asar)
    header, data_offset = load_header(archive)
    files = []
    walk(header, "", files)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    for needle in args.match:
        hits = [(n, e) for n, e in files if needle in n]
        if not hits:
            print(f"MISS {needle}")
            continue
        for name, entry in hits:
            size = int(entry.get("size") or 0)
            with archive.open("rb") as handle:
                handle.seek(data_offset + int(entry["offset"]))
                text = handle.read(size).decode("utf-8", errors="replace")
            lines = text.splitlines()
            chunks = []
            for lo, hi in spans:
                lo = max(1, lo)
                hi = min(len(lines), hi)
                if lo > hi:
                    continue
                chunks.append("\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1)))
            body = "\n...\n".join(chunks)
            target = outdir / (name.replace("/", "__"))
            target.write_text(body, encoding="utf-8")
            print(f"{name} -> {target} ({len(lines)} lines total)")


if __name__ == "__main__":
    main()
