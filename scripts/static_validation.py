#!/usr/bin/env python3
"""Project static validation checks for provider wiring and legacy imports."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]

# Known runtime composition roots where RaceLink_LoRa(...) must pass race_provider explicitly.
KNOWN_RUNTIME_FILES = [
    Path("plugins/rotorhazard/plugin_runtime.py"),
    Path("plugins/standalone/flask_adapter.py"),
]

# Patterns for forbidden top-level providers imports.
ROOT_PROVIDERS_PATTERNS = [
    re.compile(r"^\s*from\s+providers(?:\.|\s+import\b)", re.MULTILINE),
    re.compile(r"^\s*import\s+providers(?:\.|\b)", re.MULTILINE),
]

NOOP_PROVIDER_PATTERN = re.compile(r"\bNoOpRaceProvider\b")


@dataclass
class Finding:
    path: Path
    message: str


def iter_python_files() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.py")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    ]


def check_no_root_providers_imports() -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_python_files():
        rel_path = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in ROOT_PROVIDERS_PATTERNS):
            findings.append(
                Finding(
                    rel_path,
                    "forbidden root-level providers import detected",
                )
            )
    return findings


def check_no_noop_provider_refs() -> list[Finding]:
    findings: list[Finding] = []
    excluded_paths = {Path("scripts/static_validation.py")}
    for path in iter_python_files():
        rel_path = path.relative_to(REPO_ROOT)
        if rel_path in excluded_paths:
            continue
        text = path.read_text(encoding="utf-8")
        if NOOP_PROVIDER_PATTERN.search(text):
            findings.append(
                Finding(
                    rel_path,
                    "forbidden NoOpRaceProvider reference detected",
                )
            )
    return findings


def check_known_runtime_race_provider() -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in KNOWN_RUNTIME_FILES:
        path = REPO_ROOT / rel_path
        if not path.exists():
            findings.append(Finding(rel_path, "known runtime file missing (check list/update)"))
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "RaceLink_LoRa":
                has_race_provider_kw = any(
                    kw.arg == "race_provider" for kw in node.keywords if kw.arg is not None
                )
                if not has_race_provider_kw:
                    findings.append(
                        Finding(
                            rel_path,
                            f"RaceLink_LoRa call at line {node.lineno} is missing race_provider=",
                        )
                    )
    return findings


def print_findings(title: str, findings: list[Finding]) -> None:
    if not findings:
        print(f"[OK] {title}")
        return
    print(f"[FAIL] {title}")
    for finding in findings:
        print(f"  - {finding.path}: {finding.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-known-runtime-provider",
        action="store_true",
        help="Treat missing race_provider in known runtime files as warning only.",
    )
    args = parser.parse_args()

    root_provider_findings = check_no_root_providers_imports()
    noop_provider_findings = check_no_noop_provider_refs()
    runtime_provider_findings = check_known_runtime_race_provider()

    print_findings("No root-level providers imports", root_provider_findings)
    print_findings("No NoOpRaceProvider references", noop_provider_findings)

    if args.warn_known_runtime_provider:
        if runtime_provider_findings:
            print("[WARN] Known runtime files: RaceLink_LoRa must pass race_provider=")
            for finding in runtime_provider_findings:
                print(f"  - {finding.path}: {finding.message}")
        else:
            print("[OK] Known runtime files: RaceLink_LoRa passes race_provider=")
    else:
        print_findings(
            "Known runtime files: RaceLink_LoRa must pass race_provider=",
            runtime_provider_findings,
        )

    failing = bool(root_provider_findings or noop_provider_findings)
    if not args.warn_known_runtime_provider:
        failing = failing or bool(runtime_provider_findings)

    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
