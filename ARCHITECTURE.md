# RaceLink Architecture

## Repository Scope

`RaceLink_Host` now contains only the host-owned parts of the system:

- core runtime wiring
- transport, protocol, state, and service layers
- the shared RaceLink WebUI
- standalone Flask hosting

RotorHazard-specific adapter code is no longer part of this repository. That adapter belongs in the separate `RaceLink_RH-plugin` repository.

## Stable Host Entry Points

External adapters should depend on the host through these stable entry points:

- `racelink.app.create_runtime(...)`
- `racelink.web.register_racelink_web(...)`

This keeps plugin repositories from reaching deeply into host internals.

## Package Layout

- `racelink/app.py`
  Runtime container and host-owned runtime factory.
- `racelink/core/`
  Cross-cutting contracts and null source/sink defaults.
- `racelink/domain/`
  Device models, metadata, and specials helpers.
- `racelink/protocol/`
  Protocol constants, rule helpers, and packet support.
- `racelink/transport/`
  Serial gateway transport and framing.
- `racelink/state/`
  Runtime repositories and persistence helpers.
- `racelink/services/`
  Host business workflows.
- `racelink/web/`
  Shared RaceLink WebUI registration, API, SSE, DTOs, and task state.
- `racelink/integrations/standalone/`
  Canonical standalone Flask bootstrap using the same host runtime and WebUI.
- `pages/` and `static/`
  Shared RaceLink WebUI assets that remain in the host repository.

## WebUI Hosting Model

There is one RaceLink WebUI.

- In standalone mode, the standalone Flask app mounts that UI through `register_racelink_web(...)`.
- In RotorHazard mode, the external adapter plugin is expected to mount that same UI through the same host-owned registration entry.
- The packaged standalone user entrypoint is `racelink-standalone`, which boots the host-owned standalone integration under `racelink.integrations.standalone`.

`pages/` and `static/` stay in the host repository so both hosting modes use the same UI implementation.

## Layer Boundaries

- `domain` stays framework-agnostic.
- `protocol` and `transport` do not depend on web-hosting concerns.
- `state` owns repositories and persistence.
- `services` implement host workflows and should not depend on external adapters.
- `web` adapts HTTP and SSE traffic to host services.
- `integrations/standalone` depends inward on host modules and does not define separate UI behavior.

## Current Notes

- `controller.py` remains a compatibility-oriented host controller, but it now only coordinates host runtime behavior.
- Standalone support continues to use the shared WebUI and host services.
- `pages/` and `static/` are intentionally retained here and are not plugin leftovers.

## Gateway Ownership (Plan P3-5)

Only **one** process must hold the USB-serial connection to the RaceLink_Gateway dongle at a time. The host enforces this by opening the port with `exclusive=True` in `racelink/transport/gateway_serial.py`.

Ownership rules:

- **Standalone mode** (`racelink-standalone`): the host owns the gateway for the lifetime of the Flask app. `run_standalone()` calls `onStartup({})` which triggers `discoverPort({})`.
- **RotorHazard plugin mode**: the plugin owns the gateway. RotorHazard itself does **not** open the dongle. When the plugin's `initialize()` runs, the Host's `onStartup` is wired to `Evt.STARTUP`; `discoverPort` then claims the port.
- **Never run both simultaneously** against the same dongle. The second process will see `serial.SerialException` from the exclusive lock and log it via `_record_gateway_error`; the UI banner (plan P1-1) surfaces this to the operator.
- **Release on shutdown**: `RaceLink_Host.shutdown()` (plan P1-2) calls `transport.close()` so the port is released before the process exits. The plugin registers this on `Evt.SHUTDOWN` where available.

If you ever need to share a gateway between processes (e.g. dev tooling + live host), serialize access at the process level -- there is no in-transport multiplexing today.

## Transport Interface (post-redesign)

The Gateway firmware keeps the SX1262 in **Continuous RX** as its default state. After each TX the Core reverts to Continuous automatically; no Timed-RX window is opened for unicast request/response flows. This was the original cause of the "No ACK_OK for ..." timeout-despite-ACK bug: the Host used to block until the firmware's `EV_RX_WINDOW_CLOSED` event arrived, but that event can be delayed by ESP32 USB CDC buffering.

Host-side matching is therefore owned entirely by `racelink/services/pending_requests.py` and the two entry points in `GatewayService`:

| Call pattern | Helper | Completion signal |
|---|---|---|
| Unicast request → single ACK or specific reply | `send_and_wait_for_reply` | `PendingRequestRegistry` matches `(sender, ack_of_or_opc)` and sets the per-request event |
| Broadcast / group → N replies within a window | `send_and_collect` | Host wall clock (`duration_s`) with early-exit on `expected` count |

The old `wait_rx_window` helper remains for backwards compatibility but is deprecated. New code should not call it.

`EV_RX_WINDOW_OPEN` / `EV_RX_WINDOW_CLOSED` stay in the wire format (the Core header is frozen) but are debug-only from the Host's perspective.

## Locking Rule: Never hold `state_repository.lock` across RF I/O

The state-repository lock (`state_repository.lock`, surfaced as `ctx.rl_lock` in the web layer) is taken by:

1. **Web handlers** that read/mutate device or group state.
2. **The gateway reader thread**, inside `GatewayService.handle_ack_event`, `on_transport_event` (status/identify branches), and `pending_*` bookkeeping.

Both paths must acquire the **same** lock so a request thread and the reader thread see a consistent view of the device list. That is the whole point of a single state lock (plan P1-4).

Consequence: **a handler that holds the state lock while waiting for a reply over RF will deadlock the reader**. The reader thread stalls in `handle_ack_event` for the reply that just arrived -- and because it is stalled, it cannot pull the *next* USB frame out of pyserial's RX buffer. USB frames for subsequent devices queue up; the next `send_and_wait_for_reply` times out even though the ACK is sitting unread in the OS buffer. Symptoms:

- First unicast call in a bulk returns promptly.
- Every subsequent unicast call in the same bulk times out at exactly the wait budget (e.g. 8.000 s).
- Immediately after the timeout releases the lock, a flood of queued USB events drains into the log (TX\_DONE, RX window OPEN, late ACK).

The rule, therefore, is:

> **Never call `setNodeGroupId`, `sendConfig(..., wait_for_ack=True)`, `sendRaceLink`, `sendGroupControl`, `send_stream`, `discover_devices`, or `get_status` while holding `state_repository.lock` / `ctx.rl_lock`.**

In practice this means bulk loops must release and re-acquire the lock around each iteration's RF call. See `_apply_device_meta_updates` in `racelink/web/api.py` for the reference pattern (acquire → read/mutate in-memory → release → blocking RF → repeat).

A regression test (`tests/test_web_handler_helpers.py::ApplyDeviceMetaUpdatesDoesNotHoldLockAcrossBlockingIO`) exercises this rule by simulating a second thread that must acquire the lock mid-bulk.

## UI Scope Matrix

State mutations travel to the UI layer via two paths: the in-process RotorHazard UI (through `on_persistence_changed` → `RotorHazardUIAdapter.apply_scoped_update`) and the browser WebUI (through the SSE `refresh` channel mapped by `racelink/domain/state_scope.sse_what_from_scopes`). Both consume the same scope tokens so that a single `save_to_db(scopes=...)` call fans out consistently.

**Authoritative scope tokens** are defined in [racelink/domain/state_scope.py](racelink/domain/state_scope.py):

| Token | When to use |
|---|---|
| `FULL` | Initial load (`load_from_db`) or migration boot -- rebuild everything. |
| `NONE` | Pure persistence, no visible change (e.g. "Save Configuration" button just flushes the combined key). |
| `DEVICES` | Device record changed that does not move it between groups (rename, specials struct rebuild). |
| `DEVICE_MEMBERSHIP` | Device moved to a different group -- affects group counts and any list embedded per group. |
| `DEVICE_SPECIALS` | A special config byte was written on a single device (startblock slot, etc.). No cross-UI effect on the RH panels. |
| `GROUPS` | Groups added / renamed / removed -- group-list-backed dropdowns must refresh. |
| `EFFECTS` | WLED presets file reloaded -- effect-list-backed selects must refresh. |

**RotorHazard adapter (`custom_plugins/racelink_rh_plugin/plugin/ui.py`)** reacts as follows. Elements in the "Once" column are bootstrapped on first sync and then guarded by the `_settings_panel_bootstrapped` / `_quickset_panel_bootstrapped` flags; calling `sync_rotorhazard_ui` repeatedly therefore no longer produces `RHUI Redefining ...` log spam.

| RH UI element | Once (bootstrap) | GROUPS | DEVICES | DEVICE_MEMBERSHIP | DEVICE_SPECIALS | EFFECTS |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Panel `rl_settings` | ✓ | | | | | |
| Panel `rl_quickset` | ✓ | | | | | |
| Option `rl_device_config` | ✓ | | | | | |
| Option `rl_groups_config` | ✓ | | | | | |
| Option `rl_assignToNewGroup` | ✓ | | | | | |
| Quickbutton `rl_btn_set_defaults` | ✓ | | | | | |
| Quickbutton `rl_btn_force_groups` | ✓ | | | | | |
| Quickbutton `rl_btn_get_devices` | ✓ | | | | | |
| Quickbutton `rl_run_autodetect` | ✓ | | | | | |
| Option `rl_quickset_brightness` | ✓ | | | | | |
| Quickbutton `run_quickset` | ✓ | | | | | |
| Option `rl_assignToGroup` (dynamic) | | ✓ | | | | |
| Option `rl_quickset_group` (dynamic) | | ✓ | | | | |
| Option `rl_quickset_effect` (dynamic) | | | | | | ✓ |
| Default `ActionEffect` `gcaction` | | ✓ | | | | ✓ |
| Per-capability special `ActionEffect`s | | ✓ | ✓ | ✓ | | ✓ |

**SSE topics (`racelink/domain/state_scope.sse_what_from_scopes`)** drive the browser WebUI:

| Token | SSE `refresh.what` payload | JS handler action |
|---|---|---|
| `FULL` | `["groups", "devices"]` | `loadGroups()` + `loadDevices()` |
| `NONE` | `[]` | no-op |
| `DEVICES` | `["devices"]` | `loadDevices()` |
| `DEVICE_MEMBERSHIP` | `["devices", "groups"]` | both (membership affects per-group counts) |
| `DEVICE_SPECIALS` | `["devices"]` | `loadDevices()` |
| `GROUPS` | `["groups"]` | `loadGroups()` |
| `EFFECTS` | `["effects"]` | preset dropdown refresh |

**Rule of thumb for new call sites.** When you call `save_to_db(args, scopes=...)`, pick the narrowest token set describing what actually changed. If you genuinely don't know, pass `{FULL}` -- but prefer to refactor so you do know. The RH adapter and SSE scope map are both designed around this precision, and the regression tests in `tests/test_ui_scope_routing.py` (plugin) and `tests/test_state_scope.py` (host) pin the mapping so an accidental FULL-regression surfaces in CI.
