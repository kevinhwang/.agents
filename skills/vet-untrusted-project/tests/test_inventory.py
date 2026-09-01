from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory.py"


def run_inventory(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class InventoryTest(unittest.TestCase):
    def test_inventory_reports_security_relevant_topology_without_following_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            root = temporary / "target"
            outside = temporary / "outside"
            (root / ".claude").mkdir(parents=True)
            outside.mkdir()

            (root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
            (root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            executable = root / "setup.sh"
            executable.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            (root / "build.mjs").write_text("export {};\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_import.py").write_text(
                "assert True\n", encoding="utf-8"
            )
            (outside / "must-not-be-seen.txt").write_text("outside\n", encoding="utf-8")
            (root / "external-link").symlink_to(outside, target_is_directory=True)
            os.link(root / "Makefile", root / "Makefile.hardlink")
            (root / ".git").write_text(
                f"gitdir: {outside / 'git-admin'}\n", encoding="utf-8"
            )

            completed = run_inventory(str(root))

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            report = payload["roots"][0]
            details = report["details"]
            candidates = {item["path"] for item in details["entrypoint_candidates"]}
            candidate_priorities = {
                item["path"]: item["priority"]
                for item in details["entrypoint_candidates"]
            }
            symlinks = {item["path"]: item for item in details["symlinks"]}
            hardlinks = {item["path"] for item in details["hardlinked_files"]}
            executables = {item["path"] for item in details["executable_files"]}

            self.assertIn(".claude/settings.json", candidates)
            self.assertIn(".git", candidates)
            self.assertIn("Makefile", candidates)
            self.assertIn("build.mjs", candidates)
            self.assertIn("setup.sh", candidates)
            self.assertNotIn("tests/test_import.py", candidates)
            self.assertEqual(details["test_trees"][0]["path"], "tests")
            self.assertEqual(
                candidate_priorities[".claude/settings.json"], "automatic_or_early"
            )
            self.assertEqual(candidate_priorities["setup.sh"], "common_entrypoint")
            self.assertEqual(symlinks["external-link"]["target_scope"], "outside_root")
            self.assertNotIn("external-link/must-not-be-seen.txt", candidates)
            self.assertIn("Makefile", hardlinks)
            self.assertIn("Makefile.hardlink", hardlinks)
            self.assertIn("setup.sh", executables)
            self.assertEqual(details["git_pointers"][0]["kind"], "worktree_git_file")
            self.assertIn(
                "tests/test_import.py",
                {item["path"] for item in details["regular_files"]},
            )
            self.assertFalse(payload["invariants"]["follows_symlinks"])
            self.assertFalse(payload["invariants"]["writes_files"])

    def test_bulk_trees_are_summarized_unless_explicitly_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / ".venv" / "bin" / "payload.sh").write_text(
                "#!/usr/bin/env bash\n", encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("pass\n", encoding="utf-8")

            summarized = run_inventory(str(root))
            expanded = run_inventory("--include-bulk", str(root))

            self.assertEqual(summarized.returncode, 0, summarized.stderr)
            summarized_report = json.loads(summarized.stdout)["roots"][0]
            self.assertFalse(summarized_report["includes_bulk_trees"])
            self.assertEqual(
                summarized_report["details"]["bulk_trees"][0]["path"], ".venv"
            )
            self.assertNotIn(
                ".venv/bin/payload.sh",
                {
                    item["path"]
                    for item in summarized_report["details"].get(
                        "entrypoint_candidates", []
                    )
                },
            )

            self.assertEqual(expanded.returncode, 0, expanded.stderr)
            expanded_report = json.loads(expanded.stdout)["roots"][0]
            self.assertTrue(expanded_report["includes_bulk_trees"])
            self.assertIn(
                ".venv/bin/payload.sh",
                {
                    item["path"]
                    for item in expanded_report["details"]["entrypoint_candidates"]
                },
            )

    def test_symlink_root_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            physical_root = temporary / "physical"
            physical_root.mkdir()
            (physical_root / "payload.sh").write_text("exit 0\n", encoding="utf-8")
            linked_root = temporary / "linked"
            linked_root.symlink_to(physical_root, target_is_directory=True)

            completed = run_inventory(str(linked_root))

            self.assertEqual(completed.returncode, 2)
            report = json.loads(completed.stdout)["roots"][0]
            self.assertEqual(report["root_type"], "symlink")
            self.assertEqual(report["fatal_error"], "root is a symlink; not followed")
            self.assertNotIn("counts", report)

    def test_git_directory_and_local_config_are_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".git" / "objects" / "aa").mkdir(parents=True)
            (root / ".git" / "config").write_text(
                "[core]\n\thooksPath = .githooks\n", encoding="utf-8"
            )
            (root / ".git" / "objects" / "aa" / "opaque").write_text(
                "object\n", encoding="utf-8"
            )

            completed = run_inventory(str(root))

            self.assertEqual(completed.returncode, 0, completed.stderr)
            details = json.loads(completed.stdout)["roots"][0]["details"]
            candidates = {item["path"] for item in details["entrypoint_candidates"]}
            vcs_roots = {item["path"] for item in details["vcs_roots"]}
            self.assertIn(".git", candidates)
            self.assertIn(".git/config", candidates)
            self.assertIn(".git", vcs_roots)
            self.assertEqual(details["opaque_vcs_storage"][0]["path"], ".git/objects")
            self.assertEqual(
                json.loads(completed.stdout)["roots"][0]["entries_seen"], 3
            )

    def test_entry_limit_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index in range(5):
                (root / f"file-{index}").write_text("x", encoding="utf-8")

            completed = run_inventory("--max-entries", "2", str(root))

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)["roots"][0]
            self.assertEqual(report["entries_seen"], 2)
            self.assertTrue(report["truncated"])


if __name__ == "__main__":
    unittest.main()
