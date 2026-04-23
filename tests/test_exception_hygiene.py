"""Enforce the P1-6 convention on silent exception handlers.

A bare ``except Exception:`` block may only:
    * log the error (``logger.*``),
    * re-raise, or
    * be followed by an inline comment starting with ``# swallow-ok:`` that
      explains why silence is intentional.

This test greps the repository and fails if any new bare swallow is added
without one of the three justifications. Narrower exception types (e.g.
``except KeyError:``, ``except ValueError:``) are out of scope.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGETS = ["controller.py", "racelink"]
_EXCEPT_LINE = re.compile(r"^\s*except\s+Exception\s*(?:\bas\b\s+\w+\s*)?:\s*(?:#.*)?$")
_SWALLOW_OK = re.compile(r"#\s*swallow-ok:")
_LOGGER_CALL = re.compile(r"\blogger\s*\.\s*(?:exception|error|warning|info|debug)\s*\(")
_RAISE = re.compile(r"^\s*raise\b")


def _iter_py_files():
    for target in TARGETS:
        root = ROOT / target
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _is_justified(lines: list[str], idx: int) -> tuple[bool, str]:
    """Inspect the except block following ``lines[idx]``."""
    except_line = lines[idx]
    if _SWALLOW_OK.search(except_line):
        return True, "swallow-ok on except line"

    # Peek the handler body (next non-blank lines at a deeper indent)
    base_indent = len(except_line) - len(except_line.lstrip())
    j = idx + 1
    while j < len(lines):
        raw = lines[j]
        stripped = raw.rstrip("\n")
        if not stripped.strip():
            j += 1
            continue
        indent = len(stripped) - len(stripped.lstrip())
        if indent <= base_indent:
            break  # block ended
        if _SWALLOW_OK.search(stripped):
            return True, "swallow-ok comment in body"
        if _LOGGER_CALL.search(stripped):
            return True, "logger call in body"
        if _RAISE.match(stripped):
            return True, "re-raise in body"
        j += 1
    return False, "no logger / raise / swallow-ok"


class ExceptionHygieneTests(unittest.TestCase):
    def test_no_unjustified_bare_except_exception(self):
        violations: list[str] = []
        for path in _iter_py_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            lines = text.splitlines(keepends=True)
            for idx, line in enumerate(lines):
                if not _EXCEPT_LINE.match(line):
                    continue
                ok, _reason = _is_justified(lines, idx)
                if not ok:
                    rel = path.relative_to(ROOT)
                    violations.append(f"{rel}:{idx + 1}: bare `except Exception:` without log/raise/swallow-ok")

        if violations:
            msg = "\n".join(violations)
            self.fail(
                "Each `except Exception:` must log, re-raise, or carry a "
                "`# swallow-ok: <reason>` comment.\n" + msg
            )


if __name__ == "__main__":
    unittest.main()
