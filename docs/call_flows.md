# Call-Flows: RotorHazard ↔ RaceLink Core

Dieses Dokument beschreibt zentrale Laufzeit-Sequenzen von RaceLink im RotorHazard-Host.

## 1) RotorHazard-Startup

### Einstiegspunkt
- `plugins/rotorhazard/bootstrap.py` → `RotorHazardAdapter.initialize()`

### Zwischenstationen
1. `RotorHazardAdapter.initialize()` ruft `RotorHazardPlugin.build(...)` auf und startet anschließend `plugin.start()`.  
2. `RotorHazardPlugin.build(...)` erstellt die Kernobjekte:
   - `InMemoryDeviceRepository`
   - `RotorHazardRaceProvider`
   - `RotorHazardRaceEventAdapter`
   - `RaceLink_LoRa(...)` inkl. `RaceLinkApp(...)`
   - UI-Bindung via `controller.bind_host_ui(RotorHazardHostUIAdapter(controller))`
3. `RotorHazardPlugin.start()` aktiviert Features (über Feature-Flags):
   - `web_blueprint.activate(...)`
   - `config_io.activate(...)`
   - `ui_extensions.activate(...)`
   - `events.activate(...)`
4. `events.activate(...)` registriert den RH-Startup-Hook:
   - `rhapi.events.on(Evt.STARTUP, controller.onStartup)`
5. Beim RH-`STARTUP` läuft `RaceLink_LoRa.onStartup(...)`:
   - `self.app.load_from_db()`
   - UI-Listen aufbauen (`createUiDevList`, `createUiGroupList`, Discovery-Gruppen)
   - Settings/Quickset/Actions registrieren
   - UI refresh (`broadcast_ui("settings")`, `broadcast_ui("run")`)
   - Port-Discovery via `discoverPort({})`
6. `discoverPort({})` delegiert in `LoRaTransportAdapter.discover_port(args)`:
   - liest `psi_comms_port` aus RH-DB
   - `LoRaUSB(...).discover_and_open()`
   - bei Erfolg: `start()`, Transport-Hooks installieren, Adapter auf `ready=True`

### Seiteneffekte
- **DB-Load/Save**:
  - `RaceLinkApp.load_from_db()` liest `rl_device_config`/`rl_groups_config`; bei fehlenden Werten werden Defaults sofort per `option_set(...)` persistiert.
- **Broadcasts**:
  - Nach UI-Registrierung: `rhapi.ui.broadcast_ui("settings")` und `rhapi.ui.broadcast_ui("run")`.
- **LoRa TX/RX**:
  - Beim Startup selbst noch kein zwingender Control-TX; aber durch `discover_port` wird die LoRa-Verbindung geöffnet, der Listener installiert und damit RX/TX-Ereignisverarbeitung aktiviert.

---

## 2) Race-Event-Verarbeitung

### Einstiegspunkt
- `plugins/rotorhazard/host/rotorhazard_provider.py` → `RotorHazardRaceEventAdapter.start(event_sink)`

### Zwischenstationen
1. Während Komposition (`RotorHazardPlugin.build(...)`) wird `RaceLinkApp(...)` inkl. `race_event_port` erstellt, **aber noch nicht gestartet**.
2. In `RotorHazardPlugin.start()` werden zuerst Feature-Module aktiviert; danach startet der Lifecycle explizit:
   - `self.app.start_event_stream()`
3. `RotorHazardRaceEventAdapter.start(...)` registriert RH-Events:
   - `Evt.RACE_START` → `_on_race_start`
   - `Evt.RACE_FINISH` → `_on_race_finish`
   - `Evt.RACE_STOP` → `_on_race_stop`
4. Jeder Handler ruft `_emit(...)` auf:
   - erzeugt `HostRaceEvent(type=..., payload={"source_payload": payload})`
   - übergibt an den Sink (`RaceLinkApp.on_race_event`)
5. `RaceLinkApp.on_race_event(event)` dispatcht nach `event.type`:
   - `RACE_STARTED` → `on_race_start(...)`
   - `RACE_FINISHED` → `on_race_finish(...)`
   - `RACE_STOPPED` → `on_race_stop(...)`
   - `RACE_SNAPSHOT` → Debug-Log
6. Optionaler Shutdown (`RotorHazardPlugin.stop()`):
   - `self.app.stop_event_stream()` ruft `race_event_port.stop()` auf und meldet den Sink sauber ab.

### Seiteneffekte
- **DB-Load/Save**:
  - In diesem Pfad derzeit keine direkte Persistierung.
- **Broadcasts**:
  - In diesem Pfad derzeit keine direkten RH-UI-Broadcasts.
- **LoRa TX/RX**:
  - In den Default-Handlern aktuell keine LoRa-Sendelogik; sie loggen primär den Race-Status.

---

## 3) UI-Aktionen: Quickset/Action-Handler bis Core-Services

Die produktiven UI-Module liegen vollständig unter `plugins/rotorhazard/`:
- Web-Blueprint/WebUI: `plugins/rotorhazard/presentation/racelink_webui.py`
- UI-Registrierung/Handler: `plugins/rotorhazard/ui/quickset_panel.py`, `plugins/rotorhazard/ui/actions_registry.py`, `plugins/rotorhazard/ui/host_ui_adapter.py`

### Einstiegspunkte
- Quickset-Panel: `plugins/rotorhazard/ui/quickset_panel.py` → `register_quickset_ui(gc)`
- Action-Registry: `plugins/rotorhazard/ui/actions_registry.py` → `register_actions(gc, args=None)`
- Handler im Host-Adapter:
  - `plugins/rotorhazard/ui/host_ui_adapter.py` → `groupSwitch(...)`
  - `plugins/rotorhazard/ui/host_ui_adapter.py` → `nodeSwitch(...)`
  - `plugins/rotorhazard/ui/host_ui_adapter.py` → `specialAction(...)`

### Zwischenstationen
1. `register_quickset_ui(...)` legt RH-UI-Optionen an (`rl_quickset_group/effect/brightness`) und registriert den Quickbutton „Apply“ auf `gc.groupSwitch` mit `args={"manual": True}`.
2. `groupSwitch(...)` löst je nach Kontext aus:
   - Action-basiert (`rl_action_group/...`) oder
   - Quickset-basiert (`manual` + DB-Optionswerte)
   - delegiert jeweils nach `self.controller.app.apply_group_switch(...)`
3. `nodeSwitch(...)` arbeitet analog auf Device-Ebene:
   - ermittelt Target-Device und Werte
   - ruft `self.controller.app.apply_device_switch(...)`
4. `RaceLinkApp` delegiert zu Services:
   - `apply_group_switch(...)` → `ControlService.apply_group_switch(...)`
   - `apply_device_switch(...)` → `ControlService.apply_device_switch(...)`
5. `ControlService` erzeugt Flags und sendet LoRa-Control:
   - Gruppe: `send_group_control(...)` → `lora.send_control(recv3=FF:FF:FF, group_id=...)`
   - Device: `send_racelink(...)` → `lora.send_control(recv3=<device>, group_id=...)`
6. Für „Special Actions“ baut `actions_registry.special_action(...)` Parameter auf und ruft dynamisch eine Comm-Funktion (z. B. `sendWledControl`, `sendStartblockConfig`, `sendStartblockControl`) am Controller/App auf. Diese landen in:
   - `ControlService.send_wled_control(...)`
   - `StartblockService.send_startblock_config(...)`
   - `StartblockService.send_startblock_control(...)`
   - ggf. darunter `ControlService.send_stream(...)` oder `ConfigService.send_config(...)`

### Seiteneffekte
- **DB-Load/Save**:
  - Quickset liest Werte aus RH-DB (`rhapi.db.option(...)`).
  - `StartblockService.send_startblock_config(...)` aktualisiert Device-Specials und triggert `save_to_db(...)` → Persistenz von Device-/Group-Konfiguration.
- **Broadcasts**:
  - Direkte Quickset-/Action-Ausführung broadcastet nicht automatisch; UI-Broadcasts erfolgen an anderer Stelle (z. B. Startup/Discovery-Rebuild).
- **LoRa TX/RX**:
  - `send_control(...)` für Group/Device-Schaltungen.
  - `send_stream(...)` für Startblock-Payloads inkl. ACK-Sammeln im RX-Fenster.
  - `send_config(...)` optional mit ACK-Warten (z. B. Startblock-Slot-Konfiguration).
