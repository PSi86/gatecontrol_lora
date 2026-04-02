# Event-Mapping: Host-Events → internes RaceLink-Eventmodell

RaceLink verarbeitet Host-Race-Events jetzt ausschließlich über ein internes, generisches Eventmodell:

- `HostRaceEventType.RACE_STARTED`
- `HostRaceEventType.RACE_FINISHED`
- `HostRaceEventType.RACE_STOPPED`
- optional: `HostRaceEventType.RACE_SNAPSHOT`

Die Event-Port-Schnittstelle liegt in `core/ports/host_race_events.py` (`start(event_sink)`, `stop()`).

## RotorHazard-Integration (Event-basiert)

`providers/rotorhazard_provider.py` enthält den Adapter `RotorHazardRaceEventAdapter`:

- `Evt.RACE_START` → `RACE_STARTED`
- `Evt.RACE_FINISH` → `RACE_FINISHED`
- `Evt.RACE_STOP` → `RACE_STOPPED`

Die App-Orchestrierung (`core/app/racelink_app.py`) registriert den Event-Sink und dispatcht ausschließlich dieses interne Eventmodell.

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
