"""WLED effects whose pixel output depends only on synced inputs.

This module is a *manual* companion to the auto-generated
:mod:`racelink.domain.wled_effects` (which is overwritten by
``gen_wled_metadata.py`` whenever the bundled WLED firmware is bumped).
The IDs below are NOT auto-extracted — they come from a hand audit of
``wled00/FX.cpp`` documented in the WLED-side analysis doc:

    ../../../WLED LoRa/WLED/usermods/racelink_wled/docs/effects-deterministic.md

Why this matters: RaceLink offset mode + ARM-on-SYNC has the master only
push the effect configuration plus a SYNC pulse; each node renders
locally. Only effects that are deterministic given the synced
``strip.now`` + segment params look identical across nodes. Every other
effect drifts (RNG seeds, ``millis()``-based ``beat*`` calls without an
explicit timebase, per-frame ``SEGENV.aux*`` / ``SEGENV.step``
accumulation that's FPS-dependent, audio input).

The host's preset editor consumes this set via
:func:`racelink.domain.specials.wled_effect_mode_options` to mark the
deterministic subset with a leading ``*`` and sort them to the top of
the dropdown. Operators picking offset-mode-safe effects only need to
look at the starred entries.

================================================================
Update workflow
================================================================

When WLED ships a NEW effect (after re-running ``gen_wled_metadata.py``
to refresh ``wled_effects.py``):

  1. Read the analysis doc above, especially §"How to verify a new /
     unlisted effect" — it has a five-step grep checklist that runs
     against the new effect's body in ``wled00/FX.cpp``.
  2. If the effect passes (no ``random*`` / no ``beat*`` without explicit
     timebase / no ``SEGENV.aux*`` or ``SEGENV.step`` accumulation / no
     audio): add its numeric ID to ``WLED_DETERMINISTIC_EFFECT_IDS``
     below, with an inline comment naming the effect + the FX.cpp anchor
     (so the next maintainer can re-verify in seconds).
  3. Update the pin test
     ``tests/test_wled_effect_metadata.py::WledDeterministicTaggingTests::
     test_deterministic_id_set_matches_analysis`` — add the same ID to
     the ``expected`` set, and bump the ``len(...)`` count assertion.
  4. Run ``py -m pytest tests/test_wled_effect_metadata.py -q``.
     Should be 18+ passing; the pin test guards against drift.
  5. (Optional but recommended) update the analysis doc's "✓
     Deterministic — directly verified" table with the new entry so the
     next reviewer doesn't have to re-derive the audit.
  6. The frontend picks the change up automatically — no JS / CSS edit
     needed. The ``*`` marker + sort-to-top come from the backend.

When a WLED release MODIFIES an existing effect that's currently in the
set (e.g. an upstream patch sneaks in a ``random8()`` call):

  1. Re-run the grep checklist for that effect against the new
     ``FX.cpp`` body.
  2. If it now fails any check: REMOVE the ID from
     ``WLED_DETERMINISTIC_EFFECT_IDS``, drop the matching line from the
     pin test, and update the analysis doc's table to demote it (move
     to "⚠ Looks deterministic but is not" with the new failure mode).
  3. Same pytest dance + commit.

When the gateway / WLED firmware audit reveals a structural change (e.g.
WLED's ``beat*`` API gains a global-timebase override and dozens of
new effects qualify): re-run the doc-side audit at scale, then bulk-
update the set + pin test in one commit. Mention the WLED-side commit
SHA in the inline comments so the audit horizon is reproducible.

The two artefacts that MUST stay in sync:
* This file's ``WLED_DETERMINISTIC_EFFECT_IDS`` constant.
* The pin test's ``expected`` set.
The pin test is the safety net — it fails fast if either artefact
drifts.
"""

from __future__ import annotations

# 19 effects, audited 2026-04-29 against WLED LoRa fork at
# ``WLED LoRa/WLED/wled00/FX.cpp`` (see analysis doc for per-ID anchors).
WLED_DETERMINISTIC_EFFECT_IDS: frozenset[int] = frozenset({
    0,    # Solid               — pure colour fill, no time dependency
    1,    # Blink               — strip.now / cycleTime phase
    2,    # Breathe              — sin16_t(strip.now * speed); end-to-end tested
    3,    # Wipe                 — strip.now % cycleTime position
    6,    # Sweep                — same engine as Wipe
    8,    # Colorloop / Rainbow  — color_wheel((strip.now * speed) >> 8)
    9,    # Rainbow Cycle        — per-pixel color_wheel from strip.now + position
    10,   # Scan                 — position from strip.now % cycleTime
    11,   # Scan Dual            — same as Scan
    12,   # Fade                 — triwave16(strip.now * speed)
    15,   # Running              — sin8_t(i*x_scale - (strip.now * speed >> 9))
    16,   # Saw                  — same engine as Running (saw=true)
    23,   # Strobe               — delegates to Blink
    35,   # Traffic Light        — strip.now-driven state machine; tested
    52,   # Running Dual         — same engine as Running (dual=true)
    65,   # Palette              — strip.now-driven shift + rotation; rich for offset demos
    83,   # Solid Pattern        — static, no strip.now at all
    84,   # Solid Pattern Tri    — static
    115,  # Blends               — strip.now-driven shift + quadwave8
})


def is_deterministic(effect_id) -> bool:
    """Return True iff the WLED effect with this id renders identically
    across nodes that share ``strip.timebase``.

    Accepts int or str (the auto-generated ``WLED_EFFECTS`` list uses
    str ``value`` keys); coerces silently. Unknown / non-numeric inputs
    return False — non-deterministic is the safe default for a UI hint.
    """
    try:
        return int(effect_id) in WLED_DETERMINISTIC_EFFECT_IDS
    except (TypeError, ValueError):
        return False
