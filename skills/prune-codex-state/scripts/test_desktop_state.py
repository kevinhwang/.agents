import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_state

THREAD = "8d0ced6a-6740-4cc9-813c-f35f418c1ea5"
KEEP = "887cae00-e0de-4130-bf8d-25243e91282a"
ATOM_SCOPE = "electron-owner-atoms:local-owner"


def edit(keys, value, thread_id=THREAD):
    return {"keys": keys, "thread_id": thread_id, "sha256": desktop_state.value_hash(value)}


class DesktopStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.home = root / "home"
        self.home.mkdir()
        self.backups = root / "backups"
        self.backups.mkdir(mode=0o700)
        self.primary, self.fallback = [self.home / name for name in desktop_state.STATE_FILES]

    def write(self, path, original):
        path.write_text(json.dumps(original), encoding="utf-8")
        return path.read_bytes()

    def read(self, path):
        return json.loads(path.read_bytes())

    def test_applies_to_each_latest_file_and_matches_list_by_id(self):
        activity_key = json.dumps(["local", THREAD])
        paths_key = json.dumps(["local", f"local:{THREAD}", "/workspace/output.txt"])
        assignment = "thread-project-assignments"
        workspace_list = "projectless-thread-ids"
        owner_map = "electron-owner-value:thread-writable-roots:local-owner"
        edits = [
            edit([assignment, THREAD], "workspace"),
            edit([workspace_list, 0], THREAD),
            edit([ATOM_SCOPE, "thread-user-activity-times-v1", activity_key], 12),
            edit([ATOM_SCOPE, "recent-workspace-files-v3", paths_key], True),
            edit([owner_map, THREAD], ["/workspace"]),
        ]
        original = {
            assignment: {THREAD: "workspace", KEEP: "kept workspace"},
            workspace_list: [KEEP, THREAD],
            ATOM_SCOPE: {
                "thread-user-activity-times-v1": {activity_key: 12},
                "recent-workspace-files-v3": {paths_key: True},
            },
            owner_map: {THREAD: ["/workspace"], KEEP: ["/keep"]},
        }
        primary = self.write(self.primary, {**original, "window": "primary"})
        fallback = self.write(self.fallback, {**original, "window": "fallback"})
        self.primary.chmod(0o640)

        desktop_state.finish(self.home, edits, self.backups)

        expected = {
            assignment: {KEEP: "kept workspace"},
            workspace_list: [KEEP],
            ATOM_SCOPE: {"thread-user-activity-times-v1": {}, "recent-workspace-files-v3": {}},
            owner_map: {KEEP: ["/keep"]},
        }
        self.assertEqual(self.read(self.primary), {**expected, "window": "primary"})
        self.assertEqual(self.read(self.fallback), {**expected, "window": "fallback"})
        self.assertEqual(stat.S_IMODE(self.primary.stat().st_mode), 0o640)
        originals = list(self.backups.glob("*.original"))
        self.assertCountEqual([file.read_bytes() for file in originals], [primary, fallback])
        self.assertTrue(all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in originals))

    def test_failure_in_second_replace_retries_without_overwriting_backups(self):
        path = "thread-project-membership-host-ids"
        edits = [edit([path, THREAD], "local")]
        primary = self.write(self.primary, {path: {THREAD: "local", KEEP: "local"}})
        fallback = self.write(self.fallback, {path: {THREAD: "local"}})
        replace = os.replace

        def fail_fallback(source, dest):
            if dest == self.fallback:
                raise OSError("interrupted replacement")
            replace(source, dest)

        with (
            patch.object(desktop_state.os, "replace", side_effect=fail_fallback),
            self.assertRaisesRegex(OSError, "interrupted replacement"),
        ):
            desktop_state.finish(self.home, edits, self.backups)
        self.assertEqual(self.read(self.primary), {path: {KEEP: "local"}})
        self.assertEqual(self.fallback.read_bytes(), fallback)
        backups = {file: file.stat().st_ino for file in self.backups.glob("*.original")}

        desktop_state.finish(self.home, edits, self.backups)
        desktop_state.finish(self.home, edits, self.backups)

        self.assertEqual(self.read(self.fallback), {path: {}})
        self.assertEqual({file: file.stat().st_ino for file in self.backups.glob("*.original")}, backups)
        self.assertCountEqual([file.read_bytes() for file in backups], [primary, fallback])

    def test_prevalidates_each_copy_before_creating_backups_or_mutating(self):
        assignment = "thread-project-assignments"
        workspace_list = "projectless-thread-ids"
        edits = [edit([assignment, THREAD], "workspace"), edit([workspace_list, 0], THREAD)]
        primary = self.write(self.primary, {assignment: {THREAD: "workspace"}, workspace_list: [THREAD]})

        for fallback, error in (
            ({assignment: {THREAD: "changed"}}, "value changed"),
            ({workspace_list: [THREAD, THREAD]}, "duplicate ID"),
            ({workspace_list: {THREAD: True}}, "changed container type"),
        ):
            with self.subTest(fallback=fallback):
                original = self.write(self.fallback, fallback)

                with self.assertRaisesRegex(ValueError, error):
                    desktop_state.finish(self.home, edits, self.backups)

                self.assertEqual(self.primary.read_bytes(), primary)
                self.assertEqual(self.fallback.read_bytes(), original)
                self.assertEqual(list(self.backups.iterdir()), [])

    def test_rejects_paths_that_do_not_identify_the_approved_local_thread(self):
        for keys in (
            ["thread-project-assignments", KEEP],
            ["unrelated", THREAD],
            ["projectless-thread-ids", True],
            [ATOM_SCOPE, "thread-user-activity-times-v1", json.dumps(["remote", THREAD])],
            [ATOM_SCOPE, "recent-workspace-files-v3", json.dumps(["local", f"local:{KEEP}", "/output"])],
        ):
            with self.subTest(keys=keys), self.assertRaisesRegex(ValueError, "approved thread"):
                desktop_state.validate_edits([edit(keys, True)])

    def test_skips_missing_keys_and_rejects_symlinked_state(self):
        edits = [edit([ATOM_SCOPE, f"thread-reference-capability:{THREAD}"], True)]
        untouched = self.write(self.primary, {ATOM_SCOPE: {}})

        desktop_state.finish(self.home, edits, self.backups)

        self.assertEqual(self.primary.read_bytes(), untouched)
        self.assertFalse(self.fallback.exists())
        self.assertEqual(list(self.backups.iterdir()), [])
        self.fallback.symlink_to(self.primary)
        with self.assertRaises(OSError):
            desktop_state.finish(self.home, edits, self.backups)


if __name__ == "__main__":
    unittest.main()
