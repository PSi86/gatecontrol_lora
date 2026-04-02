# WebUI-Runtime: SSE-Master-State & Task-Busy-Mechanik

Dieses Dokument fasst die Live-Runtime der RaceLink-WebUI zusammen, insbesondere Master-State via SSE und die Busy-Absicherung für Long-Running-Tasks.

Scope: `plugins/rotorhazard/presentation/racelink_webui.py`.

## 1) SSE-Master-State

### Relevante Funktionen
- `_ensure_transport_hooked()`
- `_on_transport_event(ev)`
- `_set_master(**updates)`
- `_broadcast(ev_name, payload)`
- `api_events()` (`GET /racelink/api/events`)
- `api_master()` (`GET /racelink/api/master`)

### Master-Zustand (Datenmodell)
`_master` enthält u. a.:
- `state`: `IDLE | TX | RX | ERROR`
- `tx_pending`: TX läuft/steht aus
- `rx_window_open`, `rx_windows`, `rx_window_ms`
- `last_event`, `last_event_ts`, `last_tx_len`, `last_rx_count_delta`, `last_error`

### Transitionen (Event-getrieben)

| Eingang | Vorbedingung | Transition im Master-State | Auslöser-Funktion |
|---|---|---|---|
| `EV_RX_WINDOW_OPEN` | Transport-Event empfangen | `state -> RX` (falls `rx_windows == 1`), `rx_window_open=True`, `last_event="RX_WINDOW_OPEN"` | `_on_transport_event` |
| `EV_RX_WINDOW_CLOSED` | Transport-Event empfangen | `rx_window_open=False`, `rx_window_ms=0`, `last_rx_count_delta` gesetzt, State zurück auf `TX` oder `IDLE` | `_on_transport_event` |
| `EV_TX_DONE` | TX abgeschlossen | `tx_pending=False`, `state -> RX` (wenn Fenster offen) sonst `IDLE`, `last_event="TX_DONE"` | `_on_transport_event` |
| `EV_ERROR` | USB/Transportfehler | `state="ERROR"`, `last_event="USB_ERROR"`, `last_error` gesetzt | `_on_transport_event` |
| LoRa-Reply (`reply` gesetzt) | Parser-Event statt EV_* | `last_event` auf Reply-Namen (z. B. `ACK`, `STATUS_REPLY`) | `_on_transport_event` |

### SSE-Fluss
1. Client verbindet sich auf `GET /racelink/api/events`.
2. Server registriert Queue in `_clients` und sendet Initial-Snapshots (`master`, `task`).
3. Änderungen laufen über `_set_master()` / `_task_update()` → `_broadcast()`.
4. Stream liefert `event: master`, `event: task`, optional `event: refresh`.
5. Keepalive: `: ping` etwa alle 15s.

### Troubleshooting-Hinweise
- **Symptom:** UI bleibt auf `IDLE`, obwohl LoRa aktiv ist.  
  **Prüfen:** Wurde `_ensure_transport_hooked()` erfolgreich ausgeführt (Listener installiert)?
- **Symptom:** UI zeigt alte Daten.  
  **Prüfen:** SSE-Verbindung zu `/racelink/api/events`; alternativ Snapshot via `/racelink/api/master` abrufen.

---

## 2) Task-Busy-Mechanik (Single-Task-Gate)

### Relevante Funktionen
- `_task_is_running()`
- `_task_busy_response()`
- `_start_task(name, target_fn, meta=None)`
- `_set_task(...)`, `_task_update(...)`

### Zustände & Transitionen

| Zustand | Trigger | Transition | Ergebnis |
|---|---|---|---|
| `no_task` (`_task is None`) | `_start_task(...)` | Task-Objekt wird mit `state="running"` erzeugt | Task startet in eigenem Thread |
| `running` | weiterer API-Task-Request | HTTP `409` via `_task_busy_response()` | Parallel-Task wird verhindert |
| `running` | `target_fn` erfolgreich | `state="done"`, `ended_ts`, `result` gesetzt | UI kann Abschluss + Ergebnis anzeigen |
| `running` | Exception in `target_fn` | `state="error"`, `last_error`, Master `ERROR` | Fehler sichtbar in Task- und Master-Ansicht |

### Wichtige Nebenwirkungen beim Start/Ende
- Beim Task-Start: `_set_master(state="TX", tx_pending=True, last_event="TASK_*_START")`
- Bei Erfolg: `_set_master(state="IDLE" oder "RX", last_event="TASK_*_DONE")` + `refresh(groups,devices)`
- Bei Fehler: `_set_master(state="ERROR", last_event="TASK_*_ERROR", last_error=...)`

### Task-Metriken für Diagnose
Während laufender Tasks aktualisiert `_on_transport_event`:
- `rx_window_events`
- `rx_count_delta_total`
- `rx_replies` (z. B. bei `discover` für `IDENTIFY_REPLY`, bei `status` für `STATUS_REPLY`)

### Troubleshooting-Hinweise
- **Symptom:** API-Aktion liefert sofort „busy“.  
  **Prüfen:** `/racelink/api/task` für laufenden Task und dessen `name/state`.
- **Symptom:** Task hängt gefühlt ohne Fortschritt.  
  **Prüfen:** `rx_window_events`, `rx_replies`, `last_error` im Task-Snapshot.

---

## 3) Relevante Runtime-Endpunkte (`racelink_webui.py`)

### Live-Status / Streams
- `GET /racelink/api/events`  
  SSE-Kanal (`master`, `task`, `refresh`) für Live-UI.
- `GET /racelink/api/master`  
  Snapshot von Master + Task in einem Call.
- `GET /racelink/api/task`  
  Nur Task-Snapshot (für Busy-/Progress-Diagnose).

### Typische Task-Start-Endpunkte (busy-geschützt)
- `POST /racelink/api/discover`
- `POST /racelink/api/status`

Diese Endpunkte prüfen vor dem Start jeweils `_task_is_running()` und liefern bei Konflikt `409` + `{"busy": true, "task": ...}`.

### Weitere busy-geschützte Schreib-Endpoints
In der Datei sind zusätzliche POST-Aktionen mit demselben Busy-Gate umgesetzt (z. B. für Config/Control/Special/FW-Task-Flows). Für Troubleshooting gilt dasselbe Muster:
1. Request kommt an,
2. Busy-Check,
3. ggf. `_start_task(...)` oder sofortiger Busy-Response,
4. Fortschritt über SSE/`/api/task`.
