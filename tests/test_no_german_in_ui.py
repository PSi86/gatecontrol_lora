"""Regression: no German strings in operator-facing assets (C1).

Several rounds of edits left German text in the HTML pages and a few
JS comments — discovered during the project-wide audit. The fix
(2026-04-27) translated every reachable string to English; this test
prevents the issue from reappearing.

Two layers of detection:

1. **German letters** (``ä ö ü Ä Ö Ü ß``) — fast and unambiguous;
   anything that ends up in a UI asset with these characters is German.
2. **Common German tokens** in user-facing files — catches German that
   only uses ASCII (``Wähle``, ``persistente``, ``natürlich`` etc. are
   not all umlauted, so the letter check alone misses them). The token
   list is intentionally short and word-boundary-anchored to keep
   false positives down — generic Latin tokens that happen to be
   German cognates (e.g. ``alarm``, ``information``) are excluded.

Files scanned: HTML pages and JS in ``racelink/`` plus a few CSS
comments. Tests in ``tests/`` and the auto-generated proto module are
excluded — translation is a UI concern, not a regression-test concern,
and proto comments are mechanically generated.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Anywhere we'd surface text to operators OR contributors who'd read
# the source. Tests are excluded — they may legitimately reference the
# plan filename which contains the word "szenen" (German plural of
# scene) for historical reasons.
SCAN_GLOBS = (
    "racelink/pages/*.html",
    "racelink/static/*.js",
    "racelink/static/*.css",
    "racelink/services/*.py",
    "racelink/web/*.py",
    "racelink/transport/*.py",
    "racelink/domain/*.py",
    "controller.py",
)

# Files we deliberately skip even within the globs above.
SKIP_FILES = {
    "racelink/racelink_proto_auto.py",  # auto-generated
    "racelink/static/vendor",  # third-party (Sortable.min.js etc.)
}

GERMAN_LETTERS = re.compile(r"[äöüÄÖÜß]")

# Word-boundary-anchored German tokens. Each entry must be specific
# enough that the English vocabulary doesn't collide. Keep this list
# short — false positives are noisier than misses.
GERMAN_TOKENS = re.compile(
    r"(?i)\b("
    # Common verbs we'd see in instructional UI strings:
    r"angewendet|gesendet|aktualisieren|hinzuf|l[oö]schen|"
    r"speichern|abbrechen|w[aä]hle[ns]?|verbinden|trennen|"
    # Nouns specific to RaceLink-flavoured German:
    r"szenen?|effekt[- ]?snapshots?|ger[aä]te|"
    # Common discourse markers that practically never appear in an
    # English UI string:
    r"nat[uü]rlich|zur[uü]ck"
    r")\b"
)


def _scan_files():
    seen = set()
    for glob in SCAN_GLOBS:
        for path in ROOT.glob(glob):
            rel = path.relative_to(ROOT).as_posix()
            if any(rel.startswith(skip) for skip in SKIP_FILES):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


class NoGermanInUITests(unittest.TestCase):

    def test_no_german_letters_in_ui_assets(self):
        """The umlaut / ß family is the cheapest indicator of German
        text that slipped through review. Any hit means a translation
        was missed."""
        violations: list[tuple[str, int, str]] = []
        for path in _scan_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if GERMAN_LETTERS.search(line):
                    violations.append(
                        (path.relative_to(ROOT).as_posix(), lineno, line.strip())
                    )
        if violations:
            details = "\n".join(
                f"  {p}:{n}: {line}" for p, n, line in violations
            )
            self.fail(
                "German characters (ä/ö/ü/ß) found in UI assets — "
                "translate to English:\n" + details
            )

    def test_no_common_german_tokens_in_ui_assets(self):
        """Catches German strings that happen to be ASCII-only.
        ``Wähle`` becomes ``Wahle`` if someone strips umlauts; the
        token list above pins both spellings so the regression test
        still trips."""
        violations: list[tuple[str, int, str, str]] = []
        for path in _scan_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                m = GERMAN_TOKENS.search(line)
                if m:
                    violations.append(
                        (
                            path.relative_to(ROOT).as_posix(),
                            lineno,
                            m.group(0),
                            line.strip(),
                        )
                    )
        if violations:
            details = "\n".join(
                f"  {p}:{n}: token={tok!r}  line={line}"
                for p, n, tok, line in violations
            )
            self.fail(
                "German tokens found in UI assets — translate to "
                "English:\n" + details
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
