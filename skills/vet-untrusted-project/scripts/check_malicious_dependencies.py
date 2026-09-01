#!/usr/bin/env python3
"""Statically inventory locked dependencies and query known-malware feeds.

The helper parses target files as inert data, follows no symlinks, invokes no
package manager, sends only validated package coordinates to hard-coded public
advisory APIs, and emits JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomllib

VERSION = "1.0.0"
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_PACKAGES = 10_000
DEFAULT_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
OSV_BATCH_SIZE = 500
GITHUB_BATCH_SIZE = 100
GITHUB_MAX_PACKAGES = 4_000
GITHUB_MAX_AFFECTS_QUERY_BYTES = 6_000
MAX_REPORTED_EXAMPLES = 50

OSV_QUERY_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL_PREFIX = "https://api.osv.dev/v1/vulns/"
GITHUB_ADVISORIES_URL = "https://api.github.com/advisories"
ALLOWED_API_HOSTS = {"api.osv.dev", "api.github.com"}

BULK_DIRECTORIES = {
    ".build",
    ".bundle",
    ".cache",
    ".git",
    ".gradle",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".terraform",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "vendor",
    "venv",
}

EXACT_FILES = {
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "gradle.lockfile",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "packages.lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}

NAME_PATTERN = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@._/+~:-]{0,254}")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!~:-]{0,127}")
REQUIREMENT_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:\[[^\]]+\])?\s*(?:==\s*([^\s;]+))?"
)
GEM_SPEC_PATTERN = re.compile(r"^ {4}([^\s(]+) \(([^),\s]+)")
GRADLE_LOCK_PATTERN = re.compile(r"^([^:#\s]+):([^:#\s]+):([^=\s]+)=")
GO_REQUIRE_PATTERN = re.compile(r"^\s*([^\s]+)\s+(v[^\s]+)")

PUBLIC_SOURCE_HOSTS = {
    "npm": {"registry.npmjs.org", "registry.yarnpkg.com"},
    "PyPI": {"pypi.org", "files.pythonhosted.org"},
    "crates.io": {"crates.io", "index.crates.io"},
    "Packagist": {"packagist.org", "repo.packagist.org"},
    "RubyGems": {"rubygems.org"},
    "NuGet": {"api.nuget.org", "www.nuget.org"},
    "Maven": {"repo.maven.apache.org", "repo1.maven.org"},
}

GITHUB_ECOSYSTEMS = {
    "npm": "npm",
    "PyPI": "pip",
    "Maven": "maven",
    "NuGet": "nuget",
    "Packagist": "composer",
    "Go": "go",
    "crates.io": "rust",
    "RubyGems": "rubygems",
    "GitHub Actions": "actions",
    "Pub": "pub",
    "SwiftURL": "swift",
}


@dataclass(frozen=True)
class Coordinate:
    ecosystem: str
    name: str
    version: str | None
    public_source: bool
    source: str
    path: str

    def query_key(self) -> tuple[str, str, str | None]:
        return (self.ecosystem, self.name, self.version)

    def as_dict(self) -> dict[str, object]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "version": self.version,
            "public_source": self.public_source,
            "source": self.source,
            "path": self.path,
        }


@dataclass
class Inventory:
    coordinates: list[Coordinate]
    files: list[dict[str, object]]
    errors: list[dict[str, str]]
    truncated_files: bool = False
    truncated_bytes: bool = False


JsonRequest = Callable[[str, str, object | None, dict[str, str], int], object]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check preexisting dependency metadata against known-malware feeds without executing target code."
    )
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Parse dependency files without making network requests.",
    )
    parser.add_argument(
        "--include-unverified-public-identities",
        action="store_true",
        help="Query valid package names whose public registry source is not proven by the lockfile; use only for known-public targets.",
    )
    parser.add_argument("--no-osv", action="store_true")
    parser.add_argument("--no-github", action="store_true")
    parser.add_argument("--max-files", type=positive_int, default=DEFAULT_MAX_FILES)
    parser.add_argument(
        "--max-file-bytes", type=positive_int, default=DEFAULT_MAX_FILE_BYTES
    )
    parser.add_argument(
        "--max-total-bytes", type=positive_int, default=DEFAULT_MAX_TOTAL_BYTES
    )
    parser.add_argument(
        "--max-packages", type=positive_int, default=DEFAULT_MAX_PACKAGES
    )
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def normalized_name(ecosystem: str, name: str) -> str | None:
    stripped = name.strip()
    if not NAME_PATTERN.fullmatch(stripped):
        return None
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", stripped).lower()
    if ecosystem in {"npm", "crates.io"}:
        return stripped.lower()
    return stripped


def normalized_version(version: object) -> str | None:
    if not isinstance(version, str):
        return None
    stripped = version.strip()
    if not VERSION_PATTERN.fullmatch(stripped):
        return None
    return stripped


def source_is_public(ecosystem: str, source: object) -> bool:
    if not isinstance(source, str) or not source:
        return False
    if ecosystem == "crates.io" and source in {
        "registry+https://github.com/rust-lang/crates.io-index",
        "sparse+https://index.crates.io/",
    }:
        return True
    try:
        host = urllib.parse.urlsplit(source).hostname
    except ValueError:
        return False
    return host in PUBLIC_SOURCE_HOSTS.get(ecosystem, set())


def sanitized_source(source: object) -> str:
    if not isinstance(source, str):
        return ""
    stripped = "".join(character for character in source.strip() if character >= " ")
    try:
        parsed = urllib.parse.urlsplit(stripped)
    except ValueError:
        return stripped[:500]
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return stripped[:500]
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{parsed.hostname}{port}", parsed.path[:400], "", "")
    )


def make_coordinate(
    ecosystem: str,
    name: object,
    version: object,
    *,
    source: object,
    path: str,
    public_source: bool | None = None,
) -> Coordinate | None:
    if not isinstance(name, str):
        return None
    safe_name = normalized_name(ecosystem, name)
    if safe_name is None:
        return None
    safe_version = normalized_version(version)
    source_text = source if isinstance(source, str) else ""
    return Coordinate(
        ecosystem=ecosystem,
        name=safe_name,
        version=safe_version,
        public_source=(
            source_is_public(ecosystem, source_text)
            if public_source is None
            else public_source
        ),
        source=sanitized_source(source_text),
        path=path,
    )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("top-level JSON value is not an object")
    return value


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        value = tomllib.load(file)
    if not isinstance(value, dict):
        raise TypeError("top-level TOML value is not a table")
    return value


def npm_name_from_package_path(package_path: str, entry: dict[str, Any]) -> str | None:
    if isinstance(entry.get("name"), str):
        return entry["name"]
    marker = "node_modules/"
    if marker not in package_path:
        return None
    suffix = package_path.rsplit(marker, 1)[1]
    parts = suffix.split("/")
    if suffix.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def parse_package_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_json(path)
    coordinates: list[Coordinate] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for package_path, raw_entry in packages.items():
            if not isinstance(package_path, str) or not isinstance(raw_entry, dict):
                continue
            if raw_entry.get("link") is True:
                continue
            name = npm_name_from_package_path(package_path, raw_entry)
            coordinate = make_coordinate(
                "npm",
                name,
                raw_entry.get("version"),
                source=raw_entry.get("resolved", ""),
                path=relative,
            )
            if coordinate:
                coordinates.append(coordinate)
        return coordinates

    def visit_dependencies(dependencies: object) -> None:
        if not isinstance(dependencies, dict):
            return
        for name, raw_entry in dependencies.items():
            if not isinstance(raw_entry, dict):
                continue
            coordinate = make_coordinate(
                "npm",
                name,
                raw_entry.get("version"),
                source=raw_entry.get("resolved", ""),
                path=relative,
            )
            if coordinate:
                coordinates.append(coordinate)
            visit_dependencies(raw_entry.get("dependencies"))

    visit_dependencies(data.get("dependencies"))
    return coordinates


def package_json_version(specifier: object) -> tuple[str | None, str | None]:
    if not isinstance(specifier, str):
        return (None, None)
    if specifier.startswith("npm:"):
        alias = specifier[4:]
        separator = alias.rfind("@")
        if separator > 0:
            return (alias[:separator], normalized_version(alias[separator + 1 :]))
        return (alias, None)
    return (None, normalized_version(specifier))


def parse_package_json(path: Path, relative: str) -> list[Coordinate]:
    data = read_json(path)
    coordinates: list[Coordinate] = []
    for section in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
    ):
        dependencies = data.get(section)
        if not isinstance(dependencies, dict):
            continue
        for declared_name, specifier in dependencies.items():
            alias_name, version = package_json_version(specifier)
            coordinate = make_coordinate(
                "npm",
                alias_name or declared_name,
                version,
                source="manifest-only",
                path=relative,
                public_source=False,
            )
            if coordinate:
                coordinates.append(coordinate)
    return coordinates


def parse_cargo_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_toml(path)
    coordinates: list[Coordinate] = []
    packages = data.get("package")
    if not isinstance(packages, list):
        return coordinates
    for package in packages:
        if not isinstance(package, dict):
            continue
        coordinate = make_coordinate(
            "crates.io",
            package.get("name"),
            package.get("version"),
            source=package.get("source", ""),
            path=relative,
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def parse_python_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_toml(path)
    coordinates: list[Coordinate] = []
    packages = data.get("package")
    if not isinstance(packages, list):
        return coordinates
    for package in packages:
        if not isinstance(package, dict):
            continue
        source = package.get("source")
        source_url = ""
        if isinstance(source, dict):
            for key in ("registry", "url"):
                if isinstance(source.get(key), str):
                    source_url = source[key]
                    break
        coordinate = make_coordinate(
            "PyPI",
            package.get("name"),
            package.get("version"),
            source=source_url or "lockfile-source-unspecified",
            path=relative,
            public_source=source_is_public("PyPI", source_url),
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def parse_pipfile_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_json(path)
    meta = data.get("_meta")
    sources = meta.get("sources") if isinstance(meta, dict) else None
    source_urls = (
        [
            source.get("url")
            for source in sources
            if isinstance(source, dict) and isinstance(source.get("url"), str)
        ]
        if isinstance(sources, list)
        else []
    )
    public = bool(source_urls) and all(
        source_is_public("PyPI", url) for url in source_urls
    )
    source = ",".join(source_urls) if source_urls else "lockfile-source-unspecified"
    coordinates: list[Coordinate] = []
    for section in ("default", "develop"):
        packages = data.get(section)
        if not isinstance(packages, dict):
            continue
        for name, package in packages.items():
            version = package.get("version") if isinstance(package, dict) else None
            if isinstance(version, str) and version.startswith("=="):
                version = version[2:]
            coordinate = make_coordinate(
                "PyPI",
                name,
                version,
                source=source,
                path=relative,
                public_source=public,
            )
            if coordinate:
                coordinates.append(coordinate)
    return coordinates


def parse_composer_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_json(path)
    coordinates: list[Coordinate] = []
    for section in ("packages", "packages-dev"):
        packages = data.get(section)
        if not isinstance(packages, list):
            continue
        for package in packages:
            if not isinstance(package, dict):
                continue
            dist = package.get("dist")
            source = package.get("source")
            source_url = ""
            for candidate in (dist, source):
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("url"), str
                ):
                    source_url = candidate["url"]
                    if source_is_public("Packagist", source_url):
                        break
            coordinate = make_coordinate(
                "Packagist",
                package.get("name"),
                package.get("version"),
                source=source_url or "lockfile-source-unspecified",
                path=relative,
            )
            if coordinate:
                coordinates.append(coordinate)
    return coordinates


def parse_nuget_lock(path: Path, relative: str) -> list[Coordinate]:
    data = read_json(path)
    coordinates: list[Coordinate] = []
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, dict):
        return coordinates
    for framework in dependencies.values():
        if not isinstance(framework, dict):
            continue
        for name, package in framework.items():
            version = package.get("resolved") if isinstance(package, dict) else None
            coordinate = make_coordinate(
                "NuGet",
                name,
                version,
                source="lockfile-source-unspecified",
                path=relative,
                public_source=False,
            )
            if coordinate:
                coordinates.append(coordinate)
    return coordinates


def parse_go_file(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_require = False
    for line in lines:
        stripped = line.split("//", 1)[0].strip()
        if path.name == "go.mod":
            if stripped == "require (":
                in_require = True
                continue
            if in_require and stripped == ")":
                in_require = False
                continue
            if not in_require and stripped.startswith("require "):
                stripped = stripped.removeprefix("require ").strip()
            elif not in_require:
                continue
        match = GO_REQUIRE_PATTERN.match(stripped)
        if not match:
            continue
        name, version = match.groups()
        if version.endswith("/go.mod"):
            version = version.removesuffix("/go.mod")
        coordinate = make_coordinate(
            "Go",
            name,
            version,
            source="module-source-unspecified",
            path=relative,
            public_source=False,
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def parse_gemfile_lock(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    in_gem = False
    official_remote = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line == "GEM":
            in_gem = True
            official_remote = False
            continue
        if in_gem and line and not line.startswith(" "):
            in_gem = False
        if not in_gem:
            continue
        stripped = line.strip()
        if stripped.startswith("remote:"):
            remote = stripped.removeprefix("remote:").strip()
            official_remote = source_is_public("RubyGems", remote)
            continue
        match = GEM_SPEC_PATTERN.match(line)
        if not match:
            continue
        coordinate = make_coordinate(
            "RubyGems",
            match.group(1),
            match.group(2),
            source="https://rubygems.org"
            if official_remote
            else "lockfile-source-unspecified",
            path=relative,
            public_source=official_remote,
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def parse_gradle_lock(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = GRADLE_LOCK_PATTERN.match(line)
        if not match:
            continue
        coordinate = make_coordinate(
            "Maven",
            f"{match.group(1)}:{match.group(2)}",
            match.group(3),
            source="lockfile-source-unspecified",
            path=relative,
            public_source=False,
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def yarn_descriptor_name(descriptor: str) -> str | None:
    first = descriptor.split(",", 1)[0].strip().strip("\"'")
    if first.startswith("@"):
        separator = first.find("@", 1)
    else:
        separator = first.find("@")
    return first[:separator] if separator > 0 else None


def parse_yarn_lock(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    name: str | None = None
    version: str | None = None
    resolved = ""

    def flush() -> None:
        nonlocal name, version, resolved
        if name:
            coordinate = make_coordinate(
                "npm", name, version, source=resolved, path=relative
            )
            if coordinate:
                coordinates.append(coordinate)
        name = None
        version = None
        resolved = ""

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            flush()
            name = yarn_descriptor_name(line[:-1])
            continue
        stripped = line.strip()
        if stripped.startswith("version "):
            version = stripped.removeprefix("version ").strip().strip("\"'")
        elif stripped.startswith("version:"):
            version = stripped.removeprefix("version:").strip().strip("\"'")
        elif stripped.startswith("resolved "):
            resolved = (
                stripped.removeprefix("resolved ").strip().strip("\"'").split("#", 1)[0]
            )
        elif stripped.startswith("resolution:") and "@npm:" in stripped:
            resolved = "npm-resolution-unverified"
    flush()
    return coordinates


def pnpm_key_coordinate(key: str) -> tuple[str, str] | None:
    normalized = key.strip().strip("\"'")
    if normalized.startswith(("file:", "link:", "git+", "http:", "https:")):
        return None
    normalized = normalized.split("(", 1)[0]
    if normalized.startswith("/"):
        parts = normalized.split("/")
        if len(parts) >= 4 and parts[1].startswith("@"):
            return (f"{parts[1]}/{parts[2]}", parts[3])
        if len(parts) >= 3 and "@" not in parts[1]:
            return (parts[1], parts[2])
        normalized = normalized[1:]
    separator = normalized.rfind("@")
    if separator > 0:
        return (normalized[:separator], normalized[separator + 1 :])
    return None


def parse_pnpm_lock(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    section = ""
    current: tuple[str, str] | None = None
    current_source = "lockfile-source-unspecified"

    def flush() -> None:
        nonlocal current, current_source
        if current:
            coordinate = make_coordinate(
                "npm",
                current[0],
                current[1],
                source=current_source,
                path=relative,
            )
            if coordinate:
                coordinates.append(coordinate)
        current = None
        current_source = "lockfile-source-unspecified"

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            flush()
            section = line.rstrip()[:-1].strip().strip("\"'")
            continue
        if section != "packages":
            continue
        if (
            line.startswith("  ")
            and not line.startswith("    ")
            and line.rstrip().endswith(":")
        ):
            flush()
            current = pnpm_key_coordinate(line.strip()[:-1])
            continue
        if not current:
            continue
        match = re.search(r"\btarball:\s*['\"]?([^,'\"}\s]+)", line)
        if match:
            current_source = match.group(1)
    flush()
    return coordinates


def parse_requirements(path: Path, relative: str) -> list[Coordinate]:
    coordinates: list[Coordinate] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "http:", "https:", "git+")):
            continue
        match = REQUIREMENT_PATTERN.match(line)
        if not match:
            continue
        coordinate = make_coordinate(
            "PyPI",
            match.group(1),
            match.group(2),
            source="manifest-only",
            path=relative,
            public_source=False,
        )
        if coordinate:
            coordinates.append(coordinate)
    return coordinates


def parser_for(path: Path) -> Callable[[Path, str], list[Coordinate]] | None:
    name = path.name.lower()
    if name in {"package-lock.json", "npm-shrinkwrap.json"}:
        return parse_package_lock
    if name == "package.json":
        return parse_package_json
    if name == "cargo.lock":
        return parse_cargo_lock
    if name in {"poetry.lock", "uv.lock"}:
        return parse_python_lock
    if name == "pipfile.lock":
        return parse_pipfile_lock
    if name == "composer.lock":
        return parse_composer_lock
    if name == "packages.lock.json":
        return parse_nuget_lock
    if name in {"go.mod", "go.sum"}:
        return parse_go_file
    if name == "gemfile.lock":
        return parse_gemfile_lock
    if name == "gradle.lockfile":
        return parse_gradle_lock
    if name == "yarn.lock":
        return parse_yarn_lock
    if name == "pnpm-lock.yaml":
        return parse_pnpm_lock
    if name.startswith("requirements") and path.suffix.lower() in {".txt", ".in"}:
        return parse_requirements
    return None


def is_dependency_file(path: Path) -> bool:
    name = path.name.lower()
    return name in EXACT_FILES or (
        name.startswith("requirements") and path.suffix.lower() in {".txt", ".in"}
    )


def discover_dependency_files(
    root: Path, *, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> tuple[list[tuple[Path, str, int]], bool, bool]:
    root_stat = root.stat(follow_symlinks=False)
    root_device = root_stat.st_dev
    files: list[tuple[Path, str, int]] = []
    total_bytes = 0
    truncated_files = False
    truncated_bytes = False

    for directory, directories, names in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        kept_directories = []
        for name in directories:
            child = directory_path / name
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if name.lower() in BULK_DIRECTORIES:
                continue
            if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_dev != root_device:
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in names:
            path = directory_path / name
            if not is_dependency_file(path):
                continue
            try:
                file_stat = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_dev != root_device:
                continue
            if file_stat.st_size > max_file_bytes:
                continue
            if len(files) >= max_files:
                truncated_files = True
                return files, truncated_files, truncated_bytes
            if total_bytes + file_stat.st_size > max_total_bytes:
                truncated_bytes = True
                return files, truncated_files, truncated_bytes
            relative = str(path.relative_to(root))
            files.append((path, relative, file_stat.st_size))
            total_bytes += file_stat.st_size
    return files, truncated_files, truncated_bytes


def inventory_dependencies(
    root: Path, *, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> Inventory:
    files, truncated_files, truncated_bytes = discover_dependency_files(
        root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    coordinates: list[Coordinate] = []
    file_records: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for path, relative, size in files:
        parser = parser_for(path)
        if parser is None:
            continue
        try:
            parsed = parser(path, relative)
            coordinates.extend(parsed)
            file_records.append(
                {"path": relative, "bytes": size, "coordinates": len(parsed)}
            )
        except (
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
        ) as error:
            errors.append({"path": relative, "error": str(error)[:500]})
    return Inventory(
        coordinates=coordinates,
        files=file_records,
        errors=errors,
        truncated_files=truncated_files,
        truncated_bytes=truncated_bytes,
    )


def deduplicate_coordinates(coordinates: Iterable[Coordinate]) -> list[Coordinate]:
    best: dict[tuple[str, str, str | None], Coordinate] = {}
    for coordinate in coordinates:
        key = coordinate.query_key()
        current = best.get(key)
        if current is None or (coordinate.public_source and not current.public_source):
            best[key] = coordinate
    return sorted(
        best.values(),
        key=lambda coordinate: (
            coordinate.ecosystem,
            coordinate.name,
            coordinate.version or "",
            not coordinate.public_source,
            coordinate.path,
        ),
    )


def choose_query_candidates(
    coordinates: Iterable[Coordinate], *, include_unverified: bool, max_packages: int
) -> tuple[list[Coordinate], list[Coordinate], bool]:
    unique = deduplicate_coordinates(coordinates)
    candidates = [
        coordinate
        for coordinate in unique
        if coordinate.public_source or include_unverified
    ]
    candidates.sort(
        key=lambda coordinate: (
            not coordinate.public_source,
            coordinate.version is None,
            coordinate.ecosystem,
            coordinate.name,
            coordinate.version or "",
        )
    )
    truncated = len(candidates) > max_packages
    selected = candidates[:max_packages]
    selected_keys = {coordinate.query_key() for coordinate in selected}
    skipped = [
        coordinate
        for coordinate in unique
        if coordinate.query_key() not in selected_keys
    ]
    return selected, skipped, truncated


def request_json(
    method: str,
    url: str,
    payload: object | None,
    headers: dict[str, str],
    timeout: int,
) -> object:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_API_HOSTS:
        raise ValueError(f"disallowed advisory endpoint: {url}")
    body = json.dumps(payload).encode() if payload is not None else None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "vet-untrusted-project-malware-check/1",
        **headers,
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname not in ALLOWED_API_HOSTS:
            raise ValueError(
                f"advisory endpoint redirected outside allowlist: {response.geturl()}"
            )
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError("advisory response exceeds byte limit")
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("advisory response exceeds byte limit")
    return json.loads(data)


def chunks(values: list[Coordinate], size: int) -> Iterable[list[Coordinate]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def github_chunks(values: list[Coordinate]) -> Iterable[list[Coordinate]]:
    batch: list[Coordinate] = []
    encoded_bytes = 0
    for coordinate in values:
        affects = (
            f"{coordinate.name}@{coordinate.version}"
            if coordinate.version
            else coordinate.name
        )
        coordinate_bytes = len(urllib.parse.quote(affects, safe="")) + 3
        if batch and (
            len(batch) >= GITHUB_BATCH_SIZE
            or encoded_bytes + coordinate_bytes > GITHUB_MAX_AFFECTS_QUERY_BYTES
        ):
            yield batch
            batch = []
            encoded_bytes = 0
        batch.append(coordinate)
        encoded_bytes += coordinate_bytes
    if batch:
        yield batch


def coordinate_context(
    coordinates: Iterable[Coordinate], ecosystem: str, name: str
) -> dict[str, object]:
    matches = [
        coordinate
        for coordinate in coordinates
        if coordinate.ecosystem == ecosystem and coordinate.name == name
    ]
    return {
        "ecosystem": ecosystem,
        "name": name,
        "queried_versions": sorted(
            {coordinate.version for coordinate in matches if coordinate.version}
        ),
        "paths": sorted({coordinate.path for coordinate in matches}),
        "source_verified": any(coordinate.public_source for coordinate in matches),
        "needs_version_confirmation": any(
            coordinate.version is None for coordinate in matches
        ),
    }


def query_osv(
    coordinates: list[Coordinate],
    *,
    timeout: int,
    requester: JsonRequest = request_json,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    malware_ids: dict[str, list[Coordinate]] = defaultdict(list)
    ordinary_advisories = 0
    errors: list[str] = []
    queries_completed = 0
    withdrawn_records = 0

    for batch in chunks(coordinates, OSV_BATCH_SIZE):
        queries = []
        for coordinate in batch:
            query: dict[str, object] = {
                "package": {
                    "ecosystem": coordinate.ecosystem,
                    "name": coordinate.name,
                }
            }
            if coordinate.version:
                query["version"] = coordinate.version
            queries.append(query)
        try:
            response = requester(
                "POST", OSV_QUERY_BATCH_URL, {"queries": queries}, {}, timeout
            )
            results = response.get("results") if isinstance(response, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise ValueError("OSV batch response shape does not match request")
            queries_completed += len(batch)
            for coordinate, result in zip(batch, results, strict=True):
                if isinstance(result, dict) and result.get("next_page_token"):
                    errors.append(
                        f"OSV result for {coordinate.ecosystem}/{coordinate.name} was paginated; additional records were not checked"
                    )
                advisories = result.get("vulns") if isinstance(result, dict) else None
                if not isinstance(advisories, list):
                    continue
                for advisory in advisories:
                    advisory_id = (
                        advisory.get("id") if isinstance(advisory, dict) else None
                    )
                    if not isinstance(advisory_id, str):
                        continue
                    if advisory_id.startswith("MAL-"):
                        malware_ids[advisory_id].append(coordinate)
                    else:
                        ordinary_advisories += 1
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            errors.append(str(error)[:500])

    hits: list[dict[str, object]] = []
    for advisory_id, matched in sorted(malware_ids.items()):
        record: object
        try:
            record = requester(
                "GET",
                OSV_VULN_URL_PREFIX + urllib.parse.quote(advisory_id, safe=""),
                None,
                {},
                timeout,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            errors.append(f"{advisory_id}: {str(error)[:400]}")
            continue
        if not isinstance(record, dict):
            errors.append(f"{advisory_id}: OSV record is not an object")
            continue
        if record.get("withdrawn"):
            withdrawn_records += 1
            continue
        package_records = []
        affected = record.get("affected")
        if isinstance(affected, list):
            for item in affected:
                package = item.get("package") if isinstance(item, dict) else None
                if not isinstance(package, dict):
                    continue
                name = package.get("name")
                ecosystem = package.get("ecosystem")
                if isinstance(name, str) and isinstance(ecosystem, str):
                    package_records.append(
                        coordinate_context(
                            matched,
                            ecosystem,
                            normalized_name(ecosystem, name) or name,
                        )
                    )
        if not package_records:
            package_records = [
                coordinate_context(matched, coordinate.ecosystem, coordinate.name)
                for coordinate in deduplicate_coordinates(matched)
            ]
        hits.append(
            {
                "provider": "OSV",
                "id": advisory_id,
                "summary": record.get("summary"),
                "modified": record.get("modified"),
                "withdrawn": None,
                "packages": package_records,
            }
        )

    status = {
        "status": "complete" if not errors else "partial",
        "queries_completed": queries_completed,
        "malware_records": len(hits),
        "withdrawn_records_ignored": withdrawn_records,
        "ordinary_advisories_ignored": ordinary_advisories,
        "errors": errors,
    }
    return status, hits


def query_github(
    coordinates: list[Coordinate],
    *,
    timeout: int,
    requester: JsonRequest = request_json,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    eligible = [
        coordinate
        for coordinate in coordinates
        if coordinate.ecosystem in GITHUB_ECOSYSTEMS
    ]
    truncated = len(eligible) > GITHUB_MAX_PACKAGES
    by_ecosystem: dict[str, list[Coordinate]] = defaultdict(list)
    for coordinate in eligible[:GITHUB_MAX_PACKAGES]:
        github_ecosystem = GITHUB_ECOSYSTEMS.get(coordinate.ecosystem)
        if github_ecosystem:
            by_ecosystem[github_ecosystem].append(coordinate)

    hits: dict[tuple[str, str, str], dict[str, object]] = {}
    errors: list[str] = []
    queries_completed = 0
    withdrawn_records = 0
    for github_ecosystem, ecosystem_coordinates in sorted(by_ecosystem.items()):
        for batch in github_chunks(ecosystem_coordinates):
            affects = ",".join(
                f"{coordinate.name}@{coordinate.version}"
                if coordinate.version
                else coordinate.name
                for coordinate in batch
            )
            query = urllib.parse.urlencode(
                {
                    "type": "malware",
                    "ecosystem": github_ecosystem,
                    "affects": affects,
                    "is_withdrawn": "false",
                    "per_page": "100",
                }
            )
            try:
                response = requester(
                    "GET",
                    f"{GITHUB_ADVISORIES_URL}?{query}",
                    None,
                    {
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                    timeout,
                )
                if not isinstance(response, list):
                    raise TypeError("GitHub malware response is not a list")
                queries_completed += len(batch)
                for advisory in response:
                    if (
                        not isinstance(advisory, dict)
                        or advisory.get("type") != "malware"
                    ):
                        continue
                    if advisory.get("withdrawn_at"):
                        withdrawn_records += 1
                        continue
                    advisory_id = advisory.get("ghsa_id")
                    vulnerabilities = advisory.get("vulnerabilities")
                    if not isinstance(advisory_id, str) or not isinstance(
                        vulnerabilities, list
                    ):
                        continue
                    for vulnerability in vulnerabilities:
                        package = (
                            vulnerability.get("package")
                            if isinstance(vulnerability, dict)
                            else None
                        )
                        if not isinstance(package, dict):
                            continue
                        name = package.get("name")
                        if not isinstance(name, str):
                            continue
                        osv_ecosystem = next(
                            (
                                ecosystem
                                for ecosystem, mapped in GITHUB_ECOSYSTEMS.items()
                                if mapped == github_ecosystem
                            ),
                            github_ecosystem,
                        )
                        safe_name = normalized_name(osv_ecosystem, name) or name
                        context = coordinate_context(batch, osv_ecosystem, safe_name)
                        key = (advisory_id, osv_ecosystem, safe_name)
                        hits[key] = {
                            "provider": "GitHub Advisory Database",
                            "id": advisory_id,
                            "summary": advisory.get("summary"),
                            "withdrawn": advisory.get("withdrawn_at"),
                            "package": context,
                            "affected_range": vulnerability.get(
                                "vulnerable_version_range"
                            ),
                        }
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ) as error:
                errors.append(str(error)[:500])

    status = {
        "status": "complete" if not errors else "partial",
        "queries_completed": queries_completed,
        "malware_records": len(hits),
        "withdrawn_records_ignored": withdrawn_records,
        "eligible_coordinates": len(eligible),
        "truncated_coordinates": max(0, len(eligible) - GITHUB_MAX_PACKAGES),
        "errors": errors,
    }
    if truncated:
        errors.append(
            f"GitHub lookup bounded to {GITHUB_MAX_PACKAGES} coordinates; remaining identities were checked only by other enabled providers"
        )
        status["status"] = "partial"
    return status, list(hits.values())


def summarize_inventory(
    inventory: Inventory,
    candidates: list[Coordinate],
    skipped: list[Coordinate],
    *,
    truncated_packages: bool,
    include_unverified: bool,
) -> dict[str, object]:
    unique = deduplicate_coordinates(inventory.coordinates)
    return {
        "dependency_files": inventory.files,
        "parse_errors": inventory.errors,
        "coordinates_observed": len(inventory.coordinates),
        "unique_coordinates": len(unique),
        "ecosystems": dict(sorted(Counter(item.ecosystem for item in unique).items())),
        "query_candidates": len(candidates),
        "source_verified_candidates": sum(item.public_source for item in candidates),
        "unverified_candidates_included": sum(
            not item.public_source for item in candidates
        ),
        "unverified_or_bounded_out": len(skipped),
        "skipped_examples": [
            coordinate.as_dict() for coordinate in skipped[:MAX_REPORTED_EXAMPLES]
        ],
        "include_unverified_public_identities": include_unverified,
        "truncated": {
            "files": inventory.truncated_files,
            "bytes": inventory.truncated_bytes,
            "packages": truncated_packages,
        },
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = Path(args.directory)
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as error:
        print(json.dumps({"fatal_error": str(error)}))
        return 2
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        print(
            json.dumps(
                {"fatal_error": "target must be a physical directory, not a symlink"}
            )
        )
        return 2

    inventory = inventory_dependencies(
        root,
        max_files=args.max_files,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
    )
    candidates, skipped, truncated_packages = choose_query_candidates(
        inventory.coordinates,
        include_unverified=args.include_unverified_public_identities,
        max_packages=args.max_packages,
    )
    providers: dict[str, object] = {}
    hits: list[dict[str, object]] = []
    if args.inventory_only:
        providers = {
            "OSV": {"status": "not_checked"},
            "GitHub Advisory Database": {"status": "not_checked"},
        }
    else:
        if args.no_osv:
            providers["OSV"] = {"status": "not_checked"}
        else:
            providers["OSV"], osv_hits = query_osv(candidates, timeout=args.timeout)
            hits.extend(osv_hits)
        if args.no_github:
            providers["GitHub Advisory Database"] = {"status": "not_checked"}
        else:
            providers["GitHub Advisory Database"], github_hits = query_github(
                candidates, timeout=args.timeout
            )
            hits.extend(github_hits)

    provider_statuses = [
        status.get("status")
        for status in providers.values()
        if isinstance(status, dict)
    ]
    checked_providers = [
        status for status in provider_statuses if status != "not_checked"
    ]
    provider_complete = bool(checked_providers) and all(
        status == "complete" for status in checked_providers
    )
    inventory_complete = not inventory.errors and not any(
        (inventory.truncated_files, inventory.truncated_bytes, truncated_packages)
    )
    if hits:
        classification_input = "KNOWN_MALWARE_MATCH"
    elif not checked_providers:
        classification_input = "NOT_CHECKED"
    elif provider_complete:
        classification_input = "NO_KNOWN_MALWARE_REPORT"
    else:
        classification_input = "INCOMPLETE_MALWARE_LOOKUP"
    result = {
        "schema_version": 1,
        "lookup_date": datetime.now(UTC).date().isoformat(),
        "target": str(root),
        "invariants": {
            "executes_target_code": False,
            "invokes_package_managers": False,
            "follows_symlinks": False,
            "writes_target": False,
            "advisory_hosts": sorted(ALLOWED_API_HOSTS),
        },
        "inventory": summarize_inventory(
            inventory,
            candidates,
            skipped,
            truncated_packages=truncated_packages,
            include_unverified=args.include_unverified_public_identities,
        ),
        "providers": providers,
        "malware_hits": hits,
        "classification_input": classification_input,
        "complete": inventory_complete and (args.inventory_only or provider_complete),
        "caveat": "Database matches require source, affected-version, and reachable install-path confirmation; no match is not proof of safety.",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
