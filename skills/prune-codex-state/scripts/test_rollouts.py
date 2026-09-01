import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rollouts

OWNER = "019eea2b-2edf-7053-9b27-4d4998d4fe91"
PHYSICAL = "01a1a2c8-95e6-7982-889d-7bb043693f6d"
BASE = "01a1d2c0-5c4f-7fe1-bb49-e478d72de7cb"
NAME = f"rollout-2026-11-05T20-20-01-{OWNER}_{PHYSICAL}.jsonl"


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.day = self.home / "sessions" / "2026" / "11" / "05"
        self.day.mkdir(parents=True)

    def test_inventory_reads_only_metadata_and_retains_distinct_physical_ids(self):
        archived = self.home / "archived_sessions"
        archived.mkdir()
        raw = json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": OWNER,
                    "history_base": {"thread_id": BASE},
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": BASE}}},
                    "base_instructions": "private text",
                },
            }
        ).encode()
        path = self.day / NAME
        path.write_bytes(raw + b"\nprivate transcript")
        compressed = archived / (NAME + ".zst")
        if rollouts.zstd is not None:
            compressed.write_bytes(rollouts.zstd.compress(path.read_bytes()))
        elif decoder := shutil.which("zstd"):
            subprocess.run([decoder, "-q", "-o", str(compressed), str(path)], check=True)
        else:
            self.skipTest("requires a zstd encoder")

        report = rollouts.inventory(self.home)

        self.assertEqual(report["unclassified"], [])
        self.assertEqual([r["store"] for r in report["rollouts"]], ["sessions", "archived_sessions"])
        for row in report["rollouts"]:
            with self.subTest(store=row["store"]):
                self.assertEqual(
                    (row["id"], row["rollout_id"], row["history_base_rollout_id"]), (OWNER, PHYSICAL, BASE)
                )
                self.assertEqual(row["spawn_parent_thread_id"], BASE)
                self.assertNotIn("private text", json.dumps(row))
                self.assertNotIn("private transcript", json.dumps(row))

    def test_corrupt_compressed_rollout_is_unclassified(self):
        if rollouts.zstd is None and not shutil.which("zstd"):
            self.skipTest("requires a zstd decoder")
        path = self.day / (NAME + ".zst")
        path.write_bytes(b"invalid frame")
        report = rollouts.inventory(self.home)
        self.assertEqual(report["rollouts"], [])
        self.assertEqual([r["path"] for r in report["unclassified"]], [str(path)])

    def test_inventory_flags_oversized_invalid_and_unreadable_ownership(self):
        path = self.day / NAME
        symlink = self.day / NAME.replace(PHYSICAL, BASE)
        symlink.symlink_to(path)
        path.write_bytes(b"x" * 33)
        with patch.object(rollouts, "HEADER_LIMIT", 32):
            report = rollouts.inventory(self.home)
        self.assertEqual(report["rollouts"], [])
        self.assertEqual(
            {r["reason"] for r in report["unclassified"]}, {"metadata record exceeds limit", "non-regular file"}
        )

        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": BASE}}) + "\n")
        report = rollouts.inventory(self.home)
        self.assertEqual(report["rollouts"], [])
        self.assertIn("filename and metadata disagree on thread ID", {r["reason"] for r in report["unclassified"]})

    def test_empty_pruning_cascades_and_preserves_root(self):
        removed = rollouts.prune_empty(self.home, [self.day / NAME])
        self.assertEqual(removed, [str(self.day), str(self.day.parent), str(self.day.parent.parent)])
        self.assertTrue((self.home / "sessions").is_dir())

    def test_hidden_file_stops_cascade(self):
        hidden = self.day.parent / ".DS_Store"
        hidden.touch()
        self.assertEqual(rollouts.prune_empty(self.home, [self.day / NAME]), [str(self.day)])
        self.assertTrue(hidden.exists())
        self.assertTrue(self.day.parent.parent.exists())

    def test_preflight_rejects_symlink_traversal_and_existing_rollout(self):
        symlink = self.day.parent / "06"
        symlink.symlink_to(self.day, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe session directory"):
            rollouts.prune_empty(self.home, [self.day / NAME, symlink / NAME])
        self.assertTrue(self.day.is_dir())

        file = self.day / NAME
        file.touch()
        with self.assertRaisesRegex(ValueError, "rollout still exists"):
            rollouts.prune_empty(self.home, [file])
        self.assertTrue(file.exists())


if __name__ == "__main__":
    unittest.main()
