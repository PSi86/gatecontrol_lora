# Event-Mapping: Host-Events → internes RaceLink-Eventmodell

RaceLink verarbeitet Host-Race-Events jetzt ausschließlich über ein internes, generisches Eventmodell:

- `HostRaceEventType.RACE_STARTED`
- `HostRaceEventType.RACE_FINISHED`
- `HostRaceEventType.RACE_STOPPED`
- optional: `HostRaceEventType.RACE_SNAPSHOT`

Die Event-Port-Schnittstelle liegt in `core/ports/host_race_events.py` (`start(event_sink)`, `stop()`).

## RotorHazard-Integration (Event-basiert)

`plugins/rotorhazard/host/rotorhazard_provider.py` enthält den Adapter `RotorHazardRaceEventAdapter`:

- `Evt.RACE_START` → `RACE_STARTED`
- `Evt.RACE_FINISH` → `RACE_FINISHED`
- `Evt.RACE_STOP` → `RACE_STOPPED`

Die App-Orchestrierung (`core/app/racelink_app.py`) registriert den Event-Sink und dispatcht ausschließlich dieses interne Eventmodell.

## Standardisiertes Payload-Mapping (`source_payload`)

Die App verwendet für Race-Events ausschließlich `payload["source_payload"]` (falls vorhanden). Dadurch bleibt das Verhalten host-agnostisch.

Folgende Felder werden ausgewertet (mit Fallbacks/Defaults):

- `heat_id`: `source_payload.heat_id` oder `source_payload.current_heat` (`int | None`)
- `group_id`: `source_payload.rl_group_id` oder `source_payload.group_id` (Default `255` = Broadcast)
- `start_preset_id`: `source_payload.rl_start_preset_id` oder `source_payload.start_preset_id` (Default `1`)
- `start_brightness`: `source_payload.rl_start_brightness` oder `source_payload.start_brightness` (Default `70`)
- `finish_preset_id`: `source_payload.rl_finish_preset_id` oder `source_payload.finish_preset_id` (Default `2`)
- `finish_brightness`: `source_payload.rl_finish_brightness` oder `source_payload.finish_brightness` (Default `100`)
- `stop_preset_id`: `source_payload.rl_stop_preset_id` oder `source_payload.stop_preset_id` (Default `1`)
- `stop_brightness`: `source_payload.rl_stop_brightness` oder `source_payload.stop_brightness` (Default `0`)
- `trigger_startblock`: `source_payload.rl_trigger_startblock` (Default `True`)

## Konkrete Event → Aktion-Regeln

1. `RACE_STARTED`
   - Gruppensteuerung via `ControlService.apply_race_event_group_control(...)`
     - Zielgruppe: `group_id`
     - Szene: `start_preset_id`
     - Helligkeit: `start_brightness`
   - optionales Startblock-Update via `StartblockService.trigger_race_event(event_name="RACE_STARTED", ...)`, sofern `trigger_startblock=True`

2. `RACE_FINISHED`
   - Gruppensteuerung via `ControlService.apply_race_event_group_control(...)`
     - Zielgruppe: `group_id`
     - Szene: `finish_preset_id`
     - Helligkeit: `finish_brightness`
   - optionales Startblock-Update via `StartblockService.trigger_race_event(event_name="RACE_FINISHED", ...)`, sofern `trigger_startblock=True`

3. `RACE_STOPPED`
   - Gruppensteuerung via `ControlService.apply_race_event_group_control(...)`
     - Zielgruppe: `group_id`
     - Szene: `stop_preset_id`
     - Helligkeit: `stop_brightness` (typisch `0` zum Ausschalten)
   - `StartblockService.trigger_race_event(event_name="RACE_STOPPED", ...)` liefert bewusst `skipped/not_applicable` zurück (kein Startblock-Stream beim Stop).

## Integration ohne Host-Eventbus (Polling)

Als Referenz ist in `providers/mock_provider.py` ein minimaler Polling-Adapter enthalten:

- `MockPollingRaceEventAdapter(state_supplier, interval_s=0.5)`
- zyklische Abfrage über `state_supplier()`
- Zustandsvergleich (`race_state`) erzeugt dieselben internen Events:
  - `running` → `RACE_STARTED`
  - `finished` → `RACE_FINISHED`
  - `stopped/idle/ready` → `RACE_STOPPED`
- zusätzlich Snapshot-Event (`RACE_SNAPSHOT`) pro Poll-Zyklus

`state_supplier()` erwartet ein Dict wie z. B.:

```python
{"race_state": "running", "heat_id": 3}
```

## Daten-Mapping für Startblock

Der Startblock liest weiterhin host-agnostisch über `RaceProviderPort`:

- `get_frequency_channels()` → Kanal-Labels pro Node (`R1`, `F4`, `--`, ...)
- `get_pilot_assignments()` → Slot-/Pilot-Zuordnung (`(node_index, callsign)`)

`StartblockService.get_current_heat_slot_list()` kombiniert beides zu:

- `(slot_index_0based, callsign, racechannel)`
