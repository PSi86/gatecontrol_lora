# Compatibility Map (Backward-Compat / Legacy APIs)

Stand: 2026-04-02.

Dieses Dokument erfasst bestehende Kompatibilitäts-Shims und Legacy-Aliase, benennt den Zielpfad (neuer API-Name), dokumentiert bekannte aktive Nutzung und definiert Bereinigungskriterien inkl. geplantem Meilenstein.

## 1) Backward-Compat Import-Shims (`platform/*`) — **historisch (entfernt)**

Die frühere Kompatibilitätsschicht unter `platform/*` wurde im Major-Cleanup am **2026-04-02** entfernt (`platform/__init__.py`, `platform/rh_adapter.py`, `platform/flask_adapter.py`, `platform/ports.py`).

| Altpfad / Shim | Typ | Zielpfad (neu) | Letzter bekannter Status | Migrationshinweis | Entfernung |
|---|---|---|---|---|---|
| `platform/rh_adapter.py` (`RotorHazardAdapter`) | Import-Reexport | `plugins.rotorhazard.bootstrap.RotorHazardAdapter` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe direkt auf `plugins.rotorhazard.bootstrap` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |
| `platform/flask_adapter.py` (`FlaskStandaloneAdapter`) | Import-Reexport | `plugins.standalone.flask_adapter.FlaskStandaloneAdapter` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe direkt auf `plugins.standalone.flask_adapter` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |
| `platform/ports.py` (`ConfigStorePort`, `EventBusPort`, `RacePilotDataProviderPort`, `UINotificationPort`) | Import-Reexport | `adapters.ports.*` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe auf `adapters.ports` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |

> Hinweis zum früheren Plan "Warnungen aktivieren": Durch den direkt vollzogenen Major-Cleanup wurden keine dauerhaften Laufzeit-Warnungen (`DeprecationWarning`) mehr in den entfernten Shims aktiviert. Downstream-Projekte müssen stattdessen auf die Zielpfade migrieren.

## 2) Legacy-Methoden / Alias-Namen in `controller.py`

> Hinweis: `RaceLink_LoRa.__getattr__` delegiert fehlende Attribute an `host_ui` und `app`. Dadurch bleiben ältere Namenskonventionen (CamelCase) indirekt erreichbar.

| Legacy-Name | Zielpfad (neu) | Aktiv genutzt von … (bekannt) | Migrationshinweis | Entfernungskriterium | Geplante Bereinigung |
|---|---|---|---|---|---|
| `onStartup(_args)` | geplanter Snake-Case-Einstieg `on_startup(args)` (bzw. expliziter Bootstrap-Hook) | RH-Eventfluss über Plugin-Startup (indirekt über Controller-Lifecycle). | RH-Event-Registrierung auf neuen Hooknamen umstellen; danach Legacy-Wrapper belassen bis nächster Major. | Kein Event-Handler referenziert `onStartup` mehr. | **v1.1 (M1)** neuen Hook ergänzen + Warnung; **v2.0 (M3)** Legacy entfernen. |
| `discoverPort(args)` | `transport_adapter.discover_port(args)` (direkt) oder `discover_port(args)` Wrapper | Settings-UI Quickbutton `gc.discoverPort` in `plugins/rotorhazard/ui/settings_panel.py`. | UI-Callback auf `discover_port` umhängen; CamelCase-Name nur noch Alias. | Keine Callback-Registrierung mehr auf `discoverPort`. | **v1.1 (M1)** Alias + Warnung; **v1.2 (M2: UI-Migration abgeschlossen)** interne Aufrufer umgestellt; **v2.0 (M3)** Entfernen. |
| `get_device_by_address(addr)` (Alias) | `getDeviceFromAddress(addr)` (derzeit), mittelfristig `get_device_from_address(addr)` | Übergabe an `LoRaTransportAdapter(get_device_by_address=...)` via `self.getDeviceFromAddress` (funktional verwandte API). | Einheitlich auf Snake Case migrieren (`get_device_from_address`), danach CamelCase als Kompat-Layer führen. | Nur noch ein kanonischer Methodenname in Controller/App. | **v1.2 (M2)** kanonischen Snake-Case-Namen einführen; **v2.0 (M3)** Dubletten bereinigen. |
| Attribut-Aliase `device_service`, `control_service`, `config_service`, `startblock_service` | Zugriff über `app.<service>` | Kommentar nennt explizit Kompat-Zweck; konkrete externe Treffer im Repo derzeit nicht eindeutig. | Verbraucher auf `controller.app.<service>` oder dedizierte Fassadenmethoden migrieren. | Keine direkten Zugriffe mehr auf Alias-Attribute. | **v1.2 (M2)** interne Umstellung + Warnung; **v2.0 (M3)** Entfernen. |

## 3) Legacy-Methoden / Alias-Namen in `plugins/rotorhazard/ui/host_ui_adapter.py`

| Legacy-Name | Zielpfad (neu) | Aktiv genutzt von … (bekannt) | Migrationshinweis | Entfernungskriterium | Geplante Bereinigung |
|---|---|---|---|---|---|
| `register_settings()` | `register_settings_ui()` | Keine direkte externe Referenz gefunden; als RH-legacy Callbackname markiert. | Alle Aufrufer auf `register_settings_ui` umstellen. | Keine Referenz mehr auf `register_settings`. | **v1.1 (M1)** Deprecation-Hinweis; **v2.0 (M3)** Entfernen. |
| `registerActions(args=None)` | `register_actions(args=None)` | RH-Event-Registrierung in `plugins/rotorhazard/features/ui_extensions.py` nutzt aktuell `registerActions`; WebUI nutzt `rl_instance.registerActions()` in `plugins/rotorhazard/presentation/racelink_webui.py`. | Event- und WebUI-Aufrufe auf `register_actions` umstellen. | Keine Registrierungen/Calls mehr auf `registerActions`. | **v1.1 (M1)** neue Aufrufer ergänzen; **v1.2 (M2)** alle internen Aufrufer migriert; **v2.0 (M3)** Entfernen. |
| `createUiDevList()` | `create_ui_device_list()` (via `settings_panel.create_ui_device_list`) | `controller.onStartup` nutzt `self.createUiDevList()` (Delegation via `__getattr__`). | Controller-Initialisierung auf `host_ui`-neuen Namen umstellen. | Kein Aufruf mehr von `createUiDevList`. | **v1.2 (M2)** interne Umstellung; **v2.0 (M3)** Entfernen. |
| `rl_createUiDevList(...)` | `create_filtered_ui_device_list(...)` | `plugins/rotorhazard/ui/actions_registry.py` nutzt `gc.rl_createUiDevList(...)`. | Action-Registry auf neuen Namen (`create_filtered_ui_device_list` oder passender Adapter-Wrapper) migrieren. | Keine Aufrufe mehr auf `rl_createUiDevList`. | **v1.2 (M2)** interne Migration; **v2.0 (M3)** Entfernen. |
| `createUiGroupList(exclude_static=False)` | `create_ui_group_list(exclude_static=False)` | `controller.onStartup` und `discoveryAction` nutzen aktuell `createUiGroupList(...)`. | Controller/Host-UI intern auf neuen Namen umstellen. | Kein Aufruf mehr von `createUiGroupList`. | **v1.2 (M2)** interne Migration; **v2.0 (M3)** Entfernen. |
| `specialAction(action, fn_key, mode)` | `special_action(...)` | Keine direkte externe Referenz gefunden (derzeit primär Adapter-intern). | Neue Integrationen direkt auf `special_action`-Pfad führen. | Keine Aufrufe mehr auf `specialAction`. | **v2.0 (M3)** zusammen mit übrigen RH-Legacy-Aliases entfernen. |
| `nodeSwitch(action, args=None)` | `app.apply_device_switch(...)` / dedizierter `apply_node_switch(...)` Wrapper | Wird als Callback in Actions/UI verwendet (via Registry/Quick-UI-Callbacks). | Callback-Registrierungen auf neue, semantische Methoden mit Snake Case umstellen. | Keine Callback-Targets mehr auf `nodeSwitch`. | **v1.2 (M2)** neue Callbacks einführen; **v2.0 (M3)** Entfernen. |
| `groupSwitch(action, args=None)` | `app.apply_group_switch(...)` / dedizierter `apply_group_switch_action(...)` Wrapper | `quickset_panel.py` und `actions_registry.py` registrieren `gc.groupSwitch`. | Callback-Ziele auf neue Methoden umstellen. | Keine Callback-Targets mehr auf `groupSwitch`. | **v1.2 (M2)** interne Migration; **v2.0 (M3)** Entfernen. |
| `discoveryAction(args)` | `discover_devices_action(args)` (neu) + App-Services (`getDevices`, `add_group`) | `settings_panel.py` registriert Quickbutton mit `gc.discoveryAction`. | UI-Button auf neuen Namen mappen; Verhalten unverändert halten. | Keine UI-Registrierung mehr auf `discoveryAction`. | **v1.2 (M2)** UI-Migration; **v2.0 (M3)** Entfernen. |

## 4) Empfohlene Umsetzung der Bereinigung

1. **M1 (v1.1)**: Deprecation-Warnungen für alle Legacy-Einstiege/Import-Shims ergänzen.
2. **M2 (v1.2)**: Interne Aufrufer (UI-Registrierungen, Controller-Bootstrap, WebUI) vollständig auf neue Namen migrieren.
3. **M3 (v2.0)**: Entfernen aller dokumentierten Legacy-Aliase/Shims in einem gebündelten Breaking-Release.

## 5) Validierungs-Checkliste vor Entfernen

- Repo-weite Suche findet keine Legacy-Importe/Methodenaufrufe mehr.
- Downstream-Integrationen (falls vorhanden) wurden via Release-Notes vorab informiert.
- Ein Major-Release mit Migrationshinweisen ist veröffentlicht.
