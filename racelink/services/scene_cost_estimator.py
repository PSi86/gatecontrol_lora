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

from .dispatch_planner import (
    DeviceLookup,
    RlPresetLookup,
    plan_action_dispatch,
)
from .scenes_service import KIND_DELAY

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


def estimate_action(action: Mapping[str, Any], *,
                    known_group_ids: Optional[List[int]] = None,
                    rl_preset_lookup: Optional[RlPresetLookup] = None,
                    device_lookup: Optional[DeviceLookup] = None) -> ActionCost:
    """Estimate wire cost of a single canonical scene action.

    Routes through :func:`plan_action_dispatch` (the same pure planner
    the runner consumes) and sums ``body_bytes`` per planned op. This
    is the structural-sync point: if the runner emits N packets for an
    action, this function predicts exactly N — anything else is a
    parity bug, caught by ``tests/test_dispatch_parity.py``.

    ``known_group_ids`` is forwarded to the optimizer for
    ``offset_group`` containers; without it Strategy C is ineligible
    and the estimator falls back to Strategy B (matches the runner's
    behaviour on a host with no device repository — so the prediction
    still tracks the wire even in degraded environments).

    ``rl_preset_lookup`` resolves preset references the same way the
    runner does, so RL-preset actions size their OPC_CONTROL body
    using the merged preset params (not just the action's brightness
    override).

    ``device_lookup`` resolves a device target's MAC to a device
    object — the same callable the runner uses
    (``controller.getDeviceFromAddress``). When ``None``, device
    targets degrade (matching the runner's behaviour) so the cost
    badge surfaces unresolvable targets as 0 packets.
    """
    cost = ActionCost()
    if action.get("kind") == KIND_DELAY:
        # No wire packets — both time fields are the literal sleep
        # duration. The scene total reflects "real time the operator
        # will wait" since delay adds to total scene duration even
        # though the scheduler is host-side, not radio.
        duration = float(action.get("duration_ms") or 0)
        cost.airtime_ms = duration
        cost.wall_clock_ms = duration
        return cost

    plan = plan_action_dispatch(
        action,
        known_group_ids=known_group_ids or [],
        rl_preset_lookup=rl_preset_lookup,
        device_lookup=device_lookup,
    )
    for op in plan.ops:
        cost.add_packet(op.body_bytes)

    # Forward planner detail (wire_path, preset_key, etc.) onto the
    # cost dataclass so the WebUI's per-action info row sees the same
    # values the runner's ActionResult.detail would carry. Strip
    # planner-internal keys (e.g. nested child_plans) that the
    # ActionCost shouldn't carry into the API payload.
    detail = dict(plan.detail or {})
    detail.pop("child_plans", None)
    if action.get("kind") == "offset_group":
        # Preserve the legacy detail keys the WebUI cost badge already
        # reads (``wire_path``, ``offset_packets`` count, ``child_count``).
        detail.setdefault(
            "offset_packets", detail.get("offset_packets", 0),
        )
    cost.detail = detail
    return cost


def estimate_scene(scene: Mapping[str, Any], *,
                   known_group_ids: Optional[List[int]] = None,
                   rl_preset_lookup: Optional[RlPresetLookup] = None,
                   device_lookup: Optional[DeviceLookup] = None) -> SceneCost:
    """Estimate wire cost of a whole scene. ``per_action`` is in scene order."""
    out = SceneCost()
    for action in scene.get("actions") or []:
        cost = estimate_action(action,
                               known_group_ids=known_group_ids,
                               rl_preset_lookup=rl_preset_lookup,
                               device_lookup=device_lookup)
        out.per_action.append(cost)
        out.total.packets += cost.packets
        out.total.bytes += cost.bytes
        out.total.airtime_ms += cost.airtime_ms
        out.total.wall_clock_ms += cost.wall_clock_ms
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
#
# Every per-kind sizing helper used to live here as a parallel
# implementation of what the runner does at dispatch time. Post-2026-05-02
# the dispatch planner (``racelink/services/dispatch_planner.py``) is the
# single source of truth — it produces ``WireOp`` records with
# ``body_bytes`` already sized via the canonical builders, and both the
# runner and this estimator consume the same plan. The previous
# helpers (``_target_packet_multiplier``,
# ``_estimate_offset_group_cost``, ``_materialize_rl_preset_params``,
# ``_estimate_control_body_len``) are gone.
