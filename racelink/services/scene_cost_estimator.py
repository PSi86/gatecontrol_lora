"""Predict the wire cost (packets, bytes, airtime) of a scene before it runs.

The editor calls this to show the operator how much radio time a scene
will consume — critical for a low-bandwidth LoRa link where a careless
fan-out can stretch playback to seconds. The runner is the source of
truth; this module mirrors its dispatch logic *without* side effects so
the predictions match what would actually go on the wire.

Two output dataclasses:

* :class:`ActionCost` — packets / bytes / airtime for one scene action.
* :class:`SceneCost` — total + per-action list, 1:1 with ``scene.actions``.

``cost.bytes`` is the *full* on-wire packet size: variable body +
``RADIO_HEADER_BYTES`` (Header7) + ``USB_FRAMING_BYTES`` (host link
sentinel + length prefix). LoRa-PHY framing (preamble symbols, header,
CRC) is not surfaced as bytes — it is encoded into ``airtime_ms`` via
the Semtech AN1200.13 formula in :func:`lora_airtime_ms`.

For ``rl_preset`` actions, callers should pass an ``rl_preset_lookup``
callable so the estimator materialises the referenced preset's full
params before sizing the OPC_CONTROL body. Without it, the action's own
``params`` (typically just an optional brightness override) is used,
which under-reports the wire size. The web layer wires this lookup in
``racelink/web/api.py`` from the running ``RlPresetsService``.

LoRa airtime is computed from the codable parameters at the top of this
module (see ``LORA_*``). Bumping SF/BW/CR there propagates through the
whole editor without touching the formula. The defaults match the
SX1262 settings used by the Gateway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..protocol.packets import (
    build_control_body,
    build_offset_body,
    build_preset_body,
    build_sync_body,
)
from .offset_dispatch_optimizer import plan_offset_setup
from .scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
)

# ---------------------------------------------------------------------------
# RaceLink LoRa parameters — single source of truth for airtime estimation.
# Match the SX1262 settings used by the Gateway (RaceLink_Gateway/src/main.cpp).
# Bumping BW/SF here lets us recompute airtime predictions across the whole
# scene editor without touching the formula. User feedback (point 4):
# these are intentionally codable in code — easy to read, easy to tweak.
# ---------------------------------------------------------------------------

LORA_SF: int = 7                  # spreading factor 6..12
# 2026-04-28: bumped from 250_000 -> 125_000 to match the gateway's actual
# RACELINK_BW_KHZ=125 in RaceLink_Gateway/src/main.cpp. The previous value
# under-counted airtime by exactly 2x (Tsym = 2^SF / BW; halving BW doubles
# Tsym), making cost-badge estimates appear ~half of measured wall-clock.
LORA_BW_HZ: int = 125_000         # bandwidth in Hz
LORA_CR: int = 5                  # coding rate denominator: 5 -> 4/5, 8 -> 4/8
LORA_PREAMBLE_SYM: int = 8        # preamble symbols
LORA_EXPLICIT_HEADER: bool = True
LORA_LOW_DR_OPT: bool = False     # Semtech recommends ON for SF >= 11 / BW <= 125 kHz
LORA_CRC_ON: bool = True

# USB framing + radio header overhead per packet, in bytes. Independent of
# the body length but always present on the wire.
RADIO_HEADER_BYTES = 7   # Header7 (sender + receiver + type)
USB_FRAMING_BYTES = 2    # 0x00 + LEN prefix on the host<->gateway link

# Per-packet wall-clock overhead in milliseconds, ON TOP of pure LoRa airtime.
# This is what the host's _send_m2n call wraps around the radio transmission:
# USB-CDC bridge round-trip (host write + bridge buffering + gateway read) plus
# gateway state-machine transitions (Idle → Tx → standby → transmit setup →
# IDLE → continuous-RX) plus the RX-side mirror of the same.
#
# Empirically calibrated 2026-04-29 against measured cost-badge `actual:` after
# the gateway-side optimisations (no jitter, USB low-latency mode, coalesced
# Serial.write, Core-0 display task):
#   * 1 pkt 30 B: estimate 67 ms airtime + 12 ms overhead = 79 ms predicted;
#     observed 78-81 ms wall-clock — match.
#   * 4 pkts 86 B (offset_group): 216 ms airtime + 4 × 12 ms = 264 ms;
#     observed 263-267 ms — match.
#
# This number is a single-gateway, single-host-to-Pi calibration. Bumping
# the SF/BW further or moving to a faster transport (native USB-CDC on
# ESP32-S3) would shift it; keep recalibrating when the wire changes.
WIRE_OVERHEAD_MS_PER_PACKET: float = 12.0


def lora_airtime_ms(payload_bytes: int) -> float:
    """Time-on-air for a LoRa packet of ``payload_bytes`` (Semtech AN1200.13).

    ``payload_bytes`` is the *full* PHY payload — caller adds the
    RaceLink Header7 + body and (optionally) the USB framing. Returns
    milliseconds (float). Symbol time = 2^SF / BW; the symbol count
    formula handles header presence and low-data-rate optimisation.
    """
    payload = max(1, int(payload_bytes))
    Tsym_ms = (1 << LORA_SF) / LORA_BW_HZ * 1000.0
    DE = 1 if LORA_LOW_DR_OPT else 0
    H = 0 if LORA_EXPLICIT_HEADER else 1
    CRC = 1 if LORA_CRC_ON else 0
    num = 8 * payload - 4 * LORA_SF + 28 + 16 * CRC - 20 * H
    den = 4 * (LORA_SF - 2 * DE)
    n_payload = max(0, math.ceil(num / den)) * LORA_CR + 8
    n_symbols = LORA_PREAMBLE_SYM + 4.25 + n_payload
    return n_symbols * Tsym_ms


def lora_parameters() -> Dict[str, Any]:
    """The active LoRa parameter dict — exposed via the editor schema so the
    UI can render a tooltip like "at SF7/125 kHz/CR4:5". Also surfaces
    ``wire_overhead_ms_per_packet`` so the cost-badge tooltip can break
    the wall-clock prediction down into "LoRa airtime + radio/USB overhead"."""
    return {
        "sf": LORA_SF,
        "bw_hz": LORA_BW_HZ,
        "cr": LORA_CR,
        "preamble_sym": LORA_PREAMBLE_SYM,
        "explicit_header": LORA_EXPLICIT_HEADER,
        "low_dr_opt": LORA_LOW_DR_OPT,
        "crc_on": LORA_CRC_ON,
        "wire_overhead_ms_per_packet": WIRE_OVERHEAD_MS_PER_PACKET,
    }


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActionCost:
    """Predicted wire cost for one scene action.

    Two time fields:

    * ``airtime_ms`` — pure LoRa time-on-air (Semtech AN1200.13). Useful
      for diagnostics (how many ms of radio resource the action uses).
    * ``wall_clock_ms`` — the host-observable wall-clock prediction the
      operator sees on the cost badge: ``airtime + N *
      WIRE_OVERHEAD_MS_PER_PACKET``. The cost-badge ``≈ NN ms`` should
      compare to the runner's measured ``actual:`` value within a few
      milliseconds.

    For the special ``KIND_DELAY`` action both fields equal the
    configured ``duration_ms`` — the operator wait is "real time" with
    no per-packet cost.
    """
    packets: int = 0
    bytes: int = 0
    airtime_ms: float = 0.0
    wall_clock_ms: float = 0.0
    # Optional structured detail (e.g. optimizer strategy for offset_group).
    detail: Dict[str, Any] = field(default_factory=dict)

    def add_packet(self, body_bytes: int) -> None:
        self.packets += 1
        frame = body_bytes + RADIO_HEADER_BYTES
        self.bytes += frame + USB_FRAMING_BYTES
        airtime = lora_airtime_ms(frame)
        self.airtime_ms += airtime
        # wall_clock_ms = airtime + per-packet USB/gateway overhead. See
        # WIRE_OVERHEAD_MS_PER_PACKET docstring above for the calibration
        # data behind the constant.
        self.wall_clock_ms += airtime + WIRE_OVERHEAD_MS_PER_PACKET


@dataclass
class SceneCost:
    """Aggregate cost of a whole scene + breakdown per action."""
    total: ActionCost = field(default_factory=ActionCost)
    per_action: List[ActionCost] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

RlPresetLookup = Callable[[Any], Optional[Mapping[str, Any]]]


def estimate_action(action: Mapping[str, Any], *,
                    known_group_ids: Optional[List[int]] = None,
                    rl_preset_lookup: Optional[RlPresetLookup] = None) -> ActionCost:
    """Estimate wire cost of a single canonical scene action.

    ``known_group_ids`` is forwarded to the optimizer for ``offset_group``
    container actions; pass an empty list (default) to mirror the test
    environment where no devices are wired up — the optimizer still picks
    a sane strategy (broadcast formula remains available).

    ``rl_preset_lookup`` resolves an RL-preset reference (slug, ``RL:slug``,
    or int id) to its stored preset dict. When provided, ``rl_preset``
    actions size their OPC_CONTROL body using the preset's full params;
    without it we fall back to the action's own ``params`` (typically just
    a brightness override), which under-reports the wire size. The runner's
    ``_lookup_rl_preset`` is the contract this signature mirrors.
    """
    kind = action.get("kind")
    cost = ActionCost()
    if kind == KIND_SYNC:
        # 1× OPC_SYNC (P_Sync = 4 B body)
        cost.add_packet(len(build_sync_body(0, 0)))
        return cost
    if kind == KIND_DELAY:
        # No packets; both time fields are the literal sleep duration.
        # The scene total reflects "real time the operator will wait" —
        # the scheduler is host-side, not radio, but it still adds to
        # total scene duration. wall_clock_ms tracks the same value
        # because there's no per-packet overhead to add for a delay.
        duration = float(action.get("duration_ms") or 0)
        cost.airtime_ms = duration
        cost.wall_clock_ms = duration
        return cost
    if kind == KIND_STARTBLOCK:
        # Startblock is handled by the controller via a custom path; treat
        # it as a single OPC_CONTROL-sized packet for ballpark estimation.
        cost.add_packet(_estimate_control_body_len(action.get("params") or {}))
        return cost
    if kind == KIND_WLED_PRESET:
        cost.add_packet(len(build_preset_body(0, 0, 0, 0)))
        return cost
    if kind == KIND_WLED_CONTROL:
        cost.add_packet(_estimate_control_body_len(action.get("params") or {}))
        return cost
    if kind == KIND_RL_PRESET:
        params = _materialize_rl_preset_params(action, rl_preset_lookup)
        cost.add_packet(_estimate_control_body_len(params))
        return cost
    if kind == KIND_OFFSET_GROUP:
        return _estimate_offset_group_cost(action, known_group_ids or [], rl_preset_lookup)
    # Unknown kind — return zero-cost so the editor doesn't blow up on a
    # forward-compat scene.
    return cost


def estimate_scene(scene: Mapping[str, Any], *,
                   known_group_ids: Optional[List[int]] = None,
                   rl_preset_lookup: Optional[RlPresetLookup] = None) -> SceneCost:
    """Estimate wire cost of a whole scene. ``per_action`` is in scene order."""
    out = SceneCost()
    for action in scene.get("actions") or []:
        cost = estimate_action(action,
                               known_group_ids=known_group_ids,
                               rl_preset_lookup=rl_preset_lookup)
        out.per_action.append(cost)
        out.total.packets += cost.packets
        out.total.bytes += cost.bytes
        out.total.airtime_ms += cost.airtime_ms
        out.total.wall_clock_ms += cost.wall_clock_ms
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _estimate_offset_group_cost(action: Mapping[str, Any],
                                 known_group_ids: List[int],
                                 rl_preset_lookup: Optional[RlPresetLookup] = None) -> ActionCost:
    """Use the optimizer to plan the OPC_OFFSET phase, then add one packet
    per child (broadcast / unicast). Mirrors ``_run_offset_group``."""
    cost = ActionCost()

    # Phase 1: OPC_OFFSET sequence (optimizer-planned).
    plan = plan_offset_setup(
        participant_groups=action.get("groups"),
        offset=action.get("offset") or {"mode": "none"},
        known_group_ids=known_group_ids,
    )
    for op in plan.ops:
        cost.add_packet(op.body_bytes)

    # Phase 2: one packet per child. Use kind to pick the right body builder.
    children = action.get("actions") or []
    for child in children:
        child_kind = child.get("kind")
        if child_kind == KIND_WLED_PRESET:
            cost.add_packet(len(build_preset_body(0, 0, 0, 0)))
        elif child_kind == KIND_WLED_CONTROL:
            cost.add_packet(_estimate_control_body_len(child.get("params") or {}))
        elif child_kind == KIND_RL_PRESET:
            params = _materialize_rl_preset_params(child, rl_preset_lookup)
            cost.add_packet(_estimate_control_body_len(params))
        # Unknown child kinds add 0 — validator already rejects them, this
        # is a forward-compat safety net.

    cost.detail = {
        "wire_path": plan.strategy,
        "offset_mode": (action.get("offset") or {}).get("mode") or "none",
        "offset_packets": plan.packet_count,
        "child_count": len(children),
    }
    return cost


def _materialize_rl_preset_params(action: Mapping[str, Any],
                                   lookup: Optional[RlPresetLookup]) -> Dict[str, Any]:
    """Resolve an ``rl_preset`` action's params to what the runner would
    actually emit on the wire.

    The runner pops ``presetId`` from the action's params, looks up the
    stored preset, and merges the action's brightness override on top of
    ``preset.params``. Without ``lookup`` we cannot resolve the preset, so
    fall back to whatever ``action.params`` carries (usually empty + a
    brightness override) — that under-reports but never crashes.
    """
    base_params = dict(action.get("params") or {})
    preset_ref = base_params.pop("presetId", None)
    if lookup is None or preset_ref is None:
        return base_params
    try:
        preset = lookup(preset_ref)
    except Exception:
        # swallow-ok: estimator must never throw — observability path.
        preset = None
    if not preset:
        return base_params
    merged: Dict[str, Any] = dict(preset.get("params") or {})
    if "brightness" in base_params and base_params["brightness"] is not None:
        merged["brightness"] = base_params["brightness"]
    return merged


def _estimate_control_body_len(params: Mapping[str, Any]) -> int:
    """Approximate body length of an OPC_CONTROL with the given params dict.

    Uses the canonical builder so the prediction matches what the runner
    would actually emit. Unknown params default to 0 (smallest body),
    which is a conservative under-estimate for ad-hoc inputs but matches
    the runner's ``ControlService.send_wled_control`` behaviour where
    only explicitly-provided fields are serialised.
    """
    kwargs: Dict[str, Any] = {}
    if "brightness" in params and params["brightness"] is not None:
        kwargs["brightness"] = int(params["brightness"]) & 0xFF
    for key in ("mode", "speed", "intensity", "custom1", "custom2", "custom3", "palette"):
        if key in params and params[key] is not None:
            kwargs[key] = int(params[key]) & 0xFF
    for key in ("check1", "check2", "check3"):
        if key in params and params[key] is not None:
            kwargs[key] = bool(params[key])
    for key in ("color1", "color2", "color3"):
        if key in params and params[key] is not None:
            kwargs[key] = tuple(int(c) & 0xFF for c in params[key])
    body = build_control_body(group_id=0, flags=0, **kwargs)
    return len(body)
