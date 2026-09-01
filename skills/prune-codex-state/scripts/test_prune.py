import hashlib
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import desktop_state
import prune
import prune_databases

ORPHAN = "00000000-0000-4000-8000-000000000001"
RETAINED = "00000000-0000-4000-8000-000000000002"
OWNER = "fixture-desktop-owner"


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def index_line(owner):
    return (json.dumps({"id": owner, "thread_name": "Fixture"}) + "\n").encode()


class ApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "codex"
        self.home.mkdir()
        self.run_dir = self.root / "run"
        self.index = self.home / "session_index.jsonl"
        self.index.write_bytes(index_line(ORPHAN) + index_line(RETAINED))
        self.rollout = self.make_rollout(ORPHAN)
        self.workspace = self.root / "workspaces" / "project"
        self.workspace.mkdir(parents=True)
        (self.workspace / "empty-output").mkdir()
        (self.home / "config.toml").write_text(
            f"[desktop]\nprojectlessWorkspaceRoot = {json.dumps(str(self.workspace.parent))}\n", encoding="utf-8"
        )
        self.state = self.home / ".codex-global-state.json"
        self.state_value = {
            "electron-renderer-content-owner": OWNER,
            "projectless-thread-ids": [ORPHAN, RETAINED],
        }
        write_json(self.state, self.state_value)
        self.database = self.home / "queue_1.sqlite"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE queued_thread_revisions(revision INTEGER PRIMARY KEY, thread_id TEXT NOT NULL)")
            conn.executemany(
                "INSERT INTO queued_thread_revisions VALUES (?, ?)",
                [(1, ORPHAN), (2, RETAINED)],
            )
        databases = prune_databases.snapshot(
            self.home,
            [{"name": self.database.name, "rows": [{"table": "queued_thread_revisions", "thread_id": ORPHAN}]}],
        )
        self.manifest = self.root / "approved.json"
        write_json(
            self.manifest,
            {
                "version": 1,
                "host_id": "local",
                "codex_home": str(self.home),
                "account_mode": "include-signed-out",
                "desktop_owner_sha256": desktop_state.value_hash(OWNER),
                "rollouts": [
                    {"path": str(self.rollout), "thread_id": ORPHAN, "sha256": self.sha(self.rollout.read_bytes())}
                ],
                "workspaces": [
                    {
                        "path": str(self.workspace),
                        "thread_ids": [ORPHAN],
                        "directories": [str(self.workspace), str(self.workspace / "empty-output")],
                    }
                ],
                "index": [{"thread_id": ORPHAN, "sha256": self.sha(index_line(ORPHAN)), "count": 1}],
                "databases": databases,
                "desktop": [
                    {
                        "thread_id": ORPHAN,
                        "keys": ["projectless-thread-ids", 0],
                        "sha256": desktop_state.value_hash(ORPHAN),
                    }
                ],
            },
        )
        self.preflight = self.root / "preflight.json"
        self.snapshot = {
            "manifest_sha256": self.sha(self.manifest.read_bytes()),
            "observed_at": datetime.now(UTC).isoformat(),
            "host_id": "local",
            "account_mode": "include-signed-out",
            "archived_ids": [RETAINED],
            "unarchived_ids": [],
            "protected_ids": [],
            "eligible_ids": [ORPHAN],
        }

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()

    def make_rollout(self, owner, **references):
        path = self.home / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T00-00-00-{owner}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"type": "session_meta", "payload": {"id": owner, **references}})
        return path

    def run_prune(self, operation="apply"):
        write_json(self.preflight, self.snapshot)
        return prune.apply_plan(
            Namespace(
                operation=operation,
                manifest=self.manifest,
                approved_sha256=self.snapshot["manifest_sha256"],
                preflight=self.preflight,
                run_dir=self.run_dir,
            )
        )

    def queue_owners(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return [row[0] for row in conn.execute("SELECT thread_id FROM queued_thread_revisions ORDER BY revision")]

    def test_live_failure_can_resume_and_finish_against_latest_desktop_json(self):
        original_write = prune.atomic_write

        def interrupt_index(path, data, *args):
            if path == self.index:
                raise OSError("interrupted index replacement")
            return original_write(path, data, *args)

        with (
            patch.object(prune, "atomic_write", side_effect=interrupt_index),
            self.assertRaisesRegex(OSError, "interrupted index replacement"),
        ):
            self.run_prune()

        self.assertFalse(self.rollout.exists())
        self.assertIn(ORPHAN, self.index.read_text())
        extra_line = b'{"id":"unrelated-new-write"}\n'
        with self.index.open("ab") as stream:
            stream.write(extra_line)

        result = self.run_prune()

        self.assertEqual(self.index.read_bytes(), index_line(RETAINED) + extra_line)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(result["deferred"], {"database_rows": 1, "desktop_edits": 1})
        self.assertEqual(self.queue_owners(), [ORPHAN, RETAINED])
        self.assertEqual(json.loads(self.state.read_text()), self.state_value)
        self.state_value["last-minute-setting"] = True
        self.state_value["projectless-thread-ids"].reverse()
        write_json(self.state, self.state_value)

        with patch.object(prune, "require_codex_closed"):
            finished = self.run_prune("finish-desktop-state")
            resumed = self.run_prune("finish-desktop-state")

        self.assertEqual(finished["database_rows_removed_this_run"], 1)
        self.assertEqual(resumed["database_rows_removed_this_run"], 0)
        self.assertEqual(self.queue_owners(), [RETAINED])
        self.assertEqual(json.loads(self.state.read_text()), {**self.state_value, "projectless-thread-ids": [RETAINED]})

    def test_refuses_changed_or_stale_scope(self):
        cases = [
            ({"unarchived_ids": [ORPHAN]}, "protected thread"),
            ({"eligible_ids": [RETAINED]}, "unapproved candidates"),
            ({"observed_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat()}, "refresh the MCP"),
        ]
        for change, message in cases:
            with (
                self.subTest(change=change),
                patch.dict(self.snapshot, change),
                self.assertRaisesRegex(ValueError, message),
            ):
                self.run_prune()
        self.assertTrue(self.rollout.exists())
        write_json(self.state, {**self.state_value, "electron-renderer-content-owner": "other owner"})
        with self.assertRaisesRegex(ValueError, "owner changed"):
            self.run_prune()
        self.assertTrue(self.rollout.exists())

    def test_live_writer_or_new_rollout_dependency_prevents_unlink(self):
        lock_dir = self.home / "thread-writer-locks"
        lock_dir.mkdir()
        with prune.lock(lock_dir / f"{ORPHAN}.lock"), self.assertRaisesRegex(ValueError, "writer is busy"):
            self.run_prune()
        self.assertTrue(self.rollout.exists())

        self.make_rollout(RETAINED, history_base={"thread_id": ORPHAN})
        with self.assertRaisesRegex(ValueError, "unapproved rollout or reference"):
            self.run_prune()
        self.assertTrue(self.rollout.exists())

    def test_live_apply_refuses_database_drift_before_removing_files(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO queued_thread_revisions VALUES (?, ?)", (3, ORPHAN))

        with self.assertRaisesRegex(ValueError, "SQLite fingerprints changed"):
            self.run_prune()

        self.assertTrue(self.rollout.exists())
        self.assertFalse((self.run_dir / "preflight.json").exists())

    def test_checks_inactive_desktop_db_without_creating_wal_files(self):
        desktop = self.home / "sqlite" / "codex-dev.db"
        desktop.parent.mkdir()
        with closing(sqlite3.connect(desktop)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE active_chat_turns(thread_id TEXT)")
            conn.execute("INSERT INTO active_chat_turns VALUES (?)", (ORPHAN,))

        with self.assertRaisesRegex(ValueError, "Desktop runtime reference"):
            self.run_prune()

        self.assertEqual(list(desktop.parent.iterdir()), [desktop])
        self.assertTrue(self.rollout.exists())

    def test_finish_requires_closed_before_mutating_deferred_stores(self):
        self.run_prune()
        with (
            patch.object(prune.subprocess, "run") as process_list,
            self.assertRaisesRegex(ValueError, "quit Codex"),
        ):
            process_list.return_value.stdout = "/Applications/Codex.app/Contents/MacOS/Codex\n"
            self.run_prune("finish-desktop-state")
        self.assertEqual(self.queue_owners(), [ORPHAN, RETAINED])
        self.assertEqual(json.loads(self.state.read_text()), self.state_value)


if __name__ == "__main__":
    unittest.main()
