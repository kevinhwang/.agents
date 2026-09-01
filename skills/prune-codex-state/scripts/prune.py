#!/usr/bin/env python3
"""Apply a reviewed Codex prune manifest on the local Unix host."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import desktop_state
import prune_databases
from rollouts import ROLLOUT, inventory, is_id, prune_empty

MANIFEST_VERSION = 1
PREFLIGHT_MAX_AGE_SECONDS = 300


def require(condition, message):
    if not condition:
        raise ValueError(message)


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys.split()), f"expected fields: {keys}")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha256(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def thread_id(value):
    return is_id(value) and value == value.lower()


def ids(values):
    require(isinstance(values, list) and all(thread_id(value) for value in values), "invalid thread IDs")
    require(len(set(values)) == len(values), "duplicate thread IDs")
    return set(values)


def canonical(path, root=None):
    path = Path(path)
    require(path.is_absolute() and path == path.resolve() and not path.is_symlink(), f"unsafe path: {path}")
    require(root is None or (path != root and path.is_relative_to(root)), f"path outside approved root: {path}")
    return path


def rollout_path(path, home, owner):
    path = canonical(path, home)
    parts = path.relative_to(home).parts
    match = ROLLOUT.fullmatch(path.name)
    require(match and match[1] == owner, f"rollout filename does not match owner: {path}")
    require(
        (len(parts) == 2 and parts[0] == "archived_sessions")
        or (
            len(parts) == 5
            and parts[0] == "sessions"
            and all(re.fullmatch(rf"[0-9]{{{width}}}", component) for component, width in zip(parts[1:4], (4, 2, 2)))
        ),
        f"unrecognized rollout path: {path}",
    )
    return path


def load_json(path):
    def unique(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), f"duplicate JSON keys in {path}")
        return result

    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def validate_manifest(plan):
    exact_keys(
        plan, "version host_id codex_home account_mode desktop_owner_sha256 rollouts workspaces index databases desktop"
    )
    require(
        type(plan["version"]) is int and plan["version"] == MANIFEST_VERSION and plan["host_id"] == "local",
        "unsupported manifest version or host",
    )
    require(plan["account_mode"] in {"active", "include-signed-out"}, "invalid account mode")
    require(sha256(plan["desktop_owner_sha256"]), "invalid Desktop owner fingerprint")
    home = canonical(plan["codex_home"])
    require(home.is_dir(), "Codex home does not exist")
    for key in ("rollouts", "workspaces", "index"):
        require(isinstance(plan[key], list), f"{key} must be a list")
    for entry in plan["rollouts"]:
        exact_keys(entry, "path thread_id sha256")
        require(thread_id(entry["thread_id"]) and sha256(entry["sha256"]), "invalid rollout approval")
        rollout_path(entry["path"], home, entry["thread_id"])
    for entry in plan["workspaces"]:
        exact_keys(entry, "path thread_ids directories")
        require(ids(entry["thread_ids"]), "workspace must have verified owners")
        root = canonical(entry["path"])
        require(
            isinstance(entry["directories"], list) and entry["directories"] and entry["path"] in entry["directories"],
            "workspace must list its root and approved empty directories",
        )
        require(len(set(entry["directories"])) == len(entry["directories"]), "duplicate workspace directories")
        for directory in entry["directories"]:
            if directory != entry["path"]:
                canonical(directory, root)
    for key in ("rollouts", "workspaces"):
        paths = [entry["path"] for entry in plan[key]]
        require(len(set(paths)) == len(paths), f"duplicate {key} paths")
    for entry in plan["index"]:
        exact_keys(entry, "thread_id sha256 count")
        require(thread_id(entry["thread_id"]) and sha256(entry["sha256"]), "invalid index approval")
        require(type(entry["count"]) is int and entry["count"] > 0, "invalid index line count")
    require(
        len({(entry["thread_id"], entry["sha256"]) for entry in plan["index"]}) == len(plan["index"]),
        "duplicate index selectors",
    )
    prune_databases.validate(plan["databases"])
    desktop_state.validate_edits(plan["desktop"])
    return home


def candidate_ids(plan):
    return (
        {entry["thread_id"] for section in ("rollouts", "index", "desktop") for entry in plan[section]}
        | {owner for entry in plan["workspaces"] for owner in entry["thread_ids"]}
        | {row["thread_id"] for database in plan["databases"] for row in database["rows"]}
    )


def eligible_ids(snapshot, plan, manifest_hash, *, live):
    exact_keys(
        snapshot,
        "manifest_sha256 host_id account_mode observed_at unarchived_ids archived_ids protected_ids eligible_ids",
    )
    require(snapshot["manifest_sha256"] == manifest_hash, "preflight refers to a different manifest")
    require(snapshot["host_id"] == plan["host_id"], "preflight host changed")
    require(snapshot["account_mode"] == plan["account_mode"], "preflight account scope changed")
    observed_at = datetime.fromisoformat(snapshot["observed_at"])
    require(observed_at.utcoffset() is not None, "preflight timestamp must include timezone")
    elapsed = (datetime.now(UTC) - observed_at).total_seconds()
    require(elapsed >= 0, "preflight timestamp is in the future")
    if live:
        require(elapsed <= PREFLIGHT_MAX_AGE_SECONDS, "refresh the MCP listings and preflight")
    protected = ids(snapshot["unarchived_ids"]) | ids(snapshot["archived_ids"]) | ids(snapshot["protected_ids"])
    eligible = ids(snapshot["eligible_ids"])
    require(eligible <= candidate_ids(plan), "preflight added unapproved candidates")
    require(not eligible & protected, "preflight includes a protected thread")
    return eligible


@contextlib.contextmanager
def lock(path):
    path = canonical(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), f"not a regular lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"writer is busy: {path}") from exc
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def writer_locks(home, eligible):
    root = canonical(home / "thread-writer-locks", home)
    root.mkdir(mode=0o700, exist_ok=True)
    with contextlib.ExitStack() as stack:
        # Exclusive coordination prevents rollout publication while the reference scan and removal run.
        stack.enter_context(lock(root / ".coordination.lock"))
        for owner in sorted(eligible):
            stack.enter_context(lock(root / f"{owner}.lock"))
        yield


def read_regular(path):
    path = canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), f"not a regular file: {path}")
        return stream.read()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, mode=0o600):
    path = canonical(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def store_original(path, data):
    if path.exists():
        require(read_regular(path) == data, f"backup does not match: {path}")
    else:
        atomic_write(path, data)


def file_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextlib.contextmanager
def read_runtime_database(path):
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-journal")]
    for sidecar in sidecars:
        canonical(sidecar, path.parent)
    # Inactive Desktop profiles may have a WAL-mode database with no sidecars. A plain read-only open can
    # try to create them. Verify that the immutable read saw a checkpointed file that stayed unchanged.
    quiescent = not any(sidecar.exists() for sidecar in sidecars)
    identity = file_identity(path) if quiescent else None
    uri = path.as_uri() + ("?mode=ro&immutable=1" if quiescent else "?mode=ro")
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
        conn.execute("BEGIN")
        yield conn
    if quiescent:
        require(
            not any(sidecar.exists() for sidecar in sidecars) and file_identity(path) == identity,
            f"runtime database changed during verification: {path}",
        )


def verify_runtime(home, eligible):
    checks = {
        "active_chat_turns": "thread_id",
        "automations": "target_thread_id",
        "automation_runs": "thread_id",
    }
    for name in ("codex.db", "codex-dev.db"):
        path = canonical(home / "sqlite" / name, home)
        if not path.exists():
            continue
        with read_runtime_database(path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            for table, column in checks.items():
                if table not in tables:
                    continue
                for (owner,) in conn.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"):
                    require(owner not in eligible, f"thread has a Desktop runtime reference: {owner}")
    state = canonical(home / "state_5.sqlite", home)
    if state.exists():
        with read_runtime_database(state) as conn:
            for query in (
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges",
                "SELECT id, attached_thread_parent_id FROM threads WHERE attached_thread_parent_id IS NOT NULL",
            ):
                for parent, child in conn.execute(query):
                    require(not eligible & {parent, child}, "approved thread has a current spawn or attachment link")


def verify_rollout_graph(home, approved, eligible):
    report = inventory(home)
    for entry in report["unclassified"]:
        path = Path(entry["path"])
        require(
            path.name == ".DS_Store" and stat.S_ISREG(path.lstat().st_mode),
            f"unclassified rollout artifact: {path}",
        )
    physical = {ROLLOUT.fullmatch(Path(entry["path"]).name)[2] or entry["thread_id"] for entry in approved}
    paths = {entry["path"] for entry in approved}
    for entry in report["rollouts"]:
        owner = entry["id"]
        if entry["path"] in paths:
            require(owner in eligible, f"rollout ownership changed: {entry['path']}")
            continue
        refs = {
            entry.get(key) for key in ("parent_thread_id", "session_id", "forked_from_id", "spawn_parent_thread_id")
        }
        require(
            owner not in eligible and not refs & eligible and entry.get("history_base_rollout_id") not in physical,
            f"unapproved rollout or reference for eligible thread: {entry['path']}",
        )


def remove_rollout(entry, home, run_dir):
    path = rollout_path(entry["path"], home, entry["thread_id"])
    label = f"rollout-{digest(os.fsencode(path))}"
    backup = run_dir / label
    done = run_dir / f"{label}.done"
    if path.exists():
        require(not done.exists(), f"removed rollout reappeared: {path}")
        identity = file_identity(path)
        data = read_regular(path)
        require(digest(data) == entry["sha256"], f"rollout changed: {path}")
        store_original(backup, data)
        require(
            file_identity(path) == identity and read_regular(path) == data, f"rollout changed during backup: {path}"
        )
        path.unlink()
        sync_directory(path.parent)
    else:
        require(backup.is_file(), f"approved rollout absent before backup: {path}")
        require(digest(read_regular(backup)) == entry["sha256"], f"rollout backup changed: {path}")
    store_original(done, entry["sha256"].encode())
    return path


def rewrite_index(home, approvals, run_dir):
    if not approvals:
        return
    path = canonical(home / "session_index.jsonl", home)
    expected = defaultdict(Counter)
    for entry in approvals:
        expected[entry["thread_id"]][entry["sha256"]] = entry["count"]
    with lock(home / "session_index.jsonl.lock"):
        original = read_regular(path)
        remaining = []
        current = defaultdict(Counter)
        for line in original.splitlines(keepends=True):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                row = None
            owner = row.get("id") if isinstance(row, dict) else None
            if isinstance(owner, str) and owner in expected:
                current[owner][digest(line)] += 1
            else:
                remaining.append(line)
        for owner, hashes in expected.items():
            intent = run_dir / f"index-{owner}.intent"
            if intent.exists():
                record = load_json(intent)
                require(
                    record["lines"] == hashes and sha256(record["backup"]), f"index recovery scope changed for {owner}"
                )
                backup = run_dir / f"session-index-{record['backup']}"
                require(digest(read_regular(backup)) == record["backup"], f"session index backup changed for {owner}")
                if not current[owner]:
                    continue
            require(current[owner] == hashes, f"session index rows changed for {owner}")
        if not any(current.values()):
            return
        backup_hash = digest(original)
        store_original(run_dir / f"session-index-{backup_hash}", original)
        for owner, hashes in expected.items():
            intent = run_dir / f"index-{owner}.intent"
            if not intent.exists():
                store_original(intent, json.dumps({"lines": hashes, "backup": backup_hash}, sort_keys=True).encode())
        atomic_write(path, b"".join(remaining), stat.S_IMODE(path.stat().st_mode))


def workspace_root(home):
    config = home / "config.toml"
    requirements = home / "requirements.toml"
    settings = tomllib.loads(read_regular(config).decode()) if config.exists() else {}
    managed = tomllib.loads(read_regular(requirements).decode()) if requirements.exists() else {}
    override = managed.get("application", {}).get("storage", {}).get("projectless_workspace_root")
    configured = settings.get("desktop", {}).get("projectlessWorkspaceRoot", "~/Documents/Codex")
    return canonical(Path(override or configured).expanduser())


def remove_workspace(entry, root, run_dir):
    path = canonical(entry["path"], root)
    allowed = {canonical(directory, root) for directory in entry["directories"]}
    for directory in allowed:
        require(
            directory == path or (directory.is_relative_to(path) and directory.parent in allowed),
            "invalid workspace tree",
        )
    intent = run_dir / f"workspace-{digest(os.fsencode(path))}.json"
    if intent.exists():
        original = load_json(intent)
        require(set(original) == {str(directory) for directory in allowed}, f"workspace recovery scope changed: {path}")
    else:
        original = {}
        for directory in allowed:
            require(directory.is_dir(), f"approved workspace directory missing: {directory}")
            info = directory.stat()
            original[str(directory)] = [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]
    for directory in allowed:
        if directory.exists():
            require(
                directory.is_dir() and set(directory.iterdir()) <= allowed, f"workspace is no longer empty: {directory}"
            )
            info = directory.stat()
            require([info.st_dev, info.st_ino] == original[str(directory)][:2], f"workspace was replaced: {directory}")
    if not intent.exists():
        atomic_write(intent, json.dumps(original, sort_keys=True).encode())
    for directory in sorted(allowed, key=lambda candidate: (-len(candidate.parts), candidate)):
        if directory.exists():
            directory.rmdir()
            sync_directory(directory.parent)


def apply_files(plan, home, eligible, run_dir):
    if not eligible:
        return {"rollouts": 0, "index_lines": 0, "workspaces": 0}
    approved_rollouts = [entry for entry in plan["rollouts"] if entry["thread_id"] in eligible]
    workspaces = [entry for entry in plan["workspaces"] if set(entry["thread_ids"]) <= eligible]
    index = [entry for entry in plan["index"] if entry["thread_id"] in eligible]
    workspace_location = workspace_root(home) if workspaces else None
    with writer_locks(home, eligible):
        verify_runtime(home, eligible)
        verify_rollout_graph(home, approved_rollouts, eligible)
        removed = [remove_rollout(entry, home, run_dir) for entry in approved_rollouts]
        rewrite_index(home, index, run_dir)
        for entry in workspaces:
            remove_workspace(entry, workspace_location, run_dir)
        prune_empty(home, [path for path in removed if path.is_relative_to(home / "sessions")])
    return {
        "rollouts": len(removed),
        "index_lines": sum(entry["count"] for entry in index),
        "workspaces": len(workspaces),
    }


def require_codex_closed():
    result = subprocess.run(["ps", "-axo", "comm="], check=True, capture_output=True, text=True)
    for command in result.stdout.splitlines():
        name = Path(command.strip()).name.casefold()
        require(not re.match(r"^codex(?:$|[ ._-])", name), "quit Codex and stop its app-server before finishing")


def prepare_run_directory(path, home, plan):
    path = canonical(path)
    excluded = [home, *(Path(entry["path"]) for entry in plan["workspaces"])]
    require(not any(path == root or path.is_relative_to(root) for root in excluded), "backups overlap candidate data")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), f"invalid run directory: {path}")
    require(stat.S_IMODE(info.st_mode) & 0o077 == 0, f"run directory must be private: {path}")
    return path


def apply_plan(args):
    manifest_path = canonical(args.manifest)
    manifest_data = read_regular(manifest_path)
    manifest_hash = digest(manifest_data)
    require(manifest_hash == args.approved_sha256, "manifest bytes differ from the approved digest")
    plan = load_json(manifest_path)
    require(read_regular(manifest_path) == manifest_data, "manifest changed while opening it")
    home = validate_manifest(plan)
    run_dir = prepare_run_directory(args.run_dir, home, plan)
    with lock(run_dir / ".lock"):
        store_original(run_dir / "approved-manifest.json", manifest_data)
        snapshot_path = run_dir / "preflight.json"
        live = args.operation == "apply"
        snapshot = load_json(canonical(args.preflight) if live else snapshot_path)
        eligible = eligible_ids(snapshot, plan, manifest_hash, live=live)
        if not live:
            require_codex_closed()
        owner = load_json(canonical(home / ".codex-global-state.json", home)).get("electron-renderer-content-owner")
        require(
            isinstance(owner, str) and desktop_state.value_hash(owner) == plan["desktop_owner_sha256"],
            "Desktop account owner changed since preflight",
        )
        databases = []
        for db in plan["databases"]:
            rows = [row for row in db["rows"] if row["thread_id"] in eligible]
            if rows:
                databases.append({**db, "rows": rows})
        desktop = [entry for entry in plan["desktop"] if entry["thread_id"] in eligible]
        if live:
            selectors = [
                {
                    "name": db["name"],
                    "rows": [{"table": row["table"], "thread_id": row["thread_id"]} for row in db["rows"]],
                }
                for db in databases
            ]
            require(prune_databases.snapshot(home, selectors) == databases, "approved SQLite fingerprints changed")
            atomic_write(snapshot_path, json.dumps(snapshot, sort_keys=True).encode())
        processed = apply_files(plan, home, eligible, run_dir)
        result = {
            "confirmed_absent": processed,
            "run_dir": str(run_dir),
            "ineligible_threads": len(candidate_ids(plan) - eligible),
        }
        if live:
            result["deferred"] = {
                "database_rows": sum(row["count"] for db in databases for row in db["rows"]),
                "desktop_edits": len(desktop),
            }
        else:
            require_codex_closed()
            # Thread rows are the ownership evidence for the other stores until those stores finish.
            ordered = sorted(databases, key=lambda db: db["name"] == "state_5.sqlite")
            result["database_rows_removed_this_run"] = sum(prune_databases.apply(home, db, run_dir) for db in ordered)
            require_codex_closed()
            if desktop:
                desktop_state.finish(home, desktop, run_dir)
            result["desktop_edits_checked"] = len(desktop)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    fingerprints = commands.add_parser("fingerprint-databases", help="read-only database manifest preparation")
    fingerprints.add_argument("--codex-home", required=True, type=Path)
    fingerprints.add_argument("--selectors", required=True, type=Path)
    for name in ("apply", "finish-desktop-state"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--approved-sha256", required=True)
        command.add_argument("--run-dir", required=True, type=Path)
        if name == "apply":
            command.add_argument("--preflight", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "fingerprint-databases":
            result = prune_databases.snapshot(canonical(args.codex_home), load_json(canonical(args.selectors)))
        else:
            result = apply_plan(args)
    except (OSError, TypeError, ValueError, EOFError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"prune: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
