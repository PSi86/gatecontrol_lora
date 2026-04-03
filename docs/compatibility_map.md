# Compatibility Map (Backward-Compat / Legacy APIs)

Stand: 2026-04-03.

Dieses Dokument erfasst bestehende Kompatibilitäts-Shims und Legacy-Aliase, benennt den Zielpfad (neuer API-Name), dokumentiert bekannte aktive Nutzung und definiert Bereinigungskriterien inkl. geplantem Meilenstein.

> Update 2026-04-03: Top-Level-`providers/*` wurde entfernt. Gültige Provider-Importpfade sind `plugins.rotorhazard.providers.*` (produktiver RH-Host) und `plugins.mock.providers.*` (Beispiel-/Dev-Plugin).

## 1) Backward-Compat Import-Shims (`platform/*`) — **historisch (entfernt)**

Die frühere Kompatibilitätsschicht unter `platform/*` wurde im Major-Cleanup am **2026-04-02** entfernt (`platform/__init__.py`, `platform/rh_adapter.py`, `platform/flask_adapter.py`, `platform/ports.py`).

| Altpfad / Shim | Typ | Zielpfad (neu) | Letzter bekannter Status | Migrationshinweis | Entfernung |
|---|---|---|---|---|---|
| `platform/rh_adapter.py` (`RotorHazardAdapter`) | Import-Reexport | `plugins.rotorhazard.bootstrap.RotorHazardAdapter` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe direkt auf `plugins.rotorhazard.bootstrap` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |
| `platform/flask_adapter.py` (`FlaskStandaloneAdapter`) | Import-Reexport | `plugins.standalone.flask_adapter.FlaskStandaloneAdapter` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe direkt auf `plugins.standalone.flask_adapter` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |
| `platform/ports.py` (`ConfigStorePort`, `EventBusPort`, `RacePilotDataProviderPort`, `UINotificationPort`) | Import-Reexport | `adapters.ports.*` | Shim war vorhanden, interne Nutzung nicht nachweisbar. | Importe auf `adapters.ports` umstellen. | **Entfernt am 2026-04-02 (Major-Cleanup)** |

## 2) Legacy-Methoden / Alias-Namen in `controller.py`

> Stand M2: Produktive Übergänge wurden auf explizite Controller-Weiterleitungen (`on_startup`, `discover_port`, UI-Registrierung, Device/Group-Aktionen, Comm-Methoden) umgestellt. `RaceLink_LoRa.__getattr__` ist nur noch Warn-/Fallback und soll nach Beobachtungsphase entfallen.

| Legacy-Name | Zielpfad (neu) | Aktiv genutzt von … (bekannt) | Migrationshinweis | Entfernungskriterium | Geplante Bereinigung |
|---|---|---|---|---|---|
| `onStartup(_args)` | `on_startup(args)` | Legacy-Wrapper vorhanden; RH-Startup-Hook nutzt Snake Case. | Externe Aufrufer auf `on_startup` umstellen. | Kein Event-Handler/Callsite referenziert `onStartup` mehr. | **M2 erledigt**, **M3** entfernen. |
| `discoverPort(args)` | `discover_port(args)` | Legacy-Wrapper vorhanden; Settings-Button nutzt Snake Case. | Externe Aufrufer auf `discover_port` umstellen. | Keine Callback-Registrierung mehr auf `discoverPort`. | **M2 erledigt**, **M3** entfernen. |
| `getDeviceFromAddress(addr)` | `get_device_from_address(addr)` | Legacy-Wrapper für Alt-Aufrufer (WebUI/Alt-Integrationen). | Neue Aufrufer nur Snake Case. | Keine CamelCase-Aufrufe mehr. | **M2** beibehalten, **M3** entfernen. |
| `forceGroups(args, sanityCheck)` | `force_groups(args, sanity_check)` | Legacy-Wrapper für Alt-Aufrufer. | Neue Aufrufer nur Snake Case. | Keine CamelCase-Aufrufe mehr. | **M2** beibehalten, **M3** entfernen. |

## 2.5) Provider-Default im Controller (Runtime-Verhalten)

| Verhalten | Status | Details | Migrationshinweis |
|---|---|---|---|
| Impliziter `MockRaceProvider` im Controller | **Entfernt am 2026-04-03** | `RaceLink_LoRa` importiert kein Top-Level-`providers.*` mehr. | Host-Plugin soll den gewünschten `RaceProviderPort` explizit injizieren. |
| Default ohne injizierten Provider | **Entfernt am 2026-04-03** | Kein Fallback mehr: `RaceLink_LoRa` erwartet einen injizierten `RaceProviderPort` und wirft sonst `ValueError`. | Host-Plugin muss immer einen hostspezifischen Provider (z. B. `plugins.rotorhazard.providers.RotorHazardRaceProvider`) übergeben. |

## 3) Legacy-Methoden / Alias-Namen in `plugins/rotorhazard/ui/host_ui_adapter.py`

> Stand M2: Produktive Callback-Registrierungen laufen über Snake-Case-Methoden. Legacy-CamelCase-Methoden bleiben als dünne Wrapper mit Warn-Logging.

| Legacy-Name | Zielpfad (neu) | Aktiv genutzt von … (bekannt) | Migrationshinweis | Entfernungskriterium | Geplante Bereinigung |
|---|---|---|---|---|---|
| `registerActions(args=None)` | `register_actions(args=None)` | Legacy-Wrapper; interne Aufrufer sind migriert. | Downstream-Aufrufer umstellen. | Keine Registrierungen/Calls mehr auf `registerActions`. | **M2 erledigt**, **M3** entfernen. |
| `createUiDevList()` | `create_ui_device_list()` | Legacy-Wrapper; Startup nutzt Snake Case. | Downstream-Aufrufer umstellen. | Keine Aufrufe mehr von `createUiDevList`. | **M2 erledigt**, **M3** entfernen. |
| `rl_createUiDevList(...)` | `create_filtered_ui_device_list(...)` | Legacy-Wrapper; Action-Registry nutzt Snake Case. | Downstream-Aufrufer umstellen. | Keine internen Aufrufe mehr auf `rl_createUiDevList`. | **M2 erledigt**, **M3** entfernen. |
| `createUiGroupList(exclude_static=False)` | `create_ui_group_list(exclude_static=False)` | Legacy-Wrapper; Startup/Discovery nutzen Snake Case. | Downstream-Aufrufer umstellen. | Keine internen Aufrufe mehr auf `createUiGroupList`. | **M2 erledigt**, **M3** entfernen. |
| `nodeSwitch(action, args=None)` | `node_switch_action(...)` | Legacy-Wrapper; Callback-Ziele sind Snake Case. | Downstream-Aufrufer umstellen. | Keine Callback-Targets mehr auf `nodeSwitch`. | **M2 erledigt**, **M3** entfernen. |
| `groupSwitch(action, args=None)` | `group_switch_action(...)` | Legacy-Wrapper; Callback-Ziele sind Snake Case. | Downstream-Aufrufer umstellen. | Keine Callback-Targets mehr auf `groupSwitch`. | **M2 erledigt**, **M3** entfernen. |
| `discoveryAction(args)` | `discover_devices_action(args)` | Legacy-Wrapper; Settings-Button nutzt Snake Case. | Downstream-Aufrufer umstellen. | Keine UI-Registrierung mehr auf `discoveryAction`. | **M2 erledigt**, **M3** entfernen. |

## 4) Empfohlene Umsetzung der Bereinigung

1. **M2 (jetzt)**: Interne Aufrufer vollständig auf Snake Case migriert, Legacy als Warn-Wrapper markiert.
2. **M2.5**: Laufzeit-Logs auswerten (`Legacy call ...`, `dynamic attribute fallback hit ...`).
3. **M3 (v2.0)**: Entfernen der verbliebenen Legacy-Aliase und `__getattr__`-Fallbacks.

## 5) Validierungs-Checkliste vor Entfernen

- Repo-weite Suche findet keine Legacy-Methodenaufrufe mehr.
- Laufzeit-Telemetrie/Logs zeigen keine Legacy-Warnungen über definierten Beobachtungszeitraum.
- Downstream-Integrationen wurden via Release-Notes informiert.
- Major-Release mit Migrationshinweisen ist veröffentlicht.
