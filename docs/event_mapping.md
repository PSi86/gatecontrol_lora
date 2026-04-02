# Event-Mapping: Externe Race-Events → interne RaceLink-Aktionen

Diese Zuordnung beschreibt den neuen `RaceProviderPort`-basierten Fluss.

## RotorHazard (`providers/rotorhazard_provider.py`)

- `Evt.RACE_START` → `RaceLink_LoRa.onRaceStart(args)`
- `Evt.RACE_FINISH` → `RaceLink_LoRa.onRaceFinish(args)`
- `Evt.RACE_STOP` → `RaceLink_LoRa.onRaceStop(args)`

Die Registrierung erfolgt in `platform/rh_adapter.py` über:

- `race_provider.on_race_start(...)`
- `race_provider.on_race_finish(...)`
- `race_provider.on_race_stop(...)`

## Daten-Mapping für Startblock

Der Startblock liest keine RH-internen Objekte mehr direkt aus, sondern über den Provider-Port:

- `RaceProviderPort.get_frequency_channels()` → Kanal-Labels pro Node (`R1`, `F4`, `--`, ...)
- `RaceProviderPort.get_pilot_assignments()` → Slot-/Pilot-Zuordnung (`(node_index, callsign)`)

`StartblockService.get_current_heat_slot_list()` kombiniert beide Datenquellen zu:

- `(slot_index_0based, callsign, racechannel)`

Damit kann derselbe Service mit RotorHazard und mit Fremdsoftware (`providers/mock_provider.py`) betrieben werden.
