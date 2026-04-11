"""Legacy compatibility shim for transport imports now living in ``racelink.transport``."""

try:
    from .racelink.transport import (
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        EV_TX_DONE,
        LP,
        LoRaUSB,
        mac_last3_from_hex,
        u16le,
    )
except ImportError:  # pragma: no cover
    from racelink.transport import (
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        EV_TX_DONE,
        LP,
        LoRaUSB,
        mac_last3_from_hex,
        u16le,
    )

_mac_last3_from_hex = mac_last3_from_hex
_u16le = u16le

__all__ = [
    "EV_ERROR",
    "EV_RX_WINDOW_CLOSED",
    "EV_RX_WINDOW_OPEN",
    "EV_TX_DONE",
    "LP",
    "LoRaUSB",
    "_mac_last3_from_hex",
    "_u16le",
]
