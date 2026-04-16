# RaceLink Host

Host software for the **RaceLink** wireless control system.

`RaceLink_Host` is an installable Python package. The distribution can be installed as `racelink-host`, and the package is imported as `racelink`.

This repository now contains the host-side core runtime, the shared RaceLink WebUI, and the standalone Flask host mode. The RotorHazard adapter is no longer part of this repository and belongs in the separate `RaceLink_RH-plugin` repository.

## What stays in this repository

- RaceLink core runtime and services
- Gateway communication and protocol handling
- Shared RaceLink WebUI assets in `pages/` and `static/`
- Shared web registration in `racelink/web/`
- Standalone Flask hosting in `racelink/integrations/standalone/`

`pages/` and `static/` remain RaceLink-owned UI assets in the host repository. They are not plugin-only files.

## Hosting modes

The same RaceLink WebUI is used in different hosting modes:

- **Standalone mode** mounts the shared UI inside the standalone Flask app.
- **RotorHazard plugin mode** is expected to mount the same shared UI from the separate `RaceLink_RH-plugin` adapter repository.

The host-owned integration edge for outer adapters is intentionally small:

- `racelink.app.create_runtime(...)`
- `racelink.web.register_racelink_web(...)`

## Standalone mode

Standalone mode runs RaceLink as its own Flask application with the shared RaceLink WebUI mounted at `/racelink`.

- Start it with the packaged `racelink-standalone` command after installing `racelink-host`
- Normal standalone operation expects a connected RaceLink Gateway
- Default bind address: `127.0.0.1:5077`
- Default UI URL: `http://127.0.0.1:5077/racelink`

For full Windows and Linux installation, configuration, and usage instructions, see [docs/standalone.md](/C:/Users/psima/Dev/RaceLink_Host/docs/standalone.md).

## Local checks

Run the test suite with:

```bash
py -3 -m unittest discover -s tests -v
```

Check local package installation with:

```bash
py -3 -m pip install --no-deps --no-build-isolation .
```

## Related repositories

- RaceLink Host: `https://github.com/PSi86/RaceLink_Host`
- RaceLink RotorHazard plugin: separate adapter repository
- RaceLink Gateway: `https://github.com/PSi86/RaceLink_Gateway`
- RaceLink WLED nodes: `https://github.com/PSi86/RaceLink_WLED`
