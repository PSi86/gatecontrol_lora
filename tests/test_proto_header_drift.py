"""Regression: ``racelink_proto.h`` must stay byte-identical across the
three repos that consume it (B8).

The wire protocol is duplicated in three places by design (each
firmware repo carries its own copy so it can be flashed
standalone):

* ``./racelink_proto.h``                       (Host = this repo)
* ``../RaceLink_Gateway/src/racelink_proto.h``  (Gateway firmware)
* ``../RaceLink_WLED/racelink_proto.h``         (WLED firmware)

Drift between any pair is a real bug — opcodes / structs / flags
shift on the wire in ways that look plausible end-to-end until a
specific combination of host build + firmware build deserialises
the same bytes differently.

Per the audit's B8 finding: the project had no automated check that
fails when these diverge. This test fills that gap.

The sibling repos may not be present (e.g. CI checks out only the
Host). When a sibling is missing, the test logs and skips that
comparison rather than failing — drift can only be checked when
both files exist on disk.
"""

from __future__ import annotations

import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOST_HEADER = ROOT / "racelink_proto.h"

# Sibling repo paths, relative to the Host repo's parent. Adjust here
# if the layout ever changes — the Host's ``docs/repo_split_map.md``
# documents the canonical layout.
SIBLING_HEADERS = {
    "Gateway": ROOT.parent / "RaceLink_Gateway" / "src" / "racelink_proto.h",
    "WLED":    ROOT.parent / "RaceLink_WLED" / "racelink_proto.h",
}


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


class ProtoHeaderDriftTests(unittest.TestCase):

    def test_host_header_exists(self):
        """The Host's own copy must always exist — it's the source of
        truth for the auto-generated ``racelink_proto_auto.py``."""
        self.assertTrue(
            HOST_HEADER.is_file(),
            f"Host racelink_proto.h not found at {HOST_HEADER}",
        )

    def test_sibling_headers_match_host_byte_for_byte(self):
        """Every sibling repo's copy that is present on disk must
        match the Host's copy. A diff is a hard failure — silent
        drift produced subtle wire bugs in the past (the audit
        documented one such case where the Gateway and WLED differed
        on a struct ordering for one release).

        Missing sibling repos are not an error — operators may check
        out only the Host. Skipped comparisons get a printed message
        so a CI run that should have covered all three is visible
        in the log.
        """
        host_hash = _sha256(HOST_HEADER)
        compared = []
        for name, path in SIBLING_HEADERS.items():
            if not path.is_file():
                # Logged via ``addCleanup`` so the message survives
                # the test passing (unittest swallows print() inside
                # a passing test under some runners).
                self.addCleanup(
                    print,
                    f"[proto-drift] sibling not present: {name} ({path})",
                )
                continue
            sibling_hash = _sha256(path)
            compared.append(name)
            self.assertEqual(
                sibling_hash, host_hash,
                f"racelink_proto.h drift: Host vs {name} "
                f"({path}) — sha256 differs.\n"
                f"  Host:    {host_hash}\n"
                f"  {name + ':':9} {sibling_hash}\n"
                f"Re-sync the headers or update gen_racelink_proto_py.py.",
            )

        # Smoke-print the set of repos actually compared — useful in
        # CI logs to confirm the test exercised both siblings.
        self.addCleanup(
            print,
            f"[proto-drift] compared Host against: "
            f"{compared if compared else '(none — only Host present)'}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
