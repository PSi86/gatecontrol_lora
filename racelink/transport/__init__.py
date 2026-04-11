"""Transport adapters for serial, framing, and low-level gateway events."""

from .framing import mac_last3_from_hex, u16le
from .gateway_events import EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN, EV_TX_DONE, LP

__all__ = [
    "EV_ERROR",
    "EV_RX_WINDOW_CLOSED",
    "EV_RX_WINDOW_OPEN",
    "EV_TX_DONE",
    "LP",
    "LoRaUSB",
    "mac_last3_from_hex",
    "u16le",
]


def __getattr__(name):
    if name == "LoRaUSB":
        from .gateway_serial import LoRaUSB

        return LoRaUSB
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
