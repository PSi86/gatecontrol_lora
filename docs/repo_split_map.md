# Repository Split Map

This map records the RotorHazard split after the adapter removal from `RaceLink_Host`.

## Host-Owned Import Edge

These entry points stay in `RaceLink_Host` and are the supported surface for external adapters:

- `racelink.app:create_runtime`
- `racelink.web:register_racelink_web`
- `racelink.web:RaceLinkWebRuntime`

## Already Moved Out Of Host

The following paths used to live in this repository and now belong in the separate `RaceLink_RH-plugin` repository:

| Previous Host Path | Target In Plugin Repo | Notes |
| --- | --- | --- |
| `__init__.py` | plugin repo root `__init__.py` | RotorHazard loader shim now belongs with the plugin. |
| `racelink/integrations/rotorhazard/__init__.py` | `racelink_rh_plugin/integrations/rotorhazard/__init__.py` | Plugin package edge. |
| `racelink/integrations/rotorhazard/plugin.py` | `racelink_rh_plugin/integrations/rotorhazard/plugin.py` | Adapter bootstrap for RH. |
| `racelink/integrations/rotorhazard/ui.py` | `racelink_rh_plugin/integrations/rotorhazard/ui.py` | RotorHazard UI adapter. |
| `racelink/integrations/rotorhazard/actions.py` | `racelink_rh_plugin/integrations/rotorhazard/actions.py` | RH action registration. |
| `racelink/integrations/rotorhazard/dataio.py` | `racelink_rh_plugin/integrations/rotorhazard/dataio.py` | RH import and export adapter. |
| `racelink/integrations/rotorhazard/source.py` | `racelink_rh_plugin/integrations/rotorhazard/source.py` | RH event source adapter. |

## Files That Stay In Host

| Host Path | Why It Stays |
| --- | --- |
| `racelink/app.py` | Owns the host runtime factory and service wiring. |
| `racelink/web/**` | Owns the shared RaceLink WebUI registration, API, SSE, and task state. |
| `racelink/integrations/standalone/**` | Hosts the standalone Flask mode. |
| `racelink/pages/**` and `racelink/static/**` | Shared RaceLink WebUI assets for all hosting modes and package installation. |
| `controller.py` | Host controller and runtime coordinator. |
