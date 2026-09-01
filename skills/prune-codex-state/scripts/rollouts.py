#!/usr/bin/env python3
"""Inventory rollout ownership and remove empty ancestors of approved rollout paths."""

import argparse
import errno
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

try:
    from compression import zstd
except ImportError:
    zstd = None

HEADER_LIMIT = 4 * 1024 * 1024
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT = re.compile(
    rf"rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-({UUID})(?:_({UUID}))?\.jsonl(?:\.zst)?",
    re.IGNORECASE | re.ASCII,
)
ID_FIELDS = ("id", "session_id", "parent_thread_id", "forked_from_id")


def first_line(path):
    if path.suffix != ".zst":
        with path.open("rb") as stream:
            return stream.readline(HEADER_LIMIT + 1)
    if zstd is not None:
        try:
            with zstd.open(path, "rb") as stream:
                return stream.readline(HEADER_LIMIT + 1)
        except zstd.ZstdError as exc:
            raise ValueError("invalid compressed rollout") from exc
    decoder = shutil.which("zstd")
    if decoder is None:
        raise ValueError("zstd decoder unavailable")
    # Stopping after the header avoids decompressing the rest of a large transcript.
    with subprocess.Popen([decoder, "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            return proc.stdout.readline(HEADER_LIMIT + 1)
        finally:
            if proc.poll() is None:
                proc.kill()


def is_id(value):
    return isinstance(value, str) and re.fullmatch(UUID, value, flags=re.IGNORECASE | re.ASCII)


def read_header(path, match):
    line = first_line(path)
    if len(line) > HEADER_LIMIT:
        raise ValueError("metadata record exceeds limit")
    row = json.loads(line)
    if not isinstance(row, dict) or row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
        raise ValueError("first record is not session metadata")
    meta = row["payload"]
    owner, physical_id = match.groups()
    if not is_id(meta.get("id")) or meta["id"].lower() != owner.lower():
        raise ValueError("filename and metadata disagree on thread ID")
    result = {"rollout_id": (physical_id or owner).lower()}
    for key in ID_FIELDS:
        value = meta.get(key)
        if value is not None:
            if not is_id(value):
                raise ValueError(f"invalid {key}")
            result[key] = value.lower()
    base = meta.get("history_base")
    if base is not None:
        if not isinstance(base, dict) or not is_id(base.get("thread_id")):
            raise ValueError("invalid history base rollout ID")
        result["history_base_rollout_id"] = base["thread_id"].lower()
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if isinstance(spawn, dict):
        parent = spawn.get("parent_thread_id")
        if not is_id(parent):
            raise ValueError("invalid spawn parent thread ID")
        result["spawn_parent_thread_id"] = parent.lower()
    if isinstance(meta.get("cwd"), str):
        result["cwd"] = meta["cwd"]
    return result


def inventory(home):
    report = {"rollouts": [], "unclassified": []}
    for store in ("sessions", "archived_sessions"):
        root = home / store
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError(f"unexpected rollout root: {root}")

        def walk_error(error):
            raise error

        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error) if root.exists() else ():
            for name in sorted(dirs):
                path = Path(directory) / name
                if path.is_symlink():
                    dirs.remove(name)
                    report["unclassified"].append({"path": str(path), "reason": "symlink directory"})
            dirs.sort()
            for name in sorted(names):
                path = Path(directory) / name
                st = path.lstat()
                match = ROLLOUT.fullmatch(name)
                try:
                    if not stat.S_ISREG(st.st_mode):
                        raise ValueError("non-regular file")
                    if match is None:
                        raise ValueError("unrecognized filename")
                    metadata = read_header(path, match)
                except (OSError, ValueError, EOFError) as exc:
                    # JSON parsing errors can contain document content. Keep diagnostics bounded and generic.
                    reason = str(exc) if type(exc) is ValueError else type(exc).__name__
                    report["unclassified"].append({"path": str(path), "reason": reason})
                else:
                    report["rollouts"].append(
                        {"path": str(path), "store": store, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, **metadata}
                    )
    return report


def prune_empty(home, removed_rollouts):
    root = home / "sessions"
    days = set()
    for path in removed_rollouts:
        if ".." in path.parts or not path.is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"expected absolute path within sessions: {path}")
        parts = path.relative_to(root).parts
        if (
            len(parts) != 4
            or not all(re.fullmatch(rf"[0-9]{{{width}}}", part) for part, width in zip(parts[:3], (4, 2, 2)))
            or not ROLLOUT.fullmatch(parts[3])
        ):
            raise ValueError(f"expected a rollout in sessions/YYYY/MM/DD: {path}")
        chain = (root, root / parts[0], root.joinpath(*parts[:2]), path.parent)
        if any(d.is_symlink() or (d.exists() and not d.is_dir()) for d in chain):
            raise ValueError(f"unsafe session directory: {path.parent}")
        if path.exists() or path.is_symlink():
            raise ValueError(f"rollout still exists: {path}")
        days.add(path.parent)

    removed = []
    for day in sorted(days):
        for directory in (day, day.parent, day.parent.parent):
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                break
            removed.append(str(directory))
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory").add_argument("--codex-home", type=Path, required=True)
    prune = commands.add_parser("prune-empty")
    prune.add_argument("--codex-home", type=Path, required=True)
    prune.add_argument("removed_rollout", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        home = args.codex_home.expanduser().resolve(strict=True)
        result = inventory(home) if args.command == "inventory" else prune_empty(home, args.removed_rollout)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
