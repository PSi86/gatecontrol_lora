# RaceLink Host

Host software for the **RaceLink** wireless control system.

`RaceLink_Host` is an installable Python package. The distribution can be installed as `racelink-host`, and the package is imported as `racelink`.

The canonical runtime version is exposed as `racelink.__version__` and `racelink.get_version()`. For shell and CI usage, the package also exposes `racelink-host-version`.

This repository now contains the host-side core runtime, the shared RaceLink WebUI, and the standalone Flask host mode. The RotorHazard adapter is no longer part of this repository and belongs in the separate `RaceLink_RH-plugin` repository.

## What stays in this repository

- RaceLink core runtime and services
- Gateway communication and protocol handling
- Shared RaceLink WebUI assets in `racelink/pages/` and `racelink/static/`
- Shared web registration in `racelink/web/`
- Standalone Flask hosting in `racelink/integrations/standalone/`

`racelink/pages/` and `racelink/static/` remain RaceLink-owned UI assets in the host repository. They are not plugin-only files.

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

## Release artifacts

Pushing a semver tag like `v0.1.0` triggers the GitHub Actions release workflow in `.github/workflows/release.yml`.

The workflow builds and publishes these stable filenames:

- `racelink_host-<version>-py3-none-any.whl`
- `racelink-host-<version>.tar.gz`
- `racelink-host-<version>-sha256.txt`

For example, tag `v0.1.0` publishes:

- `racelink_host-0.1.0-py3-none-any.whl`
- `racelink-host-0.1.0.tar.gz`
- `racelink-host-0.1.0-sha256.txt`

The workflow rejects tags whose version does not match `racelink.__version__`.

## Consuming `racelink-host` from other repositories

Other repositories should consume `RaceLink_Host` as an installable package, not by importing from a sibling source checkout.

### Build and release flow

- Create a semver tag like `v0.1.0` in `RaceLink_Host`.
- GitHub Actions builds the release artifacts from that tagged commit.
- The release publishes these stable filenames:
- `racelink_host-<version>-py3-none-any.whl`
- `racelink-host-<version>.tar.gz`
- `racelink-host-<version>-sha256.txt`

### Install from a GitHub release artifact

Online installation from a downloaded release wheel:

```bash
python -m pip install ./racelink_host-0.1.0-py3-none-any.whl
```

Offline installation from a bundled wheel:

```bash
python -m pip install --no-index ./racelink_host-0.1.0-py3-none-any.whl
```

The wheel is the preferred runtime artifact. The sdist is published for source distribution and verification workflows, but consumers should not depend on unpacking a source tree at runtime.

### Expected integration for `RaceLink_RH-plugin`

`RaceLink_RH-plugin` should declare and consume `racelink-host` as a package dependency and use the host-owned public integration surface:

- `racelink.__version__` or `racelink.get_version()` to log the loaded host version
- `racelink.app.create_runtime(...)` to construct the host runtime
- `racelink.web.register_racelink_web(...)` to mount the shared RaceLink WebUI

The plugin should not depend on repo-relative paths, copied `pages/` or `static/` folders, or imports from a local `RaceLink_Host` checkout.

### Offline bundle guidance

Offline bundles should be populated from the published wheel, not from a source checkout snapshot.

That means the offline bundle should carry `racelink_host-<version>-py3-none-any.whl` as the canonical host payload, install that wheel locally, and then import `racelink` from the installed package.

## Related repositories

- RaceLink Host: `https://github.com/PSi86/RaceLink_Host`
- RaceLink RotorHazard plugin: separate adapter repository
- RaceLink Gateway: `https://github.com/PSi86/RaceLink_Gateway`
- RaceLink WLED nodes: `https://github.com/PSi86/RaceLink_WLED`
