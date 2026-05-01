"""Streaming payload service for RaceLink (OPC_STREAM).

Thin wrapper around :meth:`GatewayService.send_stream`. Used by
:class:`StartblockService` to ship a startblock-program payload to
one or more devices; the gateway fragments the payload into
radio-sized packets and the receiver reassembles. The host's role
is just "submit one logical payload, collect ACKs from N
expected receivers".

Public API:

* ``send_stream(payload, groupId=None, device=None,
  retries=2, timeout_s=8.0) -> {"expected": N, "acked": M}`` —
  exactly one of ``groupId`` (broadcast to a group, expected
  count derived from the device repository) or ``device``
  (unicast, expected count = 1) must be set.

Threading: blocking. Callers run it in task-manager threads;
the underlying ``send_and_collect`` installs an RX listener for
the duration of the window.
"""

from __future__ import annotations

from . import rf_timing


class StreamService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    def send_stream(
        self,
        payload: bytes,
        groupId=None,
        device=None,
        retries: int = rf_timing.STREAM_MAX_ATTEMPTS - 1,
        timeout_s: float = rf_timing.STREAM_ATTEMPT_TIMEOUT_S,
    ) -> dict[str, int]:
        return self.gateway_service.send_stream(
            payload,
            groupId=groupId,
            device=device,
            retries=retries,
            timeout_s=timeout_s,
        )
