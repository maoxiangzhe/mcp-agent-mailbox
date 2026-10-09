"""Read-only helper: extract a set of asar entries into a probe directory, preserving layout.

Reads app.asar only. Not part of the deliverable.
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
    parser.add_argument("--prefix", default="dsh/")
    parser.add_argument("--match", action="append", required=True)
    args = parser.parse_args()

    archive = Path(args.asar)
    header, data_offset = load_header(archive)
    files = []
    walk(header, "", files)

    outroot = Path(args.out)
    written = 0
    for name, entry in files:
        rel = name[len(args.prefix):] if name.startswith(args.prefix) else name
        if not any(m in rel for m in args.match):
            continue
        size = int(entry.get("size") or 0)
        with archive.open("rb") as handle:
            handle.seek(data_offset + int(entry["offset"]))
            blob = handle.read(size)
        target = outroot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        written += 1
    print(f"wrote {written} files under {outroot}")


if __name__ == "__main__":
    main()
