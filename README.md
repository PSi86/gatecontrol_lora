# RaceLink

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
  Business services for gateway orchestration, discovery, status, control, config, sync, stream, startblock, OTA, presets, and host WiFi.
- `racelink/integrations/rotorhazard/`
  RotorHazard bootstrap, UI, actions, import/export, and RH data source adapter.
- `racelink/integrations/standalone/`
  Minimal standalone bootstrap, config, and Flask app factory.
- `racelink/integrations/polling/`
  Prepared polling source and HTTP sink scaffolds.
- `racelink/web/`
  Blueprint assembly, API routes, SSE handling, DTO helpers, and task state.

## Root Surface

The repository root now exposes only the RotorHazard plugin entry in `__init__.py`.
All other internal imports are expected to use the canonical package paths under `racelink/*`.

## Running Checks

- Test suite: `py -3 -m unittest discover -s tests -v`
- Architecture boundary checks are included in the same test run.

## Notes

- `racelink_proto.h` remains the protocol source of truth.
- The supported Python mirror path is `racelink_proto.h -> gen_racelink_proto_py.py -> racelink/racelink_proto_auto.py`.
- The generator now mirrors constants, response rules, packed struct sizes, and packed field layouts used by the Python side.
- `packets.py` and `codec.py` still contain the active Python builders/decoders, so generator-backed drift tests are used to keep those hand-written paths aligned with the shared header.
- RotorHazard remains the primary supported integration path.
- Standalone support exists as an additional minimal path and is not yet feature-complete.
- `controller.py` is now mostly a compatibility facade plus lifecycle/persistence coordinator, but it is still larger than the target architecture.
- `racelink/web/api.py` uses dedicated services for the heavier OTA/presets/specials flows, though some route orchestration still remains and is a candidate for further cleanup.
- Root-level legacy shim modules (`data.py`, `racelink_transport.py`, `racelink_webui.py`, `ui.py`) have been removed; package imports are now the only supported internal import path.
