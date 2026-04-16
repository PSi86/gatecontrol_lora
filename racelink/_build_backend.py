"""Minimal stdlib-only build backend for local RaceLink packaging."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import tarfile
import zipfile


NAME = "racelink-host"
VERSION = "0.1.0"
SUMMARY = "Host-side software for the RaceLink wireless control system."
REQUIRES_PYTHON = ">=3.10"
DEPENDENCIES = (
    "Flask>=3.0,<4",
    "pyserial>=3.5,<4",
)
PROJECT_URL = "Repository, https://github.com/PSi86/RaceLink_Host"
ENTRY_POINTS = {
    "console_scripts": [
        "racelink-standalone = racelink.integrations.standalone.webapp:run_standalone",
    ]
}

ROOT = Path(__file__).resolve().parents[1]


def _package_data_sources() -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    sources.extend(
        (path, f"racelink/pages/{path.relative_to(ROOT / 'pages').as_posix()}")
        for path in sorted((ROOT / "pages").rglob("*"))
        if path.is_file()
    )
    sources.extend(
        (path, f"racelink/static/{path.relative_to(ROOT / 'static').as_posix()}")
        for path in sorted((ROOT / "static").rglob("*"))
        if path.is_file()
    )
    return sources


def _dist_info_dir() -> str:
    return f"{NAME.replace('-', '_')}-{VERSION}.dist-info"


def _wheel_name() -> str:
    return f"{NAME.replace('-', '_')}-{VERSION}-py3-none-any.whl"


def _metadata_text() -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {NAME}",
        f"Version: {VERSION}",
        f"Summary: {SUMMARY}",
        f"Requires-Python: {REQUIRES_PYTHON}",
        f"Project-URL: {PROJECT_URL}",
    ]
    for requirement in DEPENDENCIES:
        lines.append(f"Requires-Dist: {requirement}")
    return "\n".join(lines) + "\n"


def _wheel_text() -> str:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: racelink._build_backend",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
        ]
    )


def _entry_points_text() -> str:
    lines: list[str] = []
    for group, entries in ENTRY_POINTS.items():
        lines.append(f"[{group}]")
        lines.extend(entries)
        lines.append("")
    return "\n".join(lines)


def _top_level_text() -> str:
    return "controller\nracelink\n"


def _iter_sources() -> list[tuple[Path, str]]:
    sources = [(ROOT / "controller.py", "controller.py")]
    sources.extend((path, path.relative_to(ROOT).as_posix()) for path in sorted((ROOT / "racelink").rglob("*.py")))
    sources.extend(_package_data_sources())
    return sources


def _record_line(rel_path: str, data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{rel_path},sha256={encoded},{len(data)}"


def _metadata_files() -> dict[str, bytes]:
    dist_info = _dist_info_dir()
    return {
        f"{dist_info}/METADATA": _metadata_text().encode("utf-8"),
        f"{dist_info}/WHEEL": _wheel_text().encode("utf-8"),
        f"{dist_info}/entry_points.txt": _entry_points_text().encode("utf-8"),
        f"{dist_info}/top_level.txt": _top_level_text().encode("utf-8"),
    }


def _write_metadata_dir(target: Path) -> str:
    dist_info = target / _dist_info_dir()
    dist_info.mkdir(parents=True, exist_ok=True)
    for rel_path, data in _metadata_files().items():
        (target / rel_path).write_bytes(data)
    return dist_info.name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    del config_settings, metadata_directory
    wheel_dir = Path(wheel_directory)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = wheel_dir / _wheel_name()

    entries: list[tuple[str, bytes]] = []
    for src_path, rel_path in _iter_sources():
        entries.append((rel_path, src_path.read_bytes()))
    entries.extend(_metadata_files().items())

    records = [_record_line(rel_path, data) for rel_path, data in entries]
    record_path = f"{_dist_info_dir()}/RECORD"
    record_bytes = ("\n".join(records + [f"{record_path},,"]) + "\n").encode("utf-8")
    entries.append((record_path, record_bytes))

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel_path, data in entries:
            archive.writestr(rel_path, data)

    return wheel_path.name


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    del config_settings
    return _write_metadata_dir(Path(metadata_directory))


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    del config_settings
    return []


def build_sdist(sdist_directory, config_settings=None) -> str:
    del config_settings
    sdist_dir = Path(sdist_directory)
    sdist_dir.mkdir(parents=True, exist_ok=True)
    sdist_name = f"{NAME}-{VERSION}.tar.gz"
    sdist_path = sdist_dir / sdist_name
    with tarfile.open(sdist_path, "w:gz") as archive:
        for src_path, rel_path in _iter_sources():
            archive.add(src_path, arcname=f"{NAME}-{VERSION}/{rel_path}")
        archive.add(ROOT / "pyproject.toml", arcname=f"{NAME}-{VERSION}/pyproject.toml")
        archive.add(ROOT / "README.md", arcname=f"{NAME}-{VERSION}/README.md")
    return sdist_path.name
