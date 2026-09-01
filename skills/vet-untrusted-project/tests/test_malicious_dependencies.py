from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_malicious_dependencies.py"
)
sys.path.insert(0, str(SCRIPT.parent))

import check_malicious_dependencies as checker


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


class DependencyInventoryTest(unittest.TestCase):
    def test_parses_common_lockfiles_without_expanding_dependency_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write(
                root / "javascript/package-lock.json",
                """
                {
                  "lockfileVersion": 3,
                  "packages": {
                    "": {"name": "demo"},
                    "node_modules/public-npm": {
                      "version": "1.2.3",
                      "resolved": "https://registry.npmjs.org/public-npm/-/public-npm-1.2.3.tgz"
                    },
                    "node_modules/private-npm": {
                      "version": "4.5.6",
                      "resolved": "https://packages.example.invalid/private-npm.tgz"
                    }
                  }
                }
                """,
            )
            write(
                root / "rust/Cargo.lock",
                """
                version = 3

                [[package]]
                name = "public-crate"
                version = "2.0.0"
                source = "registry+https://github.com/rust-lang/crates.io-index"

                [[package]]
                name = "local-crate"
                version = "0.1.0"
                """,
            )
            write(
                root / "python/Pipfile.lock",
                """
                {
                  "_meta": {"sources": [{"url": "https://pypi.org/simple"}]},
                  "default": {"public-python": {"version": "==3.1.4"}},
                  "develop": {}
                }
                """,
            )
            write(
                root / "ruby/Gemfile.lock",
                """
                GEM
                  remote: https://rubygems.org/
                  specs:
                    public-gem (5.0.0)
                      dependency (~> 1.0)

                DEPENDENCIES
                  public-gem
                """,
            )
            write(
                root / "dotnet/packages.lock.json",
                """
                {
                  "version": 1,
                  "dependencies": {
                    "net8.0": {"Public.NuGet": {"resolved": "6.0.0"}}
                  }
                }
                """,
            )
            write(
                root / "java/gradle.lockfile",
                """
                org.example:library:7.0.0=runtimeClasspath
                empty=annotationProcessor
                """,
            )
            write(
                root / "go/go.mod",
                """
                module example.invalid/demo

                require (
                    github.com/example/public v1.2.0
                )
                """,
            )
            write(
                root / "yarn/yarn.lock",
                """
                public-yarn@^1.0.0:
                  version "1.1.0"
                  resolved "https://registry.yarnpkg.com/public-yarn/-/public-yarn-1.1.0.tgz#abcdef"
                """,
            )
            write(
                root / "pnpm/pnpm-lock.yaml",
                """
                lockfileVersion: '9.0'

                packages:
                  public-pnpm@3.0.0:
                    resolution:
                      tarball: https://registry.npmjs.org/public-pnpm/-/public-pnpm-3.0.0.tgz
                  '@scope/unverified-pnpm@4.0.0':
                    resolution: {integrity: sha512-placeholder}

                snapshots:
                  must-not-appear@9.9.9: {}
                """,
            )
            write(
                root / "node_modules/ignored/package-lock.json",
                '{"packages":{"node_modules/must-not-appear":{"version":"9.9.9"}}}',
            )

            inventory = checker.inventory_dependencies(
                root,
                max_files=100,
                max_file_bytes=1_000_000,
                max_total_bytes=10_000_000,
            )
            by_name = {
                coordinate.name: coordinate for coordinate in inventory.coordinates
            }

            self.assertTrue(by_name["public-npm"].public_source)
            self.assertFalse(by_name["private-npm"].public_source)
            self.assertTrue(by_name["public-crate"].public_source)
            self.assertFalse(by_name["local-crate"].public_source)
            self.assertTrue(by_name["public-python"].public_source)
            self.assertTrue(by_name["public-gem"].public_source)
            self.assertFalse(by_name["Public.NuGet"].public_source)
            self.assertFalse(by_name["org.example:library"].public_source)
            self.assertFalse(by_name["github.com/example/public"].public_source)
            self.assertTrue(by_name["public-yarn"].public_source)
            self.assertTrue(by_name["public-pnpm"].public_source)
            self.assertFalse(by_name["@scope/unverified-pnpm"].public_source)
            self.assertNotIn("must-not-appear", by_name)
            self.assertFalse(inventory.errors)

    def test_does_not_follow_symlinked_dependency_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            root = temporary / "target"
            outside = temporary / "outside"
            root.mkdir()
            outside.mkdir()
            write(
                outside / "package-lock.json",
                '{"packages":{"node_modules/outside":{"version":"1.0.0"}}}',
            )
            (root / "package-lock.json").symlink_to(outside / "package-lock.json")

            inventory = checker.inventory_dependencies(
                root,
                max_files=100,
                max_file_bytes=1_000_000,
                max_total_bytes=10_000_000,
            )

            self.assertFalse(inventory.coordinates)
            self.assertFalse(inventory.files)

    def test_queries_only_source_verified_coordinates_by_default(self) -> None:
        coordinates = [
            checker.Coordinate(
                "npm",
                "public-package",
                "1.0.0",
                True,
                "https://registry.npmjs.org/public-package",
                "package-lock.json",
            ),
            checker.Coordinate(
                "npm",
                "unverified-package",
                "2.0.0",
                False,
                "manifest-only",
                "package.json",
            ),
        ]

        selected, skipped, truncated = checker.choose_query_candidates(
            coordinates, include_unverified=False, max_packages=100
        )

        self.assertEqual(
            [coordinate.name for coordinate in selected], ["public-package"]
        )
        self.assertEqual(
            [coordinate.name for coordinate in skipped], ["unverified-package"]
        )
        self.assertFalse(truncated)

    def test_prioritizes_verified_exact_coordinates_when_bounded(self) -> None:
        coordinates = [
            checker.Coordinate("npm", "z-unverified", None, False, "", "package.json"),
            checker.Coordinate("npm", "b-public", None, True, "", "package-lock.json"),
            checker.Coordinate(
                "npm", "a-public", "1.0.0", True, "", "package-lock.json"
            ),
        ]

        selected, _, truncated = checker.choose_query_candidates(
            coordinates, include_unverified=True, max_packages=1
        )

        self.assertEqual(selected[0].name, "a-public")
        self.assertTrue(truncated)

    def test_redacts_credentials_and_query_parameters_from_sources(self) -> None:
        coordinate = checker.make_coordinate(
            "npm",
            "public-package",
            "1.0.0",
            source="https://user:secret@registry.npmjs.org/public-package.tgz?token=secret#fragment",
            path="package-lock.json",
        )

        self.assertIsNotNone(coordinate)
        self.assertTrue(coordinate.public_source)
        self.assertEqual(
            coordinate.source,
            "https://registry.npmjs.org/public-package.tgz",
        )


class AdvisoryQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinate = checker.Coordinate(
            "PyPI",
            "malicious-demo",
            "1.0.0",
            True,
            "https://pypi.org/simple",
            "Pipfile.lock",
        )

    def test_osv_retains_only_malware_records(self) -> None:
        requests: list[tuple[str, str, object | None]] = []

        def request(
            method: str,
            url: str,
            payload: object | None,
            _headers: dict[str, str],
            _timeout: int,
        ) -> object:
            requests.append((method, url, payload))
            if method == "POST":
                return {
                    "results": [
                        {
                            "vulns": [
                                {"id": "MAL-2026-1234"},
                                {"id": "GHSA-ordinary-vulnerability"},
                            ]
                        }
                    ]
                }
            return {
                "id": "MAL-2026-1234",
                "summary": "Malicious package",
                "affected": [
                    {
                        "package": {
                            "ecosystem": "PyPI",
                            "name": "malicious-demo",
                        }
                    }
                ],
            }

        status, hits = checker.query_osv(
            [self.coordinate], timeout=10, requester=request
        )

        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["ordinary_advisories_ignored"], 1)
        self.assertEqual([hit["id"] for hit in hits], ["MAL-2026-1234"])
        self.assertEqual(len(requests), 2)
        query = requests[0][2]
        self.assertEqual(query["queries"][0]["version"], "1.0.0")

    def test_github_requests_only_malware_and_maps_matching_package(self) -> None:
        requested_urls: list[str] = []

        def request(
            _method: str,
            url: str,
            _payload: object | None,
            _headers: dict[str, str],
            _timeout: int,
        ) -> object:
            requested_urls.append(url)
            return [
                {
                    "ghsa_id": "GHSA-malware-demo",
                    "type": "malware",
                    "summary": "Malicious package",
                    "withdrawn_at": None,
                    "vulnerabilities": [
                        {
                            "package": {"ecosystem": "pip", "name": "malicious-demo"},
                            "vulnerable_version_range": "= 1.0.0",
                        }
                    ],
                }
            ]

        status, hits = checker.query_github(
            [self.coordinate], timeout=10, requester=request
        )

        query = parse_qs(urlsplit(requested_urls[0]).query)
        self.assertEqual(query["type"], ["malware"])
        self.assertEqual(query["ecosystem"], ["pip"])
        self.assertEqual(query["affects"], ["malicious-demo@1.0.0"])
        self.assertEqual(status["status"], "complete")
        self.assertEqual(hits[0]["id"], "GHSA-malware-demo")
        self.assertEqual(hits[0]["package"]["paths"], ["Pipfile.lock"])

    def test_osv_ignores_withdrawn_malware_records(self) -> None:
        def request(
            method: str,
            _url: str,
            _payload: object | None,
            _headers: dict[str, str],
            _timeout: int,
        ) -> object:
            if method == "POST":
                return {"results": [{"vulns": [{"id": "MAL-2026-withdrawn"}]}]}
            return {
                "id": "MAL-2026-withdrawn",
                "withdrawn": "2026-08-01T00:00:00Z",
            }

        status, hits = checker.query_osv(
            [self.coordinate], timeout=10, requester=request
        )

        self.assertFalse(hits)
        self.assertEqual(status["withdrawn_records_ignored"], 1)

    def test_rejects_non_allowlisted_advisory_endpoints_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "disallowed advisory endpoint"):
            checker.request_json(
                "GET", "https://example.invalid/advisories", None, {}, 10
            )


class CommandLineTest(unittest.TestCase):
    def test_inventory_only_mode_is_read_only_and_makes_no_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write(
                root / "package-lock.json",
                """
                {
                  "lockfileVersion": 3,
                  "packages": {
                    "node_modules/example": {
                      "version": "1.0.0",
                      "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz"
                    }
                  }
                }
                """,
            )

            completed = subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--inventory-only", str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["providers"]["OSV"]["status"], "not_checked")
            self.assertEqual(result["classification_input"], "NOT_CHECKED")
            self.assertTrue(result["complete"])
            self.assertFalse(result["invariants"]["executes_target_code"])
            self.assertFalse(result["invariants"]["invokes_package_managers"])
            self.assertFalse(result["invariants"]["writes_target"])


if __name__ == "__main__":
    unittest.main()
