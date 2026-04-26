from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Union

try:
    from racelink.core import HostApi
    from racelink.domain import (
        RL_Device,
        RL_DeviceGroup,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        build_specials_state,
        create_device,
        state_scope,
    )
    from racelink.services import (
        ConfigService,
        ControlService,
        DiscoveryService,
        GatewayService,
        StartblockService,
        StatusService,
        StreamService,
        SyncService,
    )
    from racelink.state import get_runtime_state_repository
    from racelink.state.migrations import migrate_state
    from racelink.state.persistence import (
        CURRENT_SCHEMA_VERSION,
        dump_records,
        dump_state,
        load_records,
        load_state,
        try_parse_legacy_repr,
    )
    from racelink.transport import GatewaySerialTransport, LP, mac_last3_from_hex
except ImportError:  # pragma: no cover - compatibility path for package-style plugin loading
    from .racelink.core import HostApi
    from .racelink.domain import (
        RL_Device,
        RL_DeviceGroup,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        build_specials_state,
        create_device,
        state_scope,
    )
    from .racelink.services import (
        ConfigService,
        ControlService,
        DiscoveryService,
        GatewayService,
        StartblockService,
        StatusService,
        StreamService,
        SyncService,
    )
    from .racelink.state import get_runtime_state_repository
    from .racelink.state.migrations import migrate_state
    from .racelink.state.persistence import (
        CURRENT_SCHEMA_VERSION,
        dump_records,
        dump_state,
        load_records,
        load_state,
        try_parse_legacy_repr,
    )
    from .racelink.transport import GatewaySerialTransport, LP, mac_last3_from_hex

logger = logging.getLogger(__name__)


# Structured gateway-error codes surfaced in ``last_gateway_error.code``.
# WebUI consumers (and log aggregators) can route on the code instead of
# pattern-matching the free-form ``reason`` text.
GW_ERR_NOT_FOUND = "NOT_FOUND"   # no matching USB-serial gateway present
GW_ERR_PORT_BUSY = "PORT_BUSY"   # port exists but held by another process
GW_ERR_LINK_LOST = "LINK_LOST"   # transport disconnected after being ready
GW_ERR_HOST_ERROR = "HOST_ERROR"  # catch-all (unexpected local failure)

# Exp-backoff schedule (seconds) for automatic gateway retries. The last entry
# is clamped, i.e. any attempt >= len(schedule) uses the final value.
_GATEWAY_RETRY_BACKOFF_S = (2.0, 5.0, 10.0, 20.0, 30.0)


def classify_gateway_error(reason: str, *, fallback: str = GW_ERR_HOST_ERROR) -> str:
    """Map a free-form gateway error message to a structured code.

    We prefer sniffing the message text over wrapping ``serial.SerialException``
    because the same strings are already raised by ``discover_and_open`` and
    surfaced through several code paths (``schedule_reconnect``, manual retry,
    startup). Returning ``fallback`` keeps unexpected errors visible without
    hiding them behind the retry machinery.
    """
    text = str(reason or "").lower()
    if not text:
        return fallback
    if "no racelink gateway" in text or "not found" in text or "no device" in text:
        return GW_ERR_NOT_FOUND
    if (
        "exclusive lock" in text
        or "could not exclusively lock" in text
        or "resource temporarily unavailable" in text
        or "port busy" in text
    ):
        return GW_ERR_PORT_BUSY
    if "disconnect" in text or "link lost" in text or "read error" in text:
        return GW_ERR_LINK_LOST
    return fallback


class RaceLink_Host:
    """Host controller coordinating runtime state, transport, and core services."""

    def __init__(
        self,
        host_api: "HostApi",
        name: str,
        label: str,
        state_repository=None,
    ):
        # The embedding host (RotorHazard plugin or standalone shim) must
        # satisfy the ``HostApi`` Protocol from ``racelink.core.host_api``.
        # The attribute is exposed as ``_host_api`` so plugin-specific names
        # do not leak into the Host codebase.
        self._host_api = host_api
        self.name = name
        self.label = label
        self.state_repository = state_repository or get_runtime_state_repository()
        self.transport = None
        self.ready = False
        self.deviceCfgValid = False
        self.groupCfgValid = False

        # Transport-level pending expectation (for online/offline determination).
        # Mutated from two threads:
        #   * the TX-listener path (``GatewayService.on_transport_tx``)
        #     stamps a new expectation when an outbound unicast goes
        #     out — runs on whatever thread called ``_send_m2n``;
        #   * the RX-reader path (``pending_try_match`` /
        #     ``pending_window_closed``) reads the expectation and
        #     clears it on a matching reply or window-closed.
        # ``_pending_expect_lock`` keeps the read+clear atomic so a TX
        # thread cannot wedge a new expectation between an RX-thread
        # snapshot and its clear (lost-update). The clear helpers below
        # implement compare-and-clear semantics so a stale matcher
        # cannot wipe a freshly-stamped expectation either.
        self._pending_expect: Optional[dict] = None
        self._pending_expect_lock = threading.Lock()

        self._transport_hooks_installed = False
        # ``_pending_config`` is mutated from two threads:
        # the web request thread (``GatewayService.send_config`` stashes the
        # outgoing option/data0 keyed by recv3) and the RX reader thread
        # (``handle_ack_event`` pops the entry on a successful ACK). On
        # CPython a same-key write+pop race can lose the update silently;
        # any future iterator over the dict could also raise
        # ``RuntimeError: dictionary changed size during iteration``.
        # ``_pending_config_lock`` is held only across the dict mutation
        # itself — the long-running follow-up (``_apply_config_update``)
        # runs outside the lock so we never block the RX thread on it.
        self._pending_config: dict = {}
        self._pending_config_lock = threading.Lock()
        self._task_manager = None
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0
        # Plan P1-1: persistent gateway-failure state surfaced via /api/master
        # even when no user was driving the connection attempt.
        self.last_gateway_error: dict | None = None
        self._gateway_failure_count: int = 0
        # Auto-retry state. PORT_BUSY and LINK_LOST schedule an exp-backoff
        # retry. NOT_FOUND never auto-retries (hardware absent). The attempt
        # counter feeds the exponential delay and is reset on success or on
        # a manual retry.
        self._gateway_retry_timer: Optional[threading.Timer] = None
        self._gateway_retry_attempt: int = 0
        # Startup-grace: the first discoverPort() runs before the user is even
        # able to click anything. Marking it as ``auto`` suppresses the RH
        # UI ERROR-alert path; subsequent auto-retries stay in the same mode.
        self._startup_done: bool = False
        # Link-recovery: once the gateway was ready at least once, treat any
        # subsequent ``NOT_FOUND`` as ``LINK_LOST`` so the auto-retry machinery
        # keeps polling until the dongle re-appears (USB unplug + replug).
        # Cleared on successful connect and on manual retry.
        self._link_recovery_pending: bool = False
        # Plan P2-2: plugins register a callback to refresh their panels after
        # state is persisted instead of monkey-patching load/save_to_db.
        self.on_persistence_changed = None
        # Plan P1-1: consumers (SSE layer, plugin UI) register a callback here
        # so a ready/last_error change produces a push notification rather
        # than requiring polling.
        self.on_gateway_status_changed = None
        # Plan P1-2: dispose transport cleanly when the host plugin unloads.
        self._shutdown_called: bool = False
        # WLED preset list (numeric ids -> labels). Pre-rename: ``uiEffectList``
        # — the entries are preset ids, not WLED effect-mode indices.
        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiPresetList = [
            {"value": "01", "label": "Red"},
            {"value": "02", "label": "Green"},
            {"value": "03", "label": "Blue"},
            {"value": "04", "label": "White"},
            {"value": "05", "label": "Yellow"},
            {"value": "06", "label": "Cyan"},
            {"value": "07", "label": "Magenta"},
            {"value": "10", "label": "Blink Multicolor"},
            {"value": "11", "label": "Pulse White"},
            {"value": "12", "label": "Colorloop"},
            {"value": "13", "label": "Blink RGB"},
            {"value": "20", "label": "WLED Chaser"},
            {"value": "21", "label": "WLED Chaser inverted"},
            {"value": "22", "label": "WLED Rainbow"},
        ]
        self.gateway_service = GatewayService(self)
        self.control_service = ControlService(self, self.gateway_service)
        self.config_service = ConfigService(self, self.gateway_service)
        self.discovery_service = DiscoveryService(self, self.gateway_service)
        self.status_service = StatusService(self, self.gateway_service)
        self.stream_service = StreamService(self, self.gateway_service)
        self.startblock_service = StartblockService(self, self.stream_service)
        self.sync_service = SyncService(self, self.gateway_service)

    def _option(self, key: str, default=None):
        return self._host_api.db.option(key, default)

    def _option_set(self, key: str, value) -> None:
        self._host_api.db.option_set(key, value)

    def _translate(self, text: str) -> str:
        return self._host_api.__(text)

    def _notify(self, message: str) -> None:
        ui = getattr(self._host_api, "ui", None)
        notify = getattr(ui, "message_notify", None) if ui else None
        if callable(notify):
            notify(message)

    def _broadcast_ui(self, panel: str) -> None:
        ui = getattr(self._host_api, "ui", None)
        broadcaster = getattr(ui, "broadcast_ui", None) if ui else None
        if callable(broadcaster):
            broadcaster(panel)

    def attach_task_manager(self, task_manager) -> None:
        self._task_manager = task_manager

    def is_discovery_active(self) -> bool:
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is None:
            return False
        try:
            snap = task_manager.snapshot()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return False
        if not snap:
            return False
        return bool(snap.get("state") == "running" and snap.get("name") == "discover")

    @property
    def device_repository(self):
        return self.state_repository.devices

    @property
    def group_repository(self):
        return self.state_repository.groups

    @property
    def backup_device_repository(self):
        return self.state_repository.backup_devices

    @property
    def backup_group_repository(self):
        return self.state_repository.backup_groups

    def onStartup(self, _args) -> None:
        self.load_from_db()
        # First-ever gateway probe runs before the user can interact. Tag it
        # as ``auto`` so a bad outcome stays at WARNING and does not trip the
        # RotorHazard log-to-UI-alert bridge. Auto-retry machinery takes over
        # from there for PORT_BUSY / LINK_LOST.
        self.discoverPort({}, origin="auto")
        self._startup_done = True

    def save_to_db(self, args, scopes=None) -> None:
        """Persist devices + groups atomically under a single combined key.

        Writing both payloads together eliminates the partial-state hazard we
        used to have with separate ``rl_device_config`` / ``rl_groups_config``
        writes (see plan P1-5). The legacy keys are left untouched so an
        operator can roll back to an older Host build without losing data.

        ``scopes`` describes which user-visible state was mutated and is
        forwarded to ``on_persistence_changed`` so plugins can avoid rebuilding
        panels that are not affected. Callers that do not know the scope
        should omit the argument, which falls back to ``{FULL}`` for
        backwards-compatibility.
        """
        logger.debug("RL: Writing current states to Database (combined)")
        groups_to_dump = self.group_repository.list()
        if len(groups_to_dump) < len(self.backup_group_repository.list()):
            groups_to_dump = self.backup_group_repository.list()
        config_str_state = dump_state(
            self.device_repository.list(),
            groups_to_dump,
            schema_version=CURRENT_SCHEMA_VERSION,
        )
        self._option_set("rl_state_v1", config_str_state)
        self._fire_persistence_changed(scopes)

    def _fire_persistence_changed(self, scopes=None) -> None:
        """Invoke ``on_persistence_changed`` with a scope set, tolerating old signatures."""
        on_changed = getattr(self, "on_persistence_changed", None)
        if not callable(on_changed):
            return
        resolved = state_scope.normalize_scopes(scopes)
        try:
            on_changed(resolved)
        except TypeError:
            try:
                on_changed()
            except Exception:
                logger.exception("RaceLink: on_persistence_changed callback failed")
        except Exception:
            logger.exception("RaceLink: on_persistence_changed callback failed")

    def _load_from_legacy_keys(self):
        """Fall back to the pre-P1-5 per-key storage.

        Plan P1-3: if a legacy key contains pre-JSON Python-repr text (from
        very old Host builds that used ``ast.literal_eval``), attempt a one-
        shot migration via :func:`try_parse_legacy_repr`. The combined-key
        save triggered afterwards by ``load_from_db`` replaces both legacy
        keys, so this path runs at most once per deployment.
        """
        config_str_devices = self._option("rl_device_config", None)
        config_str_groups = self._option("rl_groups_config", None)
        if config_str_devices is None and config_str_groups is None:
            return None, None, True  # untouched; initialize from backups

        devices = self._load_legacy_records(
            config_str_devices,
            source="rl_device_config",
            backup=self.backup_device_repository.list(),
        )
        groups = self._load_legacy_records(
            config_str_groups,
            source="rl_groups_config",
            backup=self.backup_group_repository.list(),
        )
        return devices, groups, False

    def _load_legacy_records(self, raw, *, source: str, backup) -> list[dict]:
        """JSON first; if that warns, try the Python-repr migration once."""
        default = [obj.__dict__ for obj in backup]
        if raw in (None, ""):
            return default

        text = str(raw).strip()
        if text == "":
            return default
        # Cheap pre-check: JSON lists use double quotes; Python-repr uses single.
        looks_like_json = text.startswith("[{\"") or text.startswith("[{") and '"' in text[:40]
        if looks_like_json:
            return load_records(raw, default=default, source=source)

        salvaged = try_parse_legacy_repr(raw)
        if salvaged is not None:
            logger.warning(
                "RaceLink: migrated legacy Python-repr payload in %s (%d records); "
                "combined key will be written on next save.",
                source,
                len(salvaged),
            )
            return salvaged
        # Final fallback: let load_records log the warning and use the default.
        return load_records(raw, default=default, source=source)

    def load_from_db(self) -> None:
        logger.debug("RL: Applying config from Database")

        combined_raw = self._option("rl_state_v1", None)
        config_list_devices: list[dict]
        config_list_groups: list[dict]
        needs_migration_save = False

        if combined_raw in (None, ""):
            legacy_devices, legacy_groups, fresh_install = self._load_from_legacy_keys()
            if fresh_install:
                # No record at all -> initialize from backup defaults.
                config_list_devices = [obj.__dict__ for obj in self.backup_device_repository.list()]
                config_list_groups = [obj.__dict__ for obj in self.backup_group_repository.list()]
            else:
                config_list_devices = legacy_devices or []
                config_list_groups = legacy_groups or []
            needs_migration_save = True
            loaded_version = 0
        else:
            config_list_devices, config_list_groups, loaded_version = load_state(
                combined_raw,
                default_devices=[obj.__dict__ for obj in self.backup_device_repository.list()],
                default_groups=[obj.__dict__ for obj in self.backup_group_repository.list()],
                source="rl_state_v1",
            )
            if loaded_version == 0:
                # Combined key existed but was malformed; try legacy as a rescue.
                legacy_devices, legacy_groups, fresh_install = self._load_from_legacy_keys()
                if not fresh_install:
                    logger.warning(
                        "RaceLink: combined state unreadable; recovered from legacy keys"
                    )
                    config_list_devices = legacy_devices or []
                    config_list_groups = legacy_groups or []
                needs_migration_save = True

        config_list_devices, config_list_groups, loaded_version = migrate_state(
            list(config_list_devices),
            list(config_list_groups),
            from_version=loaded_version,
        )
        if loaded_version < CURRENT_SCHEMA_VERSION:
            needs_migration_save = True

        logger.debug(
            "RL: Loaded %d devices and %d groups (schema_version=%s)",
            len(config_list_devices),
            len(config_list_groups),
            loaded_version,
        )
        loaded_devices = []

        for device in config_list_devices:
            logger.debug(device)
            try:
                flags = device.get("flags", None)
                preset_id = device.get("presetId", None)

                if flags is None:
                    legacy_state = int(device.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in device:
                        flags |= RL_FLAG_HAS_BRI

                if preset_id is None:
                    preset_id = int(device.get("effect", 1) or 1)

                brightness = int(device.get("brightness", 70) or 0)

                dev_type = device.get("dev_type", None)
                if dev_type is None:
                    dev_type = device.get("device_type", None)
                if dev_type is None:
                    dev_type = device.get("caps", device.get("type", 0))

                special_state = build_specials_state(int(dev_type or 0), device)
                loaded_devices.append(
                    create_device(
                        addr=str(device.get("addr", "")).upper(),
                        dev_type=int(dev_type or 0),
                        name=str(device.get("name", "")),
                        groupId=int(device.get("groupId", 0) or 0),
                        version=int(device.get("version", 0) or 0),
                        caps=int(dev_type or 0),
                        flags=int(flags) & 0xFF,
                        presetId=int(preset_id) & 0xFF,
                        brightness=brightness & 0xFF,
                        specials=special_state,
                    )
                )
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)
                continue
        self.device_repository.replace_all(loaded_devices)

        if not config_list_groups:
            config_list_groups = [obj.__dict__ for obj in self.backup_group_repository.list()]

        loaded_groups = []
        for group in config_list_groups:
            logger.debug(group)
            group_dev_type = group.get("dev_type", group.get("device_type", 0))
            loaded_groups.append(RL_DeviceGroup(group["name"], group["static_group"], group_dev_type))

        loaded_groups = [
            group
            for group in loaded_groups
            if str(getattr(group, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(group, "name", "")).strip().lower() == "all wled nodes" for group in loaded_groups):
            loaded_groups.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for group in loaded_groups:
                if str(getattr(group, "name", "")).strip().lower() == "all wled nodes":
                    group.name = "All WLED Nodes"
                    group.static_group = 1
                    group.dev_type = 0
        self.group_repository.replace_all(loaded_groups)

        if needs_migration_save:
            try:
                self.save_to_db({}, scopes={state_scope.FULL})
            except Exception:
                logger.exception("RaceLink: failed to persist migrated state")
        else:
            # save_to_db fires this naturally; make sure it also fires for a
            # plain load so plugins can refresh panels (plan P2-2).
            self._fire_persistence_changed({state_scope.FULL})

    def discoverPort(self, args, *, origin: Optional[str] = None) -> None:
        """Initialize the active gateway transport.

        ``origin`` describes who initiated the attempt and controls logging /
        UI notifications:
        - ``manual`` (default when ``args`` contains ``"manual"``): toast the
          result and escalate failures to ERROR.
        - ``auto``: scheduled from the background auto-retry timer or the very
          first startup probe -- silent, WARNING-level on failure.
        - ``programmatic``: any other caller (legacy).

        Persistent failure state (``ready``, ``last_gateway_error``) is tracked
        in all cases so the UI can render its banner without relying on
        toasts.
        """
        if origin is None:
            origin = "manual" if "manual" in args else "programmatic"
        port = self._option("psi_comms_port", None)

        # Always release the previous transport before building a new one.
        # Skipping this step means two ``GatewaySerialTransport`` instances
        # fight over the same OS file descriptor: the old one keeps the
        # exclusive lock while the new one's ``discover_and_open`` walks
        # the port list, making every port look busy. That in turn was the
        # source of the manual-retry-after-auto-recovery regression
        # (user saw ``NOT_FOUND`` although the gateway was already wired up).
        old_transport = self.transport
        self.transport = None
        if old_transport is not None:
            try:
                close = getattr(old_transport, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.debug("RaceLink: error closing previous transport", exc_info=True)

        try:
            self._transport_hooks_installed = False
            self.transport = GatewaySerialTransport(port=port, on_event=None)
            ok = self.transport.discover_and_open()
            if ok:
                self.transport.start()
                self.ready = True
                self._link_recovery_pending = False
                self._clear_gateway_error()
                self._install_transport_hooks()
                used = self.transport.port or "unknown"
                mac = getattr(self.transport, "ident_mac", None)
                if mac:
                    logger.info("RaceLink Gateway ready on %s with MAC: %s", used, mac)
                    if origin == "manual":
                        self._notify(self._translate("RaceLink Gateway ready on {} with MAC: {}").format(used, mac))
                return
            # ``discover_and_open`` returned False. Distinguish between "no
            # matching device present" (NOT_FOUND) and "device is there but
            # locked by another process" (PORT_BUSY).
            if getattr(self.transport, "last_discovery_had_busy_port", False):
                reason = (
                    "RaceLink Gateway port busy: another process still holds "
                    "an exclusive lock. Retrying automatically."
                )
                self._record_gateway_error(
                    reason=reason, origin=origin, code=GW_ERR_PORT_BUSY,
                )
                if origin == "manual":
                    self._notify(self._translate(reason))
                return
            reason = "No RaceLink Gateway module discovered or configured"
            self._record_gateway_error(
                reason=reason, origin=origin, code=GW_ERR_NOT_FOUND,
            )
            if origin == "manual":
                self._notify(self._translate(reason))
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            self._record_gateway_error(reason=str(ex), origin=origin)
            if origin == "manual":
                self._notify(self._translate("Failed to initialize communicator: {}").format(str(ex)))

    def _record_gateway_error(self, *, reason: str, origin: str, code: Optional[str] = None) -> None:
        self.ready = False
        self._gateway_failure_count += 1
        resolved_code = code or classify_gateway_error(reason)

        # Once a connection has been established in this session, a follow-up
        # NOT_FOUND almost always means the user pulled the USB cable. Treat
        # it as LINK_LOST so the backoff timer keeps polling until the dongle
        # re-appears.
        if resolved_code == GW_ERR_NOT_FOUND and self._link_recovery_pending:
            resolved_code = GW_ERR_LINK_LOST

        # Decide whether to auto-retry. PORT_BUSY clears itself once the other
        # process releases the lock; LINK_LOST often clears once the dongle is
        # re-seated. NOT_FOUND does not, so we do not hammer the system for
        # absent hardware.
        auto_eligible = resolved_code in {GW_ERR_PORT_BUSY, GW_ERR_LINK_LOST}
        next_retry_in_s: Optional[float] = None
        if auto_eligible:
            idx = min(self._gateway_retry_attempt, len(_GATEWAY_RETRY_BACKOFF_S) - 1)
            next_retry_in_s = _GATEWAY_RETRY_BACKOFF_S[idx]

        self.last_gateway_error = {
            "ts": time.time(),
            "reason": str(reason),
            "origin": origin,
            "code": resolved_code,
            "failure_count": int(self._gateway_failure_count),
            "next_retry_in_s": next_retry_in_s,
        }

        # Only manual retries escalate to ERROR -- automatic / startup probes
        # that naturally fail should not spam the RotorHazard log-to-UI
        # bridge. A dongle that is merely unplugged at boot stays at WARNING.
        if origin == "manual":
            logger.error(
                "Gateway transport unavailable (origin=%s, code=%s, attempt=%s): %s",
                origin, resolved_code, self._gateway_failure_count, reason,
            )
        else:
            logger.warning(
                "Gateway transport unavailable (origin=%s, code=%s, attempt=%s): %s",
                origin, resolved_code, self._gateway_failure_count, reason,
            )

        if auto_eligible and next_retry_in_s is not None and not self._shutdown_called:
            self._schedule_gateway_retry(next_retry_in_s)

        self._notify_gateway_status()

    def _clear_gateway_error(self) -> None:
        was_unready = self.last_gateway_error is not None or not self.ready
        self.last_gateway_error = None
        self._gateway_failure_count = 0
        self._gateway_retry_attempt = 0
        self._cancel_gateway_retry()
        if was_unready:
            self._notify_gateway_status()

    def _schedule_gateway_retry(self, delay_s: float) -> None:
        """Arm a one-shot auto-retry of ``discoverPort`` after ``delay_s``.

        Only one timer is ever active. The retry increments
        ``_gateway_retry_attempt`` so the next scheduled delay progresses
        through the backoff schedule even if the current attempt fails
        quickly.
        """
        self._cancel_gateway_retry()
        attempt_next = self._gateway_retry_attempt + 1

        def _fire() -> None:
            if self._shutdown_called:
                return
            self._gateway_retry_attempt = attempt_next
            try:
                self.discoverPort({}, origin="auto")
            except Exception:
                logger.exception("RaceLink: auto-retry discoverPort raised")

        timer = threading.Timer(float(delay_s), _fire)
        timer.daemon = True
        self._gateway_retry_timer = timer
        timer.start()

    def _cancel_gateway_retry(self) -> None:
        timer = self._gateway_retry_timer
        self._gateway_retry_timer = None
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:
            logger.debug("RaceLink: error cancelling gateway retry timer", exc_info=True)

    def _notify_gateway_status(self) -> None:
        cb = getattr(self, "on_gateway_status_changed", None)
        if not callable(cb):
            return
        try:
            cb(self.gateway_status())
        except Exception:
            logger.exception("RaceLink: on_gateway_status_changed callback failed")

    def gateway_status(self) -> dict:
        """Return a JSON-serialisable gateway-readiness snapshot (plan P1-1)."""
        return {
            "ready": bool(self.ready),
            "last_error": dict(self.last_gateway_error) if self.last_gateway_error else None,
            "failure_count": int(self._gateway_failure_count),
            "retry_attempt": int(self._gateway_retry_attempt),
        }

    def retry_gateway(self) -> dict:
        """User-driven retry; uses the manual-origin path so toasts still fire."""
        # Cancel any pending auto-retry and reset the exponential schedule --
        # the user just told us to try NOW, and the next failure should start
        # over at the shortest delay. Clearing ``_link_recovery_pending`` lets
        # the user escape a stuck LINK_LOST loop if they know the hardware is
        # truly gone and want to see the plain NOT_FOUND message again.
        self._cancel_gateway_retry()
        self._gateway_retry_attempt = 0
        self._link_recovery_pending = False
        self.discoverPort({"manual"}, origin="manual")
        return self.gateway_status()

    def shutdown(self) -> None:
        """Release the serial transport and flush persisted state (plan P1-2).

        Safe to call multiple times. Intended for plugin-unload / process-exit.
        """
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self._cancel_gateway_retry()
        transport = self.transport
        self.transport = None
        if transport is not None:
            try:
                close = getattr(transport, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.exception("RaceLink: error closing transport during shutdown")
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is not None:
            try:
                cancel = getattr(task_manager, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception:
                logger.exception("RaceLink: error cancelling task manager during shutdown")
        try:
            self.save_to_db({}, scopes={state_scope.NONE})
        except Exception:
            logger.exception("RaceLink: error persisting state during shutdown")
        self.ready = False

    def onRaceStart(self, _args) -> None:
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args) -> None:
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args) -> None:
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args) -> None:
        logger.warning("Event onSendMessage")

    def getDevices(
        self,
        groupFilter: int = 255,
        targetDevice: Optional[RL_Device] = None,
        addToGroup: int = -1,
    ) -> int:
        result = self.discovery_service.discover_devices(
            group_filter=groupFilter,
            target_device=targetDevice,
            add_to_group=addToGroup,
        )
        found = int(result.get("found", 0) or 0)
        # Plan P2-8: `_notify` already handles the "no ui" case, so the local
        # hasattr guards are redundant -- drop them.
        if 0 < addToGroup < 255:
            msg = "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
        else:
            msg = "Device Discovery finished with {} devices found.".format(found)
        self._notify(msg)
        return found

    def getStatus(
        self,
        groupFilter: int = 255,
        targetDevice: Optional[RL_Device] = None,
    ) -> int:
        result = self.status_service.get_status(group_filter=groupFilter, target_device=targetDevice)
        return int(result.get("updated", 0) or 0)

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        transport = getattr(self, "transport", None)
        if transport is None:
            logger.warning("setNodeGroupId: communicator not ready")
            return False

        self._install_transport_hooks()

        recv3 = mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            transport.send_set_group(recv3, group_id)

        if not wait_for_ack or is_broadcast:
            _send()
            return True

        events, _ = self._send_and_wait_for_reply(recv3, LP.OPC_SET_GROUP, _send, timeout_s=8.0)
        if not events:
            logger.warning("No ACK_OK for SET_GROUP to %s (timeout)", targetDevice.addr)
            return False

        ev = events[-1]
        ok = int(ev.get("ack_status", 1)) == 0
        if not ok:
            logger.warning(
                "No ACK_OK for SET_GROUP to %s (status=%s, opcode=%s)",
                targetDevice.addr,
                ev.get("ack_status"),
                ev.get("ack_of"),
            )
        return ok

    def forceGroups(self, args=None, sanityCheck: bool = True) -> None:
        logger.debug("Forcing all known devices to their stored groups.")
        num_groups = len(self.group_repository.list())

        for device in self.device_repository.list():
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)

    def _require_transport(self, context: str):
        if getattr(self, "transport", None):
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    @staticmethod
    def _coerce_control_values(flags, preset_id, brightness, *, fallback: RL_Device | None = None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    def _update_group_control_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        # A6: ``device_repository.list()`` returns the *live* storage.
        # Iterating it while another thread mutates the device list (a
        # gateway IDENTIFY can append; a delete can remove) used to risk
        # ``RuntimeError: list changed size during iteration``. The
        # ``state_repository.lock`` is a reentrant lock, so any caller
        # already holding it (e.g. the SSE refresh path) re-acquires
        # without deadlock.
        with self.state_repository.lock:
            for device in self.device_repository.list():
                try:
                    if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                        continue
                    device.flags = flags
                    device.presetId = preset_id
                    device.brightness = brightness
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    continue

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        """Compatibility entrypoint forwarding a fixed preset-id send to the
        control service (OPC_PRESET). Low-level shim kept for legacy callers."""
        return self.control_service.send_device_preset(targetDevice, flags, presetId, brightness)

    def sendGroupPreset(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        """Broadcast a preset id to a group (OPC_PRESET)."""
        return self.control_service.send_group_preset(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendWledPreset(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a classical WLED preset (OPC_PRESET). Pre-rename: ``sendWledControl``."""
        return self.control_service.send_wled_preset(
            targetDevice=targetDevice, targetGroup=targetGroup, params=params,
        )

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a RaceLink-native preset (OPC_CONTROL) by its stable int id.

        Phase D: this is the Specials/WebUI entry point for the "WLED Control"
        action. ``params`` carries only ``{presetId, brightness}`` — full
        14-parameter editing lives in the RL-preset editor, not here. The raw
        direct-parameter send stays available on ``ControlService`` for
        internal callers (``send_rl_preset_by_id`` uses it to dispatch
        OPC_CONTROL with the resolved snapshot).
        """
        params = params or {}
        preset_id = int(params.get("presetId", 0))
        brightness = params.get("brightness")
        return self.control_service.send_rl_preset_by_id(
            preset_id,
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            brightness_override=int(brightness) if brightness is not None else None,
        )

    def sendRlPresetById(
        self,
        preset_id,
        *,
        targetDevice=None,
        targetGroup=None,
        brightness_override=None,
    ):
        """Apply a RL-preset snapshot (stable int id) via ControlService.

        RotorHazard quickset / default group action entry point. The service
        loads the persisted params through ``rl_presets_service`` and sends
        ``OPC_CONTROL``. WLED presets keep their own path via
        :meth:`sendWledPreset`.
        """
        return self.control_service.send_rl_preset_by_id(
            preset_id,
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            brightness_override=brightness_override,
        )

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock config to StartblockService."""
        return self.startblock_service.send_startblock_config(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def runScene(self, scene_key, *, progress_cb=None):
        """Run a scene by key. Wired by ``RaceLinkApp`` factory; falls back to
        an explicit error result when the runner is not yet attached so the RH
        plugin's ``RaceLink Scene`` ActionEffect degrades gracefully on a
        partially-initialised controller.

        ``progress_cb`` (kwarg-only) forwards to the runner so the WebUI's
        synchronous ``/api/scenes/<key>/run`` route can broadcast SSE
        progress events. The RH plugin's ``applyScene`` path doesn't pass
        the kwarg, so its behaviour is unchanged.
        """
        runner = getattr(self, "scene_runner_service", None)
        if runner is None:
            from racelink.services.scene_runner_service import SceneRunResult
            return SceneRunResult(scene_key=str(scene_key), ok=False, error="runner_not_wired")
        return runner.run(str(scene_key), progress_cb=progress_cb)

    def _is_startblock_device(self, dev: RL_Device) -> bool:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.is_startblock_device(dev)

    def _iter_startblock_devices(self, *, targetDevice=None, targetGroup=None) -> list[RL_Device]:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.iter_startblock_devices(
            target_device=targetDevice,
            target_group=targetGroup,
        )

    def get_current_heat_slot_list(self):
        """Compatibility helper forwarding heat-slot lookup to the active source adapter."""
        return self.startblock_service.get_current_heat_slot_list()

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock dispatch to StartblockService."""
        return self.startblock_service.send_startblock_control(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def _normalize_startblock_slot_list(self, slot_list):
        """Compatibility helper forwarding slot normalization to StartblockService."""
        return self.startblock_service.normalize_slot_list(slot_list)

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = 8.0,
    ) -> tuple[list[dict], bool]:
        return self.gateway_service.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s=timeout_s)

    def sendConfig(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: float = 6.0,
    ):
        """Compatibility entrypoint forwarding config writes to ConfigService."""
        return self.config_service.send_config(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        """Compatibility hook forwarding ACK-side config updates to ConfigService."""
        return self.config_service.apply_config_update(dev, option, data0)

    def stash_pending_config(self, recv3_hex: str, option: int, data0: int) -> None:
        """Record the option/data0 of an in-flight ``OPC_CONFIG`` keyed by
        the receiver's last-3 MAC bytes (uppercase hex).

        Called by ``GatewayService.send_config`` on the web/scene-runner
        side just before the transport write. The matching pop happens on
        the RX reader thread inside ``handle_ack_event`` once the gateway
        ACKs the config. The dedicated ``_pending_config_lock`` keeps the
        write+pop atomic without touching the broader state-repository
        lock, so a stalled RX handler cannot delay device-list mutations
        and vice versa.
        """
        with self._pending_config_lock:
            self._pending_config[recv3_hex] = {
                "option": int(option) & 0xFF,
                "data0": int(data0) & 0xFF,
            }

    def take_pending_config(self, recv3_hex: str) -> Optional[dict]:
        """Pop and return the recorded config payload for ``recv3_hex``.

        Returns ``None`` when no pending entry exists (e.g. broadcast
        ACK, duplicate ACK, or an entry that was already consumed).
        Held under the same lock as ``stash_pending_config``.
        """
        with self._pending_config_lock:
            return self._pending_config.pop(recv3_hex, None)

    def set_pending_expect(self, dev, rule, opcode7: int, sender_last3: str, ts: float) -> None:
        """Stamp a pending unicast expectation. Called from the TX
        listener path right after a unicast request is on the wire."""
        with self._pending_expect_lock:
            self._pending_expect = {
                "dev": dev,
                "rule": rule,
                "opcode7": int(opcode7),
                "sender_last3": str(sender_last3 or "").upper(),
                "ts": float(ts),
            }

    def read_pending_expect(self) -> Optional[dict]:
        """Return the current pending-expect dict (the live reference,
        not a copy). Callers must treat it as read-only and use
        :meth:`clear_pending_expect_if` for compare-and-clear semantics
        — clearing without the reference check would let a stale RX
        matcher wipe a freshly-stamped expectation from the TX thread.
        """
        with self._pending_expect_lock:
            return self._pending_expect

    def clear_pending_expect_if(self, expected: Optional[dict]) -> bool:
        """Atomic compare-and-clear: clear ``_pending_expect`` only if
        it is still the same object reference as ``expected``. Returns
        True on a successful clear, False if the value has changed
        (i.e. a new TX-side stamp arrived in the meantime).

        This is the safe partner of :meth:`read_pending_expect` for the
        RX-thread "I matched the reply, drop the expectation" path —
        prevents the lost-update where the RX thread reads ``p``, the
        TX thread immediately stamps a new expectation, and the RX
        thread's clear wipes it.
        """
        with self._pending_expect_lock:
            if self._pending_expect is expected:
                self._pending_expect = None
                return True
            return False

    def clear_pending_expect(self) -> None:
        """Unconditional clear. Used by paths that own the lifetime of
        the expectation (e.g. shutdown / reconnect) and are intentionally
        wiping any in-flight state. Most timeout/match callers should
        prefer :meth:`clear_pending_expect_if`.
        """
        with self._pending_expect_lock:
            self._pending_expect = None

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        """Compatibility entrypoint forwarding sync packets to SyncService."""
        return self.sync_service.send_sync(ts24, brightness, recv3=recv3)

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        """Compatibility entrypoint forwarding payload streams to StreamService."""
        return self.stream_service.send_stream(payload, groupId=groupId, device=device, retries=retries, timeout_s=timeout_s)

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.gateway_service.wait_rx_window(send_fn, collect_pred=collect_pred, fail_safe_s=fail_safe_s)

    def _opcode_name(self, opcode7: int) -> str:
        return self.gateway_service.opcode_name(opcode7)

    def _log_transport_reply(self, ev: dict) -> None:
        return self.gateway_service.log_transport_reply(ev)

    def _log_rx_window_event(self, ev: dict) -> None:
        return self.gateway_service.log_rx_window_event(ev)

    def _handle_ack_event(self, ev: dict) -> None:
        return self.gateway_service.handle_ack_event(ev)

    def _install_transport_hooks(self) -> None:
        return self.gateway_service.install_transport_hooks()

    def _on_transport_tx(self, ev: dict) -> None:
        return self.gateway_service.on_transport_tx(ev)

    def _on_transport_event_gc(self, ev: dict) -> None:
        return self.gateway_service.on_transport_event(ev)

    def _schedule_reconnect(self, reason: str) -> None:
        return self.gateway_service.schedule_reconnect(reason)

    def _pending_try_match(self, ev: dict) -> None:
        return self.gateway_service.pending_try_match(ev)

    def _pending_window_closed(self, ev: dict) -> None:
        return self.gateway_service.pending_window_closed(ev)

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        """MAC as a hex string without separators: 12 chars (full) or 6 chars (last 3 bytes)."""
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            return self.device_repository.get_by_addr(s)
        if len(s) == 6:
            return self.device_repository.get_by_addr(s)
        return None

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
