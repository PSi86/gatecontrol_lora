# RaceLink Standalone Guide

This guide explains how to install and run `racelink-host` in standalone mode on Windows and Linux.

Standalone mode runs the RaceLink host runtime and the shared RaceLink WebUI without RotorHazard. It is intended for gateway-backed operation, so a connected RaceLink Gateway is expected for normal use.

## Requirements

- Python `3.10` or newer
- A terminal or shell
- A RaceLink Gateway connected by USB for normal operation
- Network access to install Python packages

The packaged standalone entrypoint is:

```bash
racelink-standalone
```

Default standalone URL:

```text
http://127.0.0.1:5077/racelink
```

## Windows installation and usage

Create and activate a virtual environment:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

Install RaceLink Host:

```powershell
python -m pip install --upgrade pip
python -m pip install racelink-host
```

Start standalone mode:

```powershell
racelink-standalone
```

Open the UI in a browser:

```text
http://127.0.0.1:5077/racelink
```

Stop the server with `Ctrl+C`.

Windows notes:

- The RaceLink Gateway usually appears as a `COM` port such as `COM3` or `COM4`
- If multiple serial devices are connected, confirm which `COM` port belongs to the gateway in Device Manager
- If the UI loads but the gateway is not detected, check the configured port and the USB connection first

## Linux installation and usage

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install RaceLink Host:

```bash
python -m pip install --upgrade pip
python -m pip install racelink-host
```

Start standalone mode:

```bash
racelink-standalone
```

Open the UI in a browser:

```text
http://127.0.0.1:5077/racelink
```

Stop the server with `Ctrl+C`.

Linux notes:

- The RaceLink Gateway usually appears as a serial device such as `/dev/ttyUSB0` or `/dev/ttyACM0`
- If RaceLink cannot open the gateway, check user permissions for serial devices
- On some systems you may need to add the user to a group such as `dialout`
- Wi-Fi helper features that depend on `nmcli` only work in environments where NetworkManager and `nmcli` are installed

## Configuration file

Standalone mode stores its local configuration in:

```text
~/.racelink/standalone_config.json
```

On Windows this usually expands to something like:

```text
C:\Users\<username>\.racelink\standalone_config.json
```

On Linux this usually expands to something like:

```text
/home/<username>/.racelink/standalone_config.json
```

Example configuration:

```json
{
  "host": "127.0.0.1",
  "port": 5077,
  "debug": false,
  "options": {
    "psi_comms_port": "COM3"
  }
}
```

Useful fields:

- `host`: bind address for the standalone Flask server
- `port`: TCP port for the standalone Flask server
- `debug`: Flask debug mode
- `options`: persisted RaceLink options used by the host runtime

To change the bind address or port, edit the config file before starting `racelink-standalone`.

## Verifying that standalone mode works

After starting the server:

1. Open `http://127.0.0.1:5077/`
2. Confirm it redirects to `/racelink`
3. Confirm the RaceLink WebUI loads successfully
4. Watch the terminal output for gateway startup messages

Expected behavior:

- Without a connected gateway, the UI can still load, but gateway communication will not be ready
- With a connected gateway, standalone mode should report that the communicator is ready and the WebUI should be able to interact with RaceLink services

## Manual validation checklist

- Create a fresh virtual environment
- Install `racelink-host`
- Start `racelink-standalone`
- Open the browser at `/racelink`
- Confirm `/` redirects to `/racelink`
- Confirm the shared RaceLink WebUI loads
- Confirm the gateway is reported as unavailable when no gateway is connected
- Confirm the gateway is detected when the correct serial port is configured and the hardware is connected
