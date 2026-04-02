from __future__ import annotations

import logging
from typing import Optional, Union

from RHUI import UIFieldSelectOption

from .core.app import RaceLinkApp
from .core.repository import InMemoryDeviceRepository
from .data import RL_Device
from .infrastructure.lora_transport_adapter import LoRaTransportAdapter
from .providers.mock_provider import MockRaceProvider
from .ui import RaceLinkUIMixin

# ---- lora proto registry (auto-generated from lora_proto.h) ----
try:
    from . import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .racelink_transport import LP
except Exception:
    from racelink_transport import LP

logger = logging.getLogger(__name__)


class RaceLink_LoRa(RaceLinkUIMixin):
    """RH-facing controller/facade delegating host-agnostic logic to RaceLinkApp."""

    def __init__(self, rhapi, name, label, repository: InMemoryDeviceRepository | None = None, race_provider=None):
        self._rhapi = rhapi
        self.name = name
        self.label = label
        self.lora = None
        self.ready = False
        self.action_reg_fn = None
        self.deviceCfgValid = False
        self.groupCfgValid = False
        self.uiDeviceList = None
        self.uiGroupList = None
        self.uiDiscoveryGroupList = None
        self.repository = repository or InMemoryDeviceRepository()

        self.transport_adapter = LoRaTransportAdapter(
            rhapi=self._rhapi,
            get_device_by_address=self.getDeviceFromAddress,
            on_status_update=self._on_status_update,
            on_identify_update=self._on_identify_update,
            on_disconnect=self._on_transport_disconnect,
            repository=self.repository,
        )

        self.app = RaceLinkApp(
            repository=self.repository,
            transport_port=self.transport_adapter,
            race_provider_port=race_provider or MockRaceProvider(),
            notify_fn=self.notify,
            config_getter=lambda key, default=None: self._rhapi.db.option(key, default),
            config_setter=lambda key, value: self._rhapi.db.option_set(key, value),
        )

        # Backward-compatible attribute access expected by UI/presentation modules.
        self.device_service = self.app.device_service
        self.control_service = self.app.control_service
        self.config_service = self.app.config_service
        self.startblock_service = self.app.startblock_service

        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiEffectList = [
            UIFieldSelectOption("01", "Red"),
            UIFieldSelectOption("02", "Green"),
            UIFieldSelectOption("03", "Blue"),
            UIFieldSelectOption("04", "White"),
            UIFieldSelectOption("05", "Yellow"),
            UIFieldSelectOption("06", "Cyan"),
            UIFieldSelectOption("07", "Magenta"),
            UIFieldSelectOption("10", "Blink Multicolor"),
            UIFieldSelectOption("11", "Pulse White"),
            UIFieldSelectOption("12", "Colorloop"),
            UIFieldSelectOption("13", "Blink RGB"),
            UIFieldSelectOption("20", "WLED Chaser"),
            UIFieldSelectOption("21", "WLED Chaser inverted"),
            UIFieldSelectOption("22", "WLED Rainbow"),
        ]

    def __getattr__(self, item):
        app = self.__dict__.get("app")
        if app and hasattr(app, item):
            return getattr(app, item)
        raise AttributeError(item)

    def onStartup(self, _args):
        self.app.load_from_db()
        self.uiDeviceList = self.createUiDevList()
        self.uiGroupList = self.createUiGroupList()
        self.uiDiscoveryGroupList = self.createUiGroupList(True)
        self.register_settings()
        self.register_quickset_ui()
        self.registerActions()
        self._rhapi.ui.broadcast_ui("settings")
        self._rhapi.ui.broadcast_ui("run")
        self.discoverPort({})

    def discoverPort(self, args):
        self.ready = self.transport_adapter.discover_port(args)
        self.lora = self.transport_adapter.lora

    def _on_status_update(self, ev: dict) -> None:
        sender3_hex = self._to_hex_str(ev.get("sender3"))
        dev = self.getDeviceFromAddress(sender3_hex) if sender3_hex else None
        if not dev:
            return
        dev.update_from_status(
            ev.get("flags"),
            ev.get("configByte"),
            ev.get("presetId"),
            ev.get("brightness"),
            ev.get("vbat_mV"),
            ev.get("node_rssi"),
            ev.get("node_snr"),
            ev.get("host_rssi"),
            ev.get("host_snr"),
        )

    def _on_identify_update(self, ev: dict, dev: RL_Device) -> None:
        mac6 = ev.get("mac6")
        dev.update_from_identify(
            ev.get("version"),
            ev.get("caps"),
            ev.get("groupId"),
            mac6,
            ev.get("host_rssi"),
            ev.get("host_snr"),
        )

    def _on_transport_disconnect(self) -> None:
        self.ready = False

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.transport_adapter.wait_rx_window(send_fn, collect_pred, fail_safe_s)

    def _opcode_name(self, opcode7: int) -> str:
        try:
            rule = LPA.find_rule(int(opcode7) & 0x7F)
        except Exception:
            rule = None
        if rule and getattr(rule, "name", None):
            return str(rule.name)
        return f"0x{int(opcode7) & 0x7F:02X}"

    def _log_lora_reply(self, ev: dict) -> None:
        try:
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            return

        sender3_hex = self._to_hex_str(ev.get("sender3")) or "??????"

        if opc == int(LP.OPC_ACK):
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            if ack_of is None or ack_status is None:
                return
            ack_name = self._opcode_name(int(ack_of))
            logger.debug(
                "ACK from %s: ack_of=%s (%s) status=%s seq=%s",
                sender3_hex,
                int(ack_of),
                ack_name,
                int(ack_status),
                ack_seq,
            )
            return

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def notify(self, msg: str) -> None:
        self._rhapi.ui.message_notify(msg)

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()

    def get_device_by_address(self, addr: str) -> Optional[RL_Device]:
        return self.getDeviceFromAddress(addr)
