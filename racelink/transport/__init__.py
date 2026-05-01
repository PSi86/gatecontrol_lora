"""Transport adapters for serial, framing, and low-level gateway events."""

from .framing import mac_last3_from_hex, u16le
from .gateway_events import (
    EV_ERROR,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_ERROR,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_TX,
    GATEWAY_STATE_UNKNOWN,
    GW_CMD_IDENTIFY,
    GW_CMD_STATE_REQUEST,
    LP,
    TX_REJECT_OVERSIZE,
    TX_REJECT_REASON_NAME,
    TX_REJECT_TXPENDING,
    TX_REJECT_UNKNOWN,
    TX_REJECT_ZEROLEN,
)

__all__ = [
    "EV_ERROR",
    "EV_STATE_CHANGED",
    "EV_STATE_REPORT",
    "EV_TX_DONE",
    "EV_TX_REJECTED",
    "GATEWAY_STATE_ERROR",
    "GATEWAY_STATE_IDLE",
    "GATEWAY_STATE_NAME",
    "GATEWAY_STATE_RX",
    "GATEWAY_STATE_RX_WINDOW",
    "GATEWAY_STATE_TX",
    "GATEWAY_STATE_UNKNOWN",
    "GW_CMD_IDENTIFY",
    "GW_CMD_STATE_REQUEST",
    "LP",
    "TX_REJECT_OVERSIZE",
    "TX_REJECT_REASON_NAME",
    "TX_REJECT_TXPENDING",
    "TX_REJECT_UNKNOWN",
    "TX_REJECT_ZEROLEN",
    "GatewaySerialTransport",
    "mac_last3_from_hex",
    "u16le",
]


def __getattr__(name):
    if name == "GatewaySerialTransport":
        from .gateway_serial import GatewaySerialTransport

        return GatewaySerialTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
