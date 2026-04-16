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
