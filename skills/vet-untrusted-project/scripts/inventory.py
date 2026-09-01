#!/usr/bin/env python3
"""Read-only filesystem inventory for an untrusted project directory.

The scanner intentionally does not parse or execute project configuration. It uses
lstat-style traversal, does not follow symlinks, stays on each root filesystem,
imports no target code, writes nothing, and emits JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import unicodedata
from collections import Counter
from typing import Any

VERSION = "2.0.0"
DEFAULT_MAX_ENTRIES = 200_000
DEFAULT_MAX_FINDINGS = 500
MAX_GIT_POINTER_BYTES = 16_384

AGENT_DIRS = {
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    ".cline",
    ".continue",
    ".github",
    ".opencode",
    ".roo",
    ".windsurf",
}

EXACT_CANDIDATE_NAMES = {
    ".bazelrc",
    ".cursorrules",
    ".env",
    ".envrc",
    ".gitattributes",
    ".gitmodules",
    ".mcp.json",
    ".npmrc",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    ".pypirc",
    ".ruby-version",
    ".tool-versions",
    ".yarnrc",
    ".yarnrc.yml",
    "agents.md",
    "agents.override.md",
    "build",
    "build.bat",
    "build.gradle",
    "build.gradle.kts",
    "build.rs",
    "build.xml",
    "cargo.lock",
    "cargo.toml",
    "claude.local.md",
    "claude.md",
    "cmakelists.txt",
    "composer.json",
    "composer.lock",
    "conanfile.py",
    "conanfile.txt",
    "conftest.py",
    "configure",
    "configure.ac",
    "devcontainer.json",
    "directory.build.props",
    "directory.build.targets",
    "docker-compose.yaml",
    "docker-compose.yml",
    "dockerfile",
    "flake.lock",
    "flake.nix",
    "gemfile",
    "gemfile.lock",
    "gemspec",
    "go.mod",
    "go.sum",
    "go.work",
    "gradle.properties",
    "gradlew",
    "gradlew.bat",
    "jenkinsfile",
    "justfile",
    "makefile",
    "manifest.in",
    "meson.build",
    "mise.toml",
    "module.bazel",
    "mvnw",
    "mvnw.cmd",
    "package-lock.json",
    "package.json",
    "packages.lock.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "pnpmfile.cjs",
    "poetry.lock",
    "pom.xml",
    "pyproject.toml",
    "pytest.ini",
    "rakefile",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "shell.nix",
    "taskfile.yaml",
    "taskfile.yml",
    "tox.ini",
    "vagrantfile",
    "workspace",
    "workspace.bazel",
    "yarn.lock",
}

SCRIPT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".fish",
    ".nu",
    ".ps1",
    ".sh",
    ".zsh",
}

TEST_DIRECTORY_NAMES = {
    "__tests__",
    "bench",
    "benches",
    "benchmark",
    "benchmarks",
    "example",
    "examples",
    "spec",
    "specs",
    "test",
    "tests",
}

BULK_DIRECTORY_NAMES = {
    ".build": "generated build output",
    ".bundle": "bundled package-manager state",
    ".cache": "generated cache",
    ".gradle": "Gradle cache and generated state",
    ".mypy_cache": "generated Python analysis cache",
    ".next": "generated web build output",
    ".pytest_cache": "generated test cache",
    ".ruff_cache": "generated Python analysis cache",
    ".terraform": "downloaded Terraform plugins and generated state",
    ".tox": "generated Python test environments",
    ".venv": "bundled Python environment",
    "__pycache__": "generated Python bytecode cache",
    "build": "generated build output",
    "coverage": "generated coverage output",
    "deriveddata": "generated Apple build output",
    "dist": "generated distribution output",
    "node_modules": "bundled JavaScript dependencies",
    "out": "generated build output",
    "target": "generated build output",
    "venv": "bundled Python environment",
}

BULK_PATH_SUFFIXES = {
    ".yarn/cache": "bundled Yarn package cache",
    ".yarn/sdks": "generated Yarn SDKs",
    ".yarn/unplugged": "expanded Yarn dependencies",
}

EXECUTION_ORIENTED_STEMS = {
    "bootstrap",
    "build",
    "configure",
    "deploy",
    "generate",
    "install",
    "migrate",
    "publish",
    "release",
    "run",
    "serve",
    "setup",
    "start",
    "task",
}

EXECUTION_ORIENTED_SUFFIXES = {
    "",
    ".bat",
    ".cjs",
    ".cmd",
    ".fish",
    ".js",
    ".lua",
    ".mjs",
    ".nu",
    ".php",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".ts",
    ".zsh",
}

ARCHIVE_SUFFIXES = {
    ".7z",
    ".apk",
    ".bz2",
    ".crate",
    ".deb",
    ".dmg",
    ".egg",
    ".gem",
    ".gz",
    ".iso",
    ".jar",
    ".msi",
    ".nupkg",
    ".pkg",
    ".rar",
    ".rpm",
    ".tar",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tgz",
    ".txz",
    ".war",
    ".whl",
    ".xz",
    ".zip",
}

BINARY_SUFFIXES = {
    ".a",
    ".appimage",
    ".bin",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".lib",
    ".node",
    ".o",
    ".obj",
    ".pyd",
    ".pyc",
    ".so",
    ".wasm",
}

BIDI_CONTROL_CODEPOINTS = {
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
}

FILE_FLAG_CONSTANT_NAMES = (
    "UF_NODUMP",
    "UF_IMMUTABLE",
    "UF_APPEND",
    "UF_OPAQUE",
    "UF_NOUNLINK",
    "UF_COMPRESSED",
    "UF_TRACKED",
    "UF_DATAVAULT",
    "UF_HIDDEN",
    "SF_ARCHIVED",
    "SF_IMMUTABLE",
    "SF_APPEND",
    "SF_RESTRICTED",
    "SF_NOUNLINK",
    "SF_SNAPSHOT",
    "SF_FIRMLINK",
    "SF_DATALESS",
)

SECURITY_RELEVANT_FILE_FLAGS = {
    "UF_IMMUTABLE",
    "UF_APPEND",
    "UF_NOUNLINK",
    "UF_DATAVAULT",
    "SF_IMMUTABLE",
    "SF_APPEND",
    "SF_RESTRICTED",
    "SF_NOUNLINK",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory untrusted directories without following symlinks or writing files."
    )
    parser.add_argument(
        "directories",
        nargs="*",
        default=["."],
        help="Directories to inventory; defaults to cwd.",
    )
    parser.add_argument(
        "--max-entries",
        type=positive_int,
        default=DEFAULT_MAX_ENTRIES,
        help=f"Maximum entries per root before stopping (default: {DEFAULT_MAX_ENTRIES}).",
    )
    parser.add_argument(
        "--max-findings",
        type=positive_int,
        default=DEFAULT_MAX_FINDINGS,
        help=f"Maximum details retained per finding category (default: {DEFAULT_MAX_FINDINGS}).",
    )
    parser.add_argument(
        "--include-bulk",
        action="store_true",
        help="Expand generated, cache, and bundled dependency trees instead of summarizing them.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def mode_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "unknown"


def octal_mode(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def lexical_target(link_path: str, target: str) -> str:
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.abspath(os.path.join(os.path.dirname(link_path), target))


def is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def split_execution_oriented_name(name: str) -> tuple[str, str]:
    lowered = name.casefold()
    for suffix in sorted(EXECUTION_ORIENTED_SUFFIXES - {""}, key=len, reverse=True):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)], suffix
    return lowered, ""


def decode_file_flags(value: int) -> tuple[list[str], int]:
    names: list[str] = []
    remaining = value
    for name in FILE_FLAG_CONSTANT_NAMES:
        flag = getattr(stat, name, None)
        if isinstance(flag, int) and flag != 0 and value & flag:
            names.append(name)
            remaining &= ~flag
    return names, remaining


def candidate_reasons(relative_path: str, entry_type: str) -> list[str]:
    normalized = relative_path.replace(os.sep, "/")
    lowered = normalized.casefold()
    rooted = f"/{lowered}"
    basename = normalized.rsplit("/", 1)[-1]
    basename_lower = basename.casefold()
    parts = lowered.split("/")
    reasons: list[str] = []

    if basename_lower in EXACT_CANDIDATE_NAMES:
        reasons.append("known project, execution, dependency, or instruction filename")

    if basename_lower == ".git":
        reasons.append("Git administrative directory or pointer")

    if basename_lower.endswith(".gemspec"):
        reasons.append("Ruby package build metadata")

    if entry_type == "regular" and any(
        basename_lower.endswith(suffix) for suffix in SCRIPT_SUFFIXES
    ):
        reasons.append("shell or command script")

    execution_stem, execution_suffix = split_execution_oriented_name(basename_lower)
    if (
        entry_type == "regular"
        and execution_stem in EXECUTION_ORIENTED_STEMS
        and execution_suffix in EXECUTION_ORIENTED_SUFFIXES
    ):
        reasons.append("execution-oriented script or program name")

    if any(part in AGENT_DIRS for part in parts):
        reasons.append("agent, IDE, or automation configuration")

    if lowered.startswith(".github/workflows/") or "/.github/workflows/" in lowered:
        reasons.append("CI workflow")

    if "/.git/hooks/" in rooted or "/.githooks/" in rooted:
        reasons.append("Git hook")

    if rooted.endswith(("/.git/config", "/.git/config.worktree")):
        reasons.append("Git executable configuration")

    if "/.git/worktrees/" in rooted or "/.git/modules/" in rooted:
        reasons.append("Git linked-worktree or submodule metadata")

    if lowered.startswith(".vscode/") or "/.vscode/" in lowered:
        reasons.append("editor workspace configuration")

    if lowered.startswith(".idea/") or "/.idea/" in lowered:
        reasons.append("IDE project configuration")

    if lowered.startswith(".devcontainer/") or "/.devcontainer/" in lowered:
        reasons.append("development-container configuration")

    if lowered.startswith(".mvn/") or "/.mvn/" in lowered:
        reasons.append("Maven wrapper or extension configuration")

    if lowered.startswith(".cargo/") or "/.cargo/" in lowered:
        reasons.append("Cargo executable configuration")

    if lowered.startswith(".yarn/") or "/.yarn/" in lowered:
        reasons.append("Yarn runtime, plugin, cache, or configuration")

    if lowered.startswith(".husky/") or "/.husky/" in lowered:
        reasons.append("Git hook framework")

    if (lowered.startswith("scripts/") or "/scripts/" in lowered) and (
        entry_type == "regular"
        or any(basename_lower.endswith(suffix) for suffix in SCRIPT_SUFFIXES)
    ):
        reasons.append("script entrypoint candidate")

    return sorted(set(reasons))


def candidate_priority(reasons: list[str]) -> str:
    automatic_markers = (
        "agent, IDE, or automation configuration",
        "CI workflow",
        "development-container configuration",
        "editor workspace configuration",
        "Git executable configuration",
        "Git hook",
        "Husky",
    )
    if any(marker in reason for marker in automatic_markers for reason in reasons):
        return "automatic_or_early"

    entrypoint_markers = (
        "execution-oriented",
        "script entrypoint",
        "shell or command script",
    )
    if any(marker in reason for marker in entrypoint_markers for reason in reasons):
        return "common_entrypoint"

    return "configuration_or_manifest"


def bulk_tree_reason(relative_path: str) -> str | None:
    normalized = relative_path.replace(os.sep, "/").casefold().rstrip("/")
    basename = normalized.rsplit("/", 1)[-1]
    if basename in BULK_DIRECTORY_NAMES:
        return BULK_DIRECTORY_NAMES[basename]
    return next(
        (
            reason
            for suffix, reason in BULK_PATH_SUFFIXES.items()
            if normalized.endswith(suffix)
        ),
        None,
    )


def matching_suffix(name: str, suffixes: set[str]) -> str | None:
    lowered = name.casefold()
    return next(
        (
            suffix
            for suffix in sorted(suffixes, key=len, reverse=True)
            if lowered.endswith(suffix)
        ),
        None,
    )


def read_small_regular_file(path: str, expected_size: int) -> bytes | None:
    if expected_size > MAX_GIT_POINTER_BYTES:
        return None

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_size > MAX_GIT_POINTER_BYTES
        ):
            return None
        return os.read(descriptor, MAX_GIT_POINTER_BYTES + 1)
    finally:
        os.close(descriptor)


def git_pointer(path: str, relative_path: str, file_size: int) -> dict[str, str] | None:
    normalized = relative_path.replace(os.sep, "/")
    basename = normalized.rsplit("/", 1)[-1]
    pointer_kind: str | None = None

    if basename == ".git":
        pointer_kind = "worktree_git_file"
    elif basename == "gitdir" and "/.git/worktrees/" in f"/{normalized}":
        pointer_kind = "linked_worktree_pointer"
    elif basename == "commondir" and "/.git/" in f"/{normalized}":
        pointer_kind = "common_git_directory_pointer"

    if pointer_kind is None:
        return None

    try:
        raw = read_small_regular_file(path, file_size)
    except OSError as error:
        return {
            "path": relative_path,
            "kind": pointer_kind,
            "error": f"{type(error).__name__}: {error}",
        }

    if raw is None:
        return None

    text = raw.decode("utf-8", errors="replace").strip()
    if pointer_kind == "worktree_git_file":
        if not text.lower().startswith("gitdir:"):
            return {
                "path": relative_path,
                "kind": pointer_kind,
                "error": "missing gitdir prefix",
            }
        target = text.split(":", 1)[1].strip()
    else:
        target = text.splitlines()[0].strip() if text else ""

    return {
        "path": relative_path,
        "kind": pointer_kind,
        "target": target,
        "lexical_target": lexical_target(path, target) if target else "",
    }


def list_extended_attributes(path: str) -> tuple[list[str] | None, str | None]:
    if not hasattr(os, "listxattr"):
        return None, None
    try:
        return sorted(os.listxattr(path, follow_symlinks=False)), None
    except (NotImplementedError, TypeError):
        return None, None
    except OSError as error:
        return None, f"{type(error).__name__}: {error}"


class DetailCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.details: dict[str, list[dict[str, Any]]] = {}
        self.omitted: Counter[str] = Counter()

    def add(self, category: str, item: dict[str, Any]) -> None:
        bucket = self.details.setdefault(category, [])
        if len(bucket) < self.limit:
            bucket.append(item)
        else:
            self.omitted[category] += 1

    def finish(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
        for items in self.details.values():
            items.sort(
                key=lambda item: (
                    str(item.get("path", "")),
                    json.dumps(item, sort_keys=True),
                )
            )
        return dict(sorted(self.details.items())), dict(sorted(self.omitted.items()))


def scan_root(
    argument: str, max_entries: int, max_findings: int, include_bulk: bool
) -> dict[str, Any]:
    root = os.path.abspath(os.path.expanduser(argument))
    collector = DetailCollector(max_findings)
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "argument": argument,
        "root": root,
        "max_entries": max_entries,
        "max_findings_per_category": max_findings,
        "includes_bulk_trees": include_bulk,
        "truncated": False,
    }

    try:
        root_stat = os.lstat(root)
    except OSError as error:
        result["fatal_error"] = f"{type(error).__name__}: {error}"
        return result

    root_type = mode_type(root_stat.st_mode)
    result["root_type"] = root_type
    result["root_mode"] = octal_mode(root_stat.st_mode)
    result["root_device"] = root_stat.st_dev
    result["platform_metadata_capabilities"] = {
        "extended_attribute_names": hasattr(os, "listxattr"),
        "numeric_file_flags": hasattr(root_stat, "st_flags"),
        "acl_entries": False,
    }

    if root_type == "symlink":
        try:
            target = os.readlink(root)
            result["root_symlink"] = {
                "target": target,
                "lexical_target": lexical_target(root, target),
            }
        except OSError as error:
            result["root_symlink"] = {"error": f"{type(error).__name__}: {error}"}
        result["fatal_error"] = "root is a symlink; not followed"
        return result

    if root_type != "directory":
        result["fatal_error"] = "root is not a directory"
        return result

    root_device = root_stat.st_dev
    visited_directories: set[tuple[int, int]] = set()
    stack: list[tuple[str, str]] = [(root, "")]
    entries_seen = 0

    while stack:
        directory, relative_directory = stack.pop()
        try:
            directory_stat = os.lstat(directory)
        except OSError as error:
            errors.append(
                {
                    "path": relative_directory or ".",
                    "operation": "lstat directory",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue

        identity = (directory_stat.st_dev, directory_stat.st_ino)
        if identity in visited_directories:
            collector.add(
                "directory_cycles",
                {
                    "path": relative_directory or ".",
                    "device": identity[0],
                    "inode": identity[1],
                },
            )
            continue
        visited_directories.add(identity)

        if directory_stat.st_dev != root_device:
            collector.add(
                "filesystem_boundaries",
                {
                    "path": relative_directory or ".",
                    "device": directory_stat.st_dev,
                    "root_device": root_device,
                },
            )
            continue

        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            errors.append(
                {
                    "path": relative_directory or ".",
                    "operation": "list directory",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue

        entries.sort(key=lambda entry: os.fsencode(entry.name))

        casefold_names: dict[str, list[str]] = {}
        normalized_names: dict[str, list[str]] = {}
        for entry in entries:
            casefold_names.setdefault(entry.name.casefold(), []).append(entry.name)
            normalized_names.setdefault(
                unicodedata.normalize("NFC", entry.name), []
            ).append(entry.name)

        for names in casefold_names.values():
            if len(names) > 1:
                collector.add(
                    "case_collisions",
                    {"path": relative_directory or ".", "names": sorted(names)},
                )

        for names in normalized_names.values():
            if len(names) > 1:
                collector.add(
                    "unicode_normalization_collisions",
                    {"path": relative_directory or ".", "names": sorted(names)},
                )

        child_directories: list[tuple[str, str]] = []
        for entry in entries:
            if entries_seen >= max_entries:
                result["truncated"] = True
                stack.clear()
                break

            entries_seen += 1
            relative_path = (
                os.path.join(relative_directory, entry.name)
                if relative_directory
                else entry.name
            )
            full_path = os.path.join(directory, entry.name)

            try:
                entry_stat = os.lstat(full_path)
            except OSError as error:
                errors.append(
                    {
                        "path": relative_path,
                        "operation": "lstat entry",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                continue

            entry_type = mode_type(entry_stat.st_mode)
            counts[entry_type] += 1

            name_flags: list[str] = []
            if has_control_character(entry.name):
                name_flags.append("control_character")
            if entry.name != entry.name.strip():
                name_flags.append("leading_or_trailing_whitespace")
            if unicodedata.normalize("NFC", entry.name) != entry.name:
                name_flags.append("non_nfc_unicode")
            if any(
                ord(character) in BIDI_CONTROL_CODEPOINTS for character in entry.name
            ):
                name_flags.append("bidirectional_control")
            if name_flags:
                collector.add(
                    "suspicious_names", {"path": relative_path, "flags": name_flags}
                )

            special_bits = stat.S_IMODE(entry_stat.st_mode) & (
                stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
            )
            if special_bits:
                collector.add(
                    "special_permissions",
                    {
                        "path": relative_path,
                        "type": entry_type,
                        "mode": octal_mode(entry_stat.st_mode),
                    },
                )

            file_flags = getattr(entry_stat, "st_flags", 0)
            if file_flags:
                flag_names, unknown_flag_bits = decode_file_flags(file_flags)
                collector.add(
                    "platform_file_flags",
                    {
                        "path": relative_path,
                        "raw": file_flags,
                        "names": flag_names,
                        "unknown_bits": unknown_flag_bits,
                        "security_relevant": bool(
                            SECURITY_RELEVANT_FILE_FLAGS.intersection(flag_names)
                        ),
                    },
                )

            extended_attributes, xattr_error = list_extended_attributes(full_path)
            if extended_attributes:
                collector.add(
                    "extended_attributes",
                    {
                        "path": relative_path,
                        "names": extended_attributes,
                    },
                )
            if xattr_error:
                collector.add(
                    "metadata_errors",
                    {
                        "path": relative_path,
                        "operation": "list extended attributes",
                        "error": xattr_error,
                    },
                )

            reasons = candidate_reasons(relative_path, entry_type)
            if reasons:
                collector.add(
                    "entrypoint_candidates",
                    {
                        "path": relative_path,
                        "type": entry_type,
                        "mode": octal_mode(entry_stat.st_mode),
                        "priority": candidate_priority(reasons),
                        "reasons": reasons,
                    },
                )

            if entry_type == "symlink":
                try:
                    target = os.readlink(full_path)
                    target_path = lexical_target(full_path, target)
                    collector.add(
                        "symlinks",
                        {
                            "path": relative_path,
                            "target": target,
                            "lexical_target": target_path,
                            "target_scope": "inside_root"
                            if is_within(target_path, root)
                            else "outside_root",
                        },
                    )
                except OSError as error:
                    collector.add(
                        "symlinks",
                        {
                            "path": relative_path,
                            "error": f"{type(error).__name__}: {error}",
                        },
                    )
                continue

            if entry_type == "directory":
                if entry.name.casefold() in TEST_DIRECTORY_NAMES:
                    collector.add(
                        "test_trees",
                        {
                            "path": relative_path,
                            "reason": "test, benchmark, example, or discovery-time code",
                        },
                    )
                if entry.name.casefold() in {".git", ".hg", ".jj", ".svn"}:
                    collector.add(
                        "vcs_roots",
                        {
                            "path": relative_path,
                            "type": entry.name.casefold(),
                            "device": entry_stat.st_dev,
                            "inode": entry_stat.st_ino,
                        },
                    )
                relative_parts = (
                    relative_path.replace(os.sep, "/").casefold().split("/")
                )
                if relative_parts[-1] == "objects" and ".git" in relative_parts[:-1]:
                    collector.add(
                        "opaque_vcs_storage",
                        {
                            "path": relative_path,
                            "reason": "Git object storage is not expanded by the inventory helper",
                        },
                    )
                    continue
                bulk_reason = bulk_tree_reason(relative_path)
                if bulk_reason is not None and not include_bulk:
                    collector.add(
                        "bulk_trees",
                        {
                            "path": relative_path,
                            "reason": bulk_reason,
                            "expanded": False,
                        },
                    )
                    continue
                if entry_stat.st_dev != root_device:
                    collector.add(
                        "filesystem_boundaries",
                        {
                            "path": relative_path,
                            "device": entry_stat.st_dev,
                            "root_device": root_device,
                        },
                    )
                else:
                    child_directories.append((full_path, relative_path))
                continue

            if entry_type != "regular":
                collector.add(
                    "special_files",
                    {
                        "path": relative_path,
                        "type": entry_type,
                        "mode": octal_mode(entry_stat.st_mode),
                    },
                )
                continue

            collector.add(
                "regular_files",
                {
                    "path": relative_path,
                    "mode": octal_mode(entry_stat.st_mode),
                    "size": entry_stat.st_size,
                },
            )

            if entry_stat.st_nlink > 1:
                collector.add(
                    "hardlinked_files",
                    {
                        "path": relative_path,
                        "links": entry_stat.st_nlink,
                        "device": entry_stat.st_dev,
                        "inode": entry_stat.st_ino,
                    },
                )

            if entry_stat.st_mode & 0o111:
                collector.add(
                    "executable_files",
                    {
                        "path": relative_path,
                        "mode": octal_mode(entry_stat.st_mode),
                        "size": entry_stat.st_size,
                    },
                )

            archive_suffix = matching_suffix(entry.name, ARCHIVE_SUFFIXES)
            if archive_suffix:
                collector.add(
                    "archive_candidates",
                    {
                        "path": relative_path,
                        "suffix": archive_suffix,
                        "size": entry_stat.st_size,
                    },
                )

            binary_suffix = matching_suffix(entry.name, BINARY_SUFFIXES)
            if binary_suffix:
                collector.add(
                    "binary_candidates",
                    {
                        "path": relative_path,
                        "suffix": binary_suffix,
                        "size": entry_stat.st_size,
                        "mode": octal_mode(entry_stat.st_mode),
                    },
                )

            pointer = git_pointer(full_path, relative_path, entry_stat.st_size)
            if pointer is not None:
                collector.add("git_pointers", pointer)

        stack.extend(reversed(child_directories))

    details, omitted = collector.finish()
    result["entries_seen"] = entries_seen
    result["counts"] = dict(sorted(counts.items()))
    result["details"] = details
    result["omitted_detail_counts"] = omitted
    result["errors"] = sorted(
        errors, key=lambda item: (item["path"], item["operation"])
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    directories = args.directories or ["."]
    roots = [
        scan_root(
            directory,
            args.max_entries,
            args.max_findings,
            args.include_bulk,
        )
        for directory in directories
    ]
    payload = {
        "schema_version": 2,
        "scanner_version": VERSION,
        "invariants": {
            "follows_symlinks": False,
            "crosses_filesystems": False,
            "writes_files": False,
            "executes_target_code": False,
            "imports_target_code": False,
        },
        "roots": roots,
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if any("fatal_error" in root for root in roots) else 0


if __name__ == "__main__":
    raise SystemExit(main())
