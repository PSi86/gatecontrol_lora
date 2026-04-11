# RaceLink LoRa

RaceLink now uses the `racelink/` package as the primary home for the refactored architecture.

## Current Structure

- `racelink/app.py`
  Central application container and dependency wiring anchor.
- `racelink/core/`
  Cross-cutting contracts such as app events plus source/sink interfaces.
- `racelink/domain/`
  Domain models, device metadata, capability helpers, and specials config.
- `racelink/protocol/`
  Protocol rule lookup, codec helpers, packet builders, and addressing helpers.
- `racelink/transport/`
  Serial gateway transport, framing, and low-level transport events.
- `racelink/state/`
  Runtime repositories plus JSON persistence helpers.
- `racelink/services/`
  Business services for gateway orchestration, discovery, status, OTA, presets, and host WiFi.
- `racelink/integrations/rotorhazard/`
  RotorHazard bootstrap, UI, actions, import/export, and RH data source adapter.
- `racelink/integrations/standalone/`
  Minimal standalone bootstrap, config, and Flask app factory.
- `racelink/integrations/polling/`
  Prepared polling source and HTTP sink scaffolds.
- `racelink/web/`
  Blueprint assembly, API routes, SSE handling, DTO helpers, and task state.

## Compatibility Shims

The repository root still contains a few thin compatibility modules so existing plugin/runtime entrypoints keep working:

- `__init__.py`
  Root plugin bootstrap entry for RotorHazard.
- `data.py`
  Legacy import shim to `racelink.domain` and runtime state repositories.
- `racelink_transport.py`
  Legacy import shim to `racelink.transport`.
- `racelink_webui.py`
  Legacy import shim to `racelink.web`.
- `ui.py`
  Legacy import shim to `racelink.integrations.rotorhazard.ui`.

New internal code should import from `racelink/*` directly instead of these root-level shims.

## Running Checks

- Test suite: `py -3 -m unittest discover -s tests -v`
- Architecture boundary checks are included in the same test run.

## Notes

- `lora_proto.h` remains the protocol source of truth.
- RotorHazard remains the primary supported integration path.
- Standalone support exists as an additional minimal path and is not yet feature-complete.
