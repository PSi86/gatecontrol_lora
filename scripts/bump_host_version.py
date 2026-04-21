"""Update the canonical RaceLink Host version for a release."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<suffix>[-+][0-9A-Za-z.-]+)?$"
)
VERSION_FILE_PATTERN = re.compile(
    r'^(?P<prefix>VERSION\s*=\s*")(?P<version>[^"]+)(?P<suffix>")$',
    re.MULTILINE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update racelink/_version.py with a release version.",
    )
    parser.add_argument(
        "--version-file",
        default=Path("racelink/_version.py"),
        type=Path,
        help="Path to the canonical version helper module.",
    )
    parser.add_argument(
        "--version",
        default="",
        help="Explicit host version. If omitted, increment the current patch version.",
    )
    return parser.parse_args()


def _normalize_version(version: str) -> str:
    normalized = str(version).strip().removeprefix("v")
    if not normalized:
        return normalized
    if not VERSION_PATTERN.fullmatch(normalized):
        message = (
            "Version must look like semantic versioning, for example 0.1.3 or 0.1.3-rc1"
        )
        raise ValueError(message)
    return normalized


def _increment_version(current_version: str) -> str:
    match = VERSION_PATTERN.fullmatch(current_version)
    if match is None:
        message = f"Current host version is not valid semver: {current_version}"
        raise ValueError(message)

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch")) + 1
    suffix = match.group("suffix") or ""
    return f"{major}.{minor}.{patch}{suffix}"


def bump_host_version(*, version_file: Path, version: str) -> str:
    """Write an explicit or auto-incremented version into racelink/_version.py."""
    source = version_file.read_text(encoding="utf-8")
    match = VERSION_FILE_PATTERN.search(source)
    if match is None:
        message = f"Could not find VERSION assignment in {version_file}"
        raise ValueError(message)

    current_version = str(match.group("version")).strip().removeprefix("v")
    target_version = _normalize_version(version) or _increment_version(current_version)
    updated = (
        source[: match.start("version")]
        + target_version
        + source[match.end("version") :]
    )
    version_file.write_text(updated, encoding="utf-8")
    return target_version


def main() -> int:
    """Run the host version updater from the command line."""
    args = _parse_args()
    version = bump_host_version(
        version_file=args.version_file.resolve(),
        version=args.version,
    )
    sys.stdout.write(f"{version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
