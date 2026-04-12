# RaceLink Architecture

## Current State

The active architecture now lives primarily under `racelink/`. The repository
root is now limited to the RotorHazard plugin entrypoint:

- `__init__.py` bootstraps the plugin, registers the blueprint, and wires
  RotorHazard events as the root plugin entry.
- `controller.py` now acts mostly as a compatibility facade and coordinator for
  persistence, communicator lifecycle, legacy entrypoints, and service
  delegation. Discovery, status, control/config/sync/stream, gateway
  orchestration, and startblock behavior live in `racelink/services/`.
- `racelink_proto.h` remains the source of truth for the protocol, mirrored by
  `gen_racelink_proto_py.py` into `racelink/racelink_proto_auto.py`.

## Target Structure

The long-term architecture is introduced as a package scaffold under
`racelink/`:

- `racelink/app.py`
  Active application container and dependency wiring entrypoint.
- `racelink/core/`
  Cross-cutting runtime abstractions, events, source/sink contracts, and
  application-level contracts.
- `racelink/domain/`
  Domain models, type metadata, capabilities, and behavior-free helpers.
- `racelink/protocol/`
  Protocol API, packet/rule access, codec helpers, and addressing helpers.
- `racelink/transport/`
  Serial gateway transport, framing, and low-level transport events.
- `racelink/state/`
  Repositories, defaults, and persistence boundaries.
- `racelink/services/`
  Business services such as gateway orchestration, discovery, status, control,
  config, sync, stream, startblock, OTA, presets, and host WiFi.
- `racelink/integrations/rotorhazard/`
  RotorHazard-specific bootstrap, UI, actions, data IO, and source adapters.
- `racelink/integrations/standalone/`
  Standalone bootstrap and config entrypoints using the same core.
- `racelink/integrations/polling/`
  Polling/web-source and sink adapters for future non-RotorHazard operation.
- `racelink/web/`
  Flask blueprint composition, API modules, SSE handling, DTOs, and task
  orchestration.

## Layer Responsibilities

- `domain` defines models and metadata, but no Flask, RotorHazard, or transport
  concerns.
- `protocol` exposes protocol structure cleanly so higher layers do not depend
  on raw body layouts.
  The generated module `racelink/racelink_proto_auto.py` is the canonical mirror
  for protocol constants, rules, packed sizes, and packed field layouts.
- `transport` handles USB/serial/framing and emits low-level events only.
- `state` owns in-memory repositories and persistence concerns.
- `services` implement business workflows against protocol, transport, and
  repositories.
- `integrations/*` adapt environment-specific systems into the core.
- `web` adapts HTTP/SSE traffic to services without embedding business logic.
  Specials metadata lives in `domain/specials.py`, but the web layer reads it
  through `SpecialsService` so routes do not depend directly on domain helpers.
- `app.py` is now the single dependency-wiring container; remaining cleanup is
  mostly about reducing legacy controller surface and moving heavy route
  workflows out of `web/api.py`.

## Source / Sink Model

RaceLink is being prepared to consume data from different environments and to
optionally publish outward-facing events without binding the core to a single
host application.

- `core.events.EventSource`
  Adapter contract for systems that provide external context or event snapshots
  to RaceLink.
- `core.events.DataSink`
  Adapter contract for systems that consume RaceLink-generated events or state
  changes.
- `core.events.NullSource` / `NullSink`
  Safe defaults when no external source or sink is configured.

Current and prepared adapters:

- `integrations/rotorhazard/source.py`
  Active `RotorHazardSource` adapter for RH-specific heat/slot data.
- `integrations/polling/web_source.py`
  Prepared polling-based web source scaffold.
- `integrations/polling/http_sink.py`
  Prepared outbound HTTP sink scaffold.
- `integrations/standalone/bootstrap.py`
  Prepared standalone bootstrap returning default source/sink wiring.

The active RotorHazard path remains unchanged, but the app container now has a
clear place for `event_source` and `data_sink` wiring.

## Migration Principles

- One backlog task per branch/PR.
- No behavior changes unless the task explicitly requires them.
- No protocol changes in `racelink_proto.h` unless a task explicitly requires them.
- The only supported Python protocol mirror path is
  `racelink_proto.h -> gen_racelink_proto_py.py -> racelink/racelink_proto_auto.py`.
- No UI redesign during the architecture refactor.
- RotorHazard remains fully functional during the migration.
- Standalone support is added as an additional path, not as a replacement.
- Temporary compatibility layers should be aggressively removed once the
  package-based import path is established.

## Import Boundaries

These boundaries document the intended architecture and now also have a small
automated guardrail in `tests/test_architecture_imports.py`. The check is
intentionally narrow: it enforces the hard layer breaks that are already
expected to hold, without trying to lint every architectural nuance.

- `domain` must not import Flask or RotorHazard modules.
- `protocol` should not depend on web or RotorHazard concerns.
- `protocol` and `transport` must import the generated protocol mirror via the
  package path `racelink.racelink_proto_auto`, never via a top-level fallback.
- `transport` must not import RotorHazard modules.
- `state` should remain framework-agnostic.
- `services` may depend on domain/state/protocol/transport abstractions, but
  not on RotorHazard UI modules.
- `web` may depend on services and DTO helpers, but should avoid embedding deep
  business logic.
- `integrations/rotorhazard` may import core/services/web modules, but core
  layers must not import RotorHazard-specific modules.
- `integrations/standalone` and `integrations/polling` follow the same adapter
  direction: inward to the core, never the other way around.
- RotorHazard adapters may still read domain-level specials metadata directly
  for RH-specific UI/action construction; the stricter service boundary is
  currently enforced for the generic web/API layer.

Review cues:

- If a file under `racelink/domain/` imports `flask`, `RHUI`, or
  `eventmanager`, that is a boundary violation.
- If a file under `racelink/transport/` imports RotorHazard-specific modules,
  that is a boundary violation.
- If a file under `racelink/services/` imports `RHUI` or anything below
  `racelink.integrations.rotorhazard`, that is a boundary violation.
- Imports from `racelink.integrations.rotorhazard` must stay at the edge:
  `core`, `domain`, `protocol`, `transport`, `state`, and `services` should not
  depend on them.

## Backlog Alignment

This structure was introduced incrementally and now reflects the real package
layout used for new work:

- RL-002 and RL-003 move bootstrap and dependency wiring.
- RL-004 through RL-006 separate domain/state/persistence.
- RL-007 through RL-012 isolate transport, protocol, and core services.
- RL-013 through RL-017 move integrations and standalone support to the edges.
- RL-018 through RL-020 add tests, import-boundary enforcement, and cleanup.

## Backlog Status

| Area | Status | Notes |
| --- | --- | --- |
| RL-001 to RL-010 | done | Package structure, bootstrap, state, persistence, transport, protocol, gateway/discovery/status services are in place. |
| RL-011 to RL-017 | done | Active send services, startblock service, RH integration split, web split, operating services, source/sink model, and standalone bootstrap exist. |
| RL-018 to RL-019 | done | Tests and import-boundary checks exist and run in the standard test suite. |
| RL-020 | done | Package imports are primary and the old root shim modules have been removed. |
| Follow-up cleanup | follow-up needed | `controller.py` is smaller but still central for persistence/lifecycle, and `racelink/web/api.py` still contains route-heavy orchestration around some web flows. |

## RL-020 Migration Note

What moved:
- Internal integration bootstraps now import the package-based web layer
  directly.
- Business logic for gateway, device operations, startblock, OTA, presets, and
  host WiFi lives in package services.
- Documentation now treats `racelink/` as the primary structure instead of a
  future scaffold.

What stayed compatible:
- Root plugin/bootstrap imports still work.

Risks:
- `controller.py` still owns persistence and communicator lifecycle, so the
  final architecture is improved but not fully normalized yet.
- `racelink/web/api.py` is thinner than before but still carries route-heavy
  orchestration that should continue to move toward smaller service entrypoints.
- The protocol mirror is now packaged correctly, but packet builders and reply
  decoding are still hand-written Python logic. They are protected by drift
  tests against `racelink/racelink_proto_auto.py`, not yet fully code-generated.
