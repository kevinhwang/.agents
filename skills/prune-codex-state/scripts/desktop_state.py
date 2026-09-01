"""Finish approved Desktop JSON edits after the Desktop writer has stopped."""

import hashlib
import json
import os
import re
import stat
import tempfile

from rollouts import is_id

STATE_FILES = (".codex-global-state.json", ".codex-global-state.json.bak")
DIRECT_MAPS = {"thread-project-assignments", "thread-project-membership-host-ids"}
OWNER_MAPS = ("thread-projectless-output-directories", "thread-writable-roots")
ATOM_MAPS = {"heartbeat-thread-permissions-by-id", "thread-descriptions-v1"}
ATOM_PREFIX = "electron-owner-atoms:"
PROJECTLESS_LIST = "projectless-thread-ids"


def value_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _composite_key(atom, key, thread_id):
    if atom not in {"thread-user-activity-times-v1", "recent-workspace-files-v3"}:
        return False
    try:
        components = json.loads(key)
    except ValueError:
        return False
    if atom == "thread-user-activity-times-v1":
        return components == ["local", thread_id]
    return (
        isinstance(components, list)
        and len(components) == 3
        and components[:2] == ["local", f"local:{thread_id}"]
        and isinstance(components[2], str)
        and bool(components[2])
    )


def _matches_thread(keys, thread_id):
    if not isinstance(keys, list) or len(keys) not in {2, 3} or not isinstance(keys[0], str):
        return False
    if keys[0] == PROJECTLESS_LIST:
        return len(keys) == 2 and type(keys[1]) is int and keys[1] >= 0
    if not all(isinstance(key, str) and key for key in keys):
        return False
    if len(keys) == 2:
        if keys[0].startswith(ATOM_PREFIX) and len(keys[0]) > len(ATOM_PREFIX):
            return keys[1] == f"thread-reference-capability:{thread_id}"
        owner_map = any(
            keys[0].startswith(prefix) and len(keys[0]) > len(prefix)
            for prefix in (f"electron-owner-value:{name}:" for name in OWNER_MAPS)
        )
        return keys[1] == thread_id and (keys[0] in DIRECT_MAPS or owner_map)
    return (
        keys[0].startswith(ATOM_PREFIX)
        and len(keys[0]) > len(ATOM_PREFIX)
        and ((keys[1] in ATOM_MAPS and keys[2] == thread_id) or _composite_key(keys[1], keys[2], thread_id))
    )


def validate_edits(edits):
    if not isinstance(edits, list):
        raise ValueError("Desktop edits must be a list")  # noqa: TRY004 - manifest validation
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"thread_id", "keys", "sha256"}:
            raise ValueError("Invalid Desktop edit fields")
        thread_id, keys, sha256 = edit["thread_id"], edit["keys"], edit["sha256"]
        if not is_id(thread_id) or thread_id != thread_id.lower() or not _matches_thread(keys, thread_id):
            raise ValueError("Desktop edit does not identify an approved thread in a supported key")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Invalid Desktop value digest")
        target = (PROJECTLESS_LIST, thread_id) if keys[0] == PROJECTLESS_LIST else tuple(keys)
        if target in seen:
            raise ValueError("Duplicate Desktop edit")
        seen.add(target)


def _patch(document, edit):
    thread_id, keys = edit["thread_id"], edit["keys"]
    container = document
    for key in keys[:-1]:
        if not isinstance(container, dict):
            raise ValueError("Desktop state key has changed container type")  # noqa: TRY004 - document schema drift
        if key not in container:
            return False
        container = container[key]
    key = keys[-1]
    if keys[0] == PROJECTLESS_LIST:
        if not isinstance(container, list):
            raise ValueError("Desktop thread list has changed container type")
        # Desktop may reorder this list while flushing state at shutdown.
        matches = [index for index, value in enumerate(container) if value == thread_id]
        if not matches:
            return False
        if len(matches) > 1:
            raise ValueError(f"Desktop thread list contains duplicate ID {thread_id}")
        key = matches[0]
    else:
        if not isinstance(container, dict):
            raise ValueError("Desktop state key has changed container type")
        if key not in container:
            return False
    if value_hash(container[key]) != edit["sha256"]:
        raise ValueError(f"Desktop state value changed for thread {thread_id}")
    del container[key]
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Desktop state contains duplicate object keys")
        result[key] = value
    return result


def _read_file(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Expected a regular file at {path}")
        return stream.read(), info


def _sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _stage(path, content, mode):
    with tempfile.NamedTemporaryFile(prefix=".prune-desktop-", dir=path.parent, delete=False) as output:
        staged = path.parent / os.path.basename(output.name)
        try:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        except BaseException:
            staged.unlink()
            raise
    return staged


def _backup(path, content, run_dir):
    digest = hashlib.sha256(content).hexdigest()
    label = "desktop-primary" if path.name == STATE_FILES[0] else "desktop-fallback"
    expected = run_dir / f"{label}.{digest}.original"
    found = list(run_dir.glob(f"{label}.*.original"))
    if found:
        if found != [expected]:
            raise ValueError(f"Desktop state changed since the existing backup of {path.name}")
    else:
        staged = _stage(expected, content, 0o600)
        try:
            os.link(staged, expected)
            _sync_directory(run_dir)
        finally:
            staged.unlink()
    snapshot, info = _read_file(expected)
    if snapshot != content or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"Desktop state backup verification failed for {path.name}")


def finish(home, edits, run_dir):
    validate_edits(edits)
    if not edits:
        return
    info = run_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Desktop backup directory must be private")
    changes = []
    for filename in STATE_FILES:
        path = home / filename
        try:
            original, info = _read_file(path)
        except FileNotFoundError:
            continue
        try:
            state = json.loads(original, object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid Desktop JSON in {filename}") from error
        if not isinstance(state, dict):
            raise ValueError(f"Expected a Desktop state object in {filename}")  # noqa: TRY004 - document schema drift
        changed = False
        for edit in edits:
            changed |= _patch(state, edit)
        if changed:
            replacement = (json.dumps(state, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("ascii")
            changes.append((path, original, info, replacement))

    for path, original, _, _ in changes:
        _backup(path, original, run_dir)
    for path, original, info, replacement in changes:
        staged = _stage(path, replacement, stat.S_IMODE(info.st_mode))
        try:
            current, current_info = _read_file(path)
            if current != original or not os.path.samestat(info, current_info):
                raise ValueError(f"Desktop state changed before replacing {path.name}")
            os.replace(staged, path)
            _sync_directory(home)
        finally:
            staged.unlink(missing_ok=True)
