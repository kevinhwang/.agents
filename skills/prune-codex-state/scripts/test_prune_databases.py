import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import prune_databases

CANDIDATE = "019eea2b-2edf-7053-9b27-4d4998d4fe91"
RETAINED = "01a1a2c8-95e6-7982-889d-7bb043693f6d"


class PruneDatabasesTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = root / "codex"
        self.home.mkdir()
        self.run = root / "run"
        self.run.mkdir(mode=0o700)

    def connect(self, name):
        return closing(sqlite3.connect(self.home / name, isolation_level=None))

    def snapshot(self, name, *tables):
        return prune_databases.snapshot(
            self.home,
            [{"name": name, "rows": [{"table": table, "thread_id": CANDIDATE} for table in tables]}],
        )[0]

    def logs(self):
        with self.connect("logs_2.sqlite") as db:
            db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, body BLOB)")
            db.executemany("INSERT INTO logs VALUES (?, ?, ?)", [(1, CANDIDATE, b"\x00\xff"), (2, RETAINED, "keep")])

    def test_applies_exact_rows_in_supported_stores_and_keeps_preimages(self):
        for name, tables in prune_databases.TABLES.items():
            with self.subTest(database=name):
                with self.connect(name) as db:
                    for table, column in tables.items():
                        db.execute(f"CREATE TABLE {table}({column} TEXT PRIMARY KEY, attached_thread_parent_id TEXT)")
                        db.executemany(f"INSERT INTO {table} VALUES (?, NULL)", [(CANDIDATE,), (RETAINED,)])
                    if name == "state_5.sqlite":
                        db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT)")
                plan = self.snapshot(name, *tables)

                self.assertEqual(prune_databases.apply(self.home, plan, self.run), len(tables))
                self.assertEqual(prune_databases.apply(self.home, plan, self.run), 0)

                with self.connect(name) as db, closing(sqlite3.connect(self.run / f"{name}.backup")) as backup:
                    for table, column in tables.items():
                        query = f"SELECT {column} FROM {table} ORDER BY {column}"
                        self.assertEqual(db.execute(query).fetchall(), [(RETAINED,)])
                        self.assertEqual(backup.execute(query).fetchall(), [(CANDIDATE,), (RETAINED,)])

    def test_resume_after_commit_uses_existing_backup_and_same_approved_plan(self):
        name = "logs_2.sqlite"
        self.logs()
        plan = self.snapshot(name, "logs")
        save_intent = prune_databases._save_intent

        def interrupt_after_commit(path, intent):
            if intent["applied"]:
                raise OSError("interrupted after SQLite commit")
            save_intent(path, intent)

        with (
            patch.object(prune_databases, "_save_intent", side_effect=interrupt_after_commit),
            self.assertRaisesRegex(OSError, "interrupted after SQLite commit"),
        ):
            prune_databases.apply(self.home, plan, self.run)
        saved_backup = (self.run / f"{name}.backup").read_bytes()

        new_plan = prune_databases.snapshot(
            self.home, [{"name": name, "rows": [{"table": "logs", "thread_id": RETAINED}]}]
        )
        with self.assertRaisesRegex(ValueError, "intent does not match"):
            prune_databases.apply(self.home, new_plan[0], self.run)
        self.assertEqual(prune_databases.apply(self.home, plan, self.run), 0)
        self.assertEqual((self.run / f"{name}.backup").read_bytes(), saved_backup)
        with self.connect(name) as db:
            self.assertEqual(db.execute("SELECT thread_id FROM logs").fetchall(), [(RETAINED,)])

    def test_content_and_schema_drifts_refuse_apply_before_backup(self):
        name = "logs_2.sqlite"
        self.logs()
        plan = self.snapshot(name, "logs")
        with self.connect(name) as db:
            db.execute("UPDATE logs SET body=? WHERE thread_id=?", (b"\x00\x0f", CANDIDATE))

        with self.assertRaisesRegex(ValueError, "approved rows changed"):
            prune_databases.apply(self.home, plan, self.run)
        plan = self.snapshot(name, "logs")
        with self.connect(name) as db:
            db.execute("ALTER TABLE logs ADD COLUMN extra TEXT")
        with self.assertRaisesRegex(ValueError, "schema changed"):
            prune_databases.apply(self.home, plan, self.run)
        self.assertFalse(list(self.run.iterdir()))

    def test_rows_missing_before_any_intent_are_not_treated_as_recovery(self):
        name = "logs_2.sqlite"
        self.logs()
        plan = self.snapshot(name, "logs")
        with self.connect(name) as db:
            db.execute("DELETE FROM logs WHERE thread_id=?", (CANDIDATE,))

        with self.assertRaisesRegex(ValueError, "approved rows changed"):
            prune_databases.apply(self.home, plan, self.run)
        self.assertFalse(list(self.run.iterdir()))

    def test_refuses_related_rows_and_unknown_or_mutating_schema(self):
        name = "state_5.sqlite"
        for precondition, error in (
            ("INSERT INTO thread_spawn_edges VALUES (?, ?)", "related row.*thread_spawn_edges"),
            ("UPDATE threads SET attached_thread_parent_id=? WHERE id=?", "related row.*threads"),
            ("INSERT INTO thread_attachments VALUES (?, ?)", "related row.*thread_attachments"),
            ("CREATE TABLE hidden_ref(ref TEXT REFERENCES threads(id) ON DELETE CASCADE)", "unexpected incoming"),
            ("CREATE TRIGGER cleanup AFTER DELETE ON threads BEGIN DELETE FROM thread_attachments; END", "trigger"),
        ):
            with self.subTest(sql=precondition), self.connect(name) as db:
                db.executescript(
                    """
                    CREATE TABLE threads(id TEXT PRIMARY KEY, attached_thread_parent_id TEXT);
                    CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
                    CREATE TABLE thread_attachments(thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE, path TEXT);
                    """
                )
                db.executemany("INSERT INTO threads VALUES (?, NULL)", [(CANDIDATE,), (RETAINED,)])
                db.execute(precondition, (CANDIDATE, RETAINED) if "?" in precondition else ())
                plan = self.snapshot(name, "threads")

                with self.assertRaisesRegex(ValueError, error):
                    prune_databases.apply(self.home, plan, self.run)

                self.assertEqual(db.execute("SELECT count(*) FROM threads").fetchone(), (2,))
                self.assertFalse(list(self.run.iterdir()))
            (self.home / name).unlink()

    def test_snapshot_and_backup_cover_committed_wal_content(self):
        name = "logs_2.sqlite"
        self.logs()
        with self.connect(name) as reader:
            reader.execute("PRAGMA journal_mode=WAL")
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT count(*) FROM logs").fetchone(), (2,))
            with self.connect(name) as writer:
                writer.execute("UPDATE logs SET body=? WHERE thread_id=?", (b"committed to WAL", CANDIDATE))
            plan = self.snapshot(name, "logs")

            self.assertEqual(prune_databases.apply(self.home, plan, self.run), 1)
            with closing(sqlite3.connect(self.run / f"{name}.backup")) as backup:
                self.assertEqual(
                    backup.execute("SELECT body FROM logs WHERE thread_id=?", (CANDIDATE,)).fetchone(),
                    (b"committed to WAL",),
                )

    def test_allows_installed_trigger_shapes_when_no_related_rows_match(self):
        name = "thread_history_1.sqlite"
        with self.connect(name) as db:
            db.executescript(
                """
                CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY);
                CREATE TABLE thread_realtime_items(thread_id TEXT NOT NULL, item_id TEXT);
                CREATE TRIGGER thread_realtime_items_projection_cleanup
                AFTER DELETE ON thread_history_projection_state
                BEGIN
                    DELETE FROM thread_realtime_items WHERE thread_id = OLD.thread_id;
                END;
                CREATE TRIGGER projection_after_insert AFTER INSERT ON thread_history_projection_state
                BEGIN SELECT 1; END;
                """
            )
            db.execute("INSERT INTO thread_history_projection_state VALUES (?)", (CANDIDATE,))
            db.execute("INSERT INTO thread_realtime_items VALUES (?, 'keep')", (RETAINED,))
            db.execute("INSERT INTO thread_realtime_items VALUES (?, 'unapproved')", (CANDIDATE,))
            plan = self.snapshot(name, "thread_history_projection_state")

            with self.assertRaisesRegex(ValueError, "related row.*thread_realtime_items"):
                prune_databases.apply(self.home, plan, self.run)
            db.execute("DELETE FROM thread_realtime_items WHERE thread_id=?", (CANDIDATE,))
            self.assertEqual(prune_databases.apply(self.home, plan, self.run), 1)
            self.assertEqual(db.execute("SELECT thread_id FROM thread_realtime_items").fetchall(), [(RETAINED,)])


if __name__ == "__main__":
    unittest.main()
