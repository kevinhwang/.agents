"""Fingerprint and prune approved thread-owned rows in known Codex SQLite stores."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing

from rollouts import UUID

TABLES = {
    "state_5.sqlite": {"threads": "id"},
    "thread_history_1.sqlite": {
        "thread_turns": "thread_id",
        "thread_items": "thread_id",
        "thread_history_projection_state": "thread_id",
    },
    "logs_2.sqlite": {"logs": "thread_id"},
    "queue_1.sqlite": {"queued_thread_revisions": "thread_id"},
}
STATE_CASCADES = {"thread_dynamic_tools", "thread_attachments", "guardian_review_feedback"}
STATE_REFERENCES = (
    ("threads", "attached_thread_parent_id"),
    ("thread_spawn_edges", "parent_thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
    ("thread_read_receipts", "thread_id"),
    ("session_model_ids", "session_id"),
)
PROJECTION_CLEANUP_TRIGGER = (
    "create trigger thread_realtime_items_projection_cleanup after delete on thread_history_projection_state "
    "begin delete from thread_realtime_items where thread_id = old.thread_id; end"
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=lambda blob: {"blob": blob.hex()}).encode()
    ).hexdigest()


def _file_digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _is_sha256(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _validate_plans(databases, *, fingerprinted):
    if not isinstance(databases, list):
        raise TypeError("expected a list of database plans")
    seen = set()
    for db in databases:
        fields = {"name", "rows", "schema_sha256"} if fingerprinted else {"name", "rows"}
        if not isinstance(db, dict) or db.keys() != fields:
            raise ValueError("invalid database plan fields")
        name, rows = db["name"], db["rows"]
        if not isinstance(name, str) or name not in TABLES or name in seen:
            raise ValueError("unknown or duplicate database")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"expected row selectors for {name}")
        if fingerprinted and not _is_sha256(db["schema_sha256"]):
            raise ValueError(f"invalid schema fingerprint for {name}")
        seen.add(name)
        selectors = set()
        for row in rows:
            fields = {"table", "thread_id", "sha256", "count"} if fingerprinted else {"table", "thread_id"}
            if not isinstance(row, dict) or row.keys() != fields:
                raise ValueError(f"invalid row selector fields for {name}")
            table, thread_id = row["table"], row["thread_id"]
            if not isinstance(table, str) or table not in TABLES[name]:
                raise ValueError(f"unknown table for {name}")
            if not isinstance(thread_id, str) or not re.fullmatch(UUID, thread_id):
                raise ValueError(f"invalid thread ID for {name}/{table}")
            if (table, thread_id) in selectors:
                raise ValueError(f"duplicate row selector for {name}/{table}")
            if fingerprinted and (not _is_sha256(row["sha256"]) or type(row["count"]) is not int or row["count"] <= 0):
                raise ValueError(f"invalid row fingerprint for {name}/{table}")
            selectors.add((table, thread_id))


def validate(databases):
    _validate_plans(databases, fingerprinted=True)


def _regular_file(path, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"unsafe SQLite file: {path}")
    return True


def _open(path, *, mode="ro"):
    _regular_file(path)
    for suffix in ("-wal", "-shm", "-journal"):
        _regular_file(path.with_name(path.name + suffix), optional=True)
    database = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=1)
    try:
        database.execute("PRAGMA trusted_schema=OFF")
        database.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        database.close()
        raise
    return database


def _schema_fingerprint(database):
    return _digest(
        database.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name, type").fetchall()
    )


def _selector(database, table, column, *, collation="BINARY"):
    is_table = database.execute(
        "SELECT 1 FROM pragma_table_list WHERE schema='main' AND name=? AND type='table'", (table,)
    ).fetchone()
    has_column = database.execute("SELECT 1 FROM pragma_table_info(?) WHERE name=?", (table, column)).fetchone()
    if not is_table or not has_column:
        raise ValueError(f"missing table or column for {table}.{column}")
    return f'"{table}" WHERE "{column}" = ? COLLATE {collation}'


def _fingerprint(database, selector, thread_id):
    hashes = sorted(_digest(row) for row in database.execute(f"SELECT * FROM {selector}", (thread_id,)))
    return len(hashes), _digest(hashes)


def snapshot(home, database_selectors):
    """Return exact counts and content hashes without exposing the selected rows."""
    _validate_plans(database_selectors, fingerprinted=False)
    plans = []
    for plan in database_selectors:
        name = plan["name"]
        with closing(_open(home / name)) as database:
            database.execute("BEGIN")
            schema = _schema_fingerprint(database)
            rows = []
            for row in plan["rows"]:
                table = row["table"]
                selector = _selector(database, table, TABLES[name][table])
                count, sha256 = _fingerprint(database, selector, row["thread_id"])
                if count:
                    rows.append({**row, "sha256": sha256, "count": count})
            if rows:
                plans.append({"name": name, "schema_sha256": schema, "rows": rows})
    return plans


def _guard_relationships(database, name, rows):
    tables = {row["table"] for row in rows}
    references = set()
    for trigger, table, sql in database.execute("SELECT name, tbl_name, sql FROM sqlite_schema WHERE type='trigger'"):
        if table.lower() not in tables:
            continue
        header = rf"\ACREATE\s+TRIGGER\s+{re.escape(trigger)}\s+(?:AFTER|BEFORE)\s+(?:INSERT|UPDATE)\b"
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", trigger) and re.match(header, sql, re.IGNORECASE):
            continue
        if name == "thread_history_1.sqlite" and " ".join(sql.lower().split()) == PROJECTION_CLEANUP_TRIGGER:
            references.add((table, "thread_realtime_items", "thread_id"))
            continue
        raise ValueError(f"unexpected trigger on {name}/{table}")

    for table, _, _, parent, source, target, _, on_delete, _ in database.execute(
        "SELECT m.name, f.* FROM sqlite_schema m, pragma_foreign_key_list(m.name) f WHERE m.type='table'"
    ):
        if parent.lower() not in tables:
            continue
        if not (
            name == "state_5.sqlite"
            and table in STATE_CASCADES
            and source == "thread_id"
            and target == "id"
            and on_delete == "CASCADE"
        ):
            raise ValueError(f"unexpected incoming foreign key for {name}/{parent}")
        references.add((parent.lower(), table, source))

    if name == "state_5.sqlite":
        for table, column in STATE_REFERENCES:
            exists = database.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (table,)).fetchone()
            if exists or table in {"threads", "thread_spawn_edges"}:
                references.add(("threads", table, column))

    for parent, table, column in sorted(references):
        selector = _selector(database, table, column, collation="NOCASE")
        for row in rows:
            if row["table"] != parent:
                continue
            if database.execute(f"SELECT 1 FROM {selector} LIMIT 1", (row["thread_id"],)).fetchone():
                raise ValueError(f"related row in {name}/{table} for {row['thread_id']}")


def _verify_rows(database, plan, *, allow_missing=False):
    name = plan["name"]
    if _schema_fingerprint(database) != plan["schema_sha256"]:
        raise ValueError(f"SQLite schema changed for {name}")
    present = []
    for row in plan["rows"]:
        table, thread_id = row["table"], row["thread_id"]
        selector = _selector(database, table, TABLES[name][table])
        count, sha256 = _fingerprint(database, selector, thread_id)
        if count == row["count"] and sha256 == row["sha256"]:
            present.append((selector, thread_id, count))
        elif count or not allow_missing:
            raise ValueError(f"approved rows changed for {name}/{table}/{thread_id}")
    if present and len(present) != len(plan["rows"]):
        raise ValueError(f"approved rows are partly missing for {name}")
    return present


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_intent(path, intent):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = path.with_name(os.path.basename(file.name))
        try:
            json.dump(intent, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _backup(source_path, backup_path, plan):
    if _regular_file(backup_path, optional=True):
        with closing(_open(backup_path)) as backup:
            _verify_rows(backup, plan)
            if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(f"SQLite backup failed integrity check for {plan['name']}")
        return _file_digest(backup_path)

    fd, name = tempfile.mkstemp(dir=backup_path.parent)
    os.close(fd)
    temporary = backup_path.with_name(os.path.basename(name))
    try:
        # The apply connection holds a reserved lock so this second connection sees the verified, committed preimage.
        with closing(_open(source_path)) as source, closing(_open(temporary, mode="rw")) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            _verify_rows(destination, plan)
            if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(f"SQLite backup failed integrity check for {plan['name']}")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, backup_path)
        _sync_directory(backup_path.parent)
        return _file_digest(backup_path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(home, plan, run_dir):
    """Apply one approved database plan after shutdown and return rows deleted this invocation."""
    validate([plan])
    name = plan["name"]
    path = home / name
    backup_path = run_dir / f"{name}.backup"
    intent_path = run_dir / f"{name}.intent.json"
    plan_digest = _digest({**plan, "rows": sorted(plan["rows"], key=lambda row: (row["table"], row["thread_id"]))})
    has_intent = _regular_file(intent_path, optional=True)
    if has_intent:
        intent = json.loads(intent_path.read_text())
        if (
            not isinstance(intent, dict)
            or intent.keys() != {"plan_sha256", "backup_sha256", "applied"}
            or intent["plan_sha256"] != plan_digest
            or type(intent["applied"]) is not bool
        ):
            raise ValueError(f"SQLite recovery intent does not match {name}")
        _regular_file(backup_path)
        if _file_digest(backup_path) != intent["backup_sha256"]:
            raise ValueError(f"SQLite recovery backup changed for {name}")

    with closing(_open(path, mode="rw")) as database:
        database.execute("BEGIN IMMEDIATE")
        present = _verify_rows(database, plan, allow_missing=has_intent)
        if not present:
            if not intent["applied"]:
                _save_intent(intent_path, {**intent, "applied": True})
            return 0
        if has_intent and intent["applied"]:
            raise ValueError(f"approved rows reappeared for {name}")
        _guard_relationships(database, name, plan["rows"])
        if not has_intent:
            intent = {"plan_sha256": plan_digest, "backup_sha256": _backup(path, backup_path, plan), "applied": False}
            _save_intent(intent_path, intent)

        before = database.total_changes
        deleted = sum(count for _, _, count in present)
        for selector, thread_id, count in present:
            if database.execute(f"DELETE FROM {selector}", (thread_id,)).rowcount != count:
                raise ValueError(f"SQLite delete count changed for {name}/{thread_id}")
        if database.total_changes - before != deleted:
            raise ValueError(f"deletion would change unapproved SQLite rows for {name}")
        if _verify_rows(database, plan, allow_missing=True):
            raise ValueError(f"approved rows survived deletion for {name}")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError(f"foreign key check failed for {name}")
        database.commit()
    _save_intent(intent_path, {**intent, "applied": True})
    return deleted
