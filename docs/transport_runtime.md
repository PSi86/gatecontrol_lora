# Transport-Runtime: `LoRaTransportAdapter`

Dieses Dokument beschreibt die Laufzeit-Logik im Transport-Layer so, dass Fehleranalyse ohne tiefe Code-Inspektion möglich ist.

Scope: `infrastructure/lora_transport_adapter.py`.

## 1) Hook-Installation (Event-/TX-Callback)

### Relevante Funktionen
- `install_hooks()`
- `discover_port(args)`

### Zustände & Transitionen

| Zustand | Trigger | Transition | Beobachtbares Verhalten |
|---|---|---|---|
| `hooks_installed = False` | erfolgreicher Port-Open in `discover_port()` | `install_hooks()` wird aufgerufen | Event-/TX-Callbacks werden genau einmal registriert |
| `hooks_installed = False` | direkter Aufruf `install_hooks()` bei vorhandener `lora` Instanz | `add_listener(_on_transport_event)` und optional `add_tx_listener(_on_transport_tx)` | Adapter kann RX/TX-Events für Status/Online-Tracking auswerten |
| `hooks_installed = True` | erneuter Aufruf `install_hooks()` | keine Änderung (früher Return) | keine Doppel-Registrierung, kein Event-Duplikat |
| `lora = None` | `install_hooks()` | keine Änderung (früher Return) | kein Hooking möglich, Transport gilt nicht als „ready“ |

### Troubleshooting-Hinweise
- **Symptom:** Kein ACK-/Status-Tracking trotz offenem Port.  
  **Prüfen:** Wurde `discover_port()` erfolgreich abgeschlossen (inkl. `start()` + `install_hooks()`)?
- **Symptom:** Events werden mehrfach verarbeitet.  
  **Prüfen:** Externe/zusätzliche Listener-Registrierungen außerhalb des Adapters.

---

## 2) RX-Window-Verhalten (`wait_rx_window`)

### Relevante Funktionen
- `wait_rx_window(send_fn, collect_pred=None, fail_safe_s=8.0)`
- `send_and_wait_for_reply(...)` (nutzt `wait_rx_window`)

### Zustände & Transitionen

| Zustand | Trigger | Transition | Ergebnis |
|---|---|---|---|
| `idle` | Aufruf `wait_rx_window(...)` | Listener-Modus oder Polling-Modus wird gewählt | Sammeln von Events startet |
| `waiting_for_close` | `send_fn()` wurde ausgeführt | wartet auf `EV_RX_WINDOW_CLOSED` oder Timeout | RX-Fenster wird logisch abgeschlossen |
| `collecting` | Event erfüllt `collect_pred` | Event wird in `collected` aufgenommen | z. B. ACK/Response wird mitgezählt |
| `window_closed` | `EV_RX_WINDOW_CLOSED` empfangen | `got_closed = True`, Return | schneller Abschluss ohne vollen Timeout |
| `timeout_failsafe` | kein CLOSE bis `fail_safe_s` | Return mit `got_closed=False` | Schutz vor Blockieren bei verlorenen Events |

### Betriebsmodi
1. **Listener-basiert (bevorzugt):** `add_listener/remove_listener` vorhanden → `threading.Event` wartet auf CLOSE.
2. **Fallback-Polling:** `drain_events(timeout_s=0.1)`-Loop bis CLOSE oder Timeout.

### Troubleshooting-Hinweise
- **Symptom:** Requests blockieren „zu lang“.  
  **Prüfen:** CLOSE-Event kommt an? Falls nicht, greift der `fail_safe_s`-Timeout.
- **Symptom:** ACK vorhanden, aber nicht als Antwort gezählt.  
  **Prüfen:** `collect_pred`-Filter (Sender/Opcode/Policy) trifft wirklich zu.

---

## 3) ACK-Policy & Reply-Matching

### Relevante Funktionen
- `send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s)`
- `_on_transport_tx(ev)`
- `_pending_try_match(ev)`
- `_pending_window_closed()`

### Kernlogik
- Policy kommt aus `LPA.find_rule(opcode7)`:
  - `RESP_NONE`: sofortiger Return ohne Warten.
  - `RESP_ACK`: erwartet `opc == LP.OPC_ACK` und `ack_of == opcode7`.
  - `RESP_SPECIFIC`: erwartet spezifisches `rsp_opcode7`.
- Optionaler Sender-Filter: bei unicast `recv3 != FF:FF:FF` werden nur Replies dieses Knotens akzeptiert.

### Zustände & Transitionen

| Zustand | Trigger | Transition | Ergebnis |
|---|---|---|---|
| `no_expectation` | `_on_transport_tx` für M2N-Frame mit Antwort-Policy | `_pending_expect` wird gesetzt | Device ist „wartend auf Reply“ |
| `pending_reply` | passendes ACK/spezifische Reply in `_pending_try_match` | Device `mark_online()`, `_pending_expect=None` | Link gilt als bestätigt |
| `pending_reply` | RX-Window endet ohne Match (`_pending_window_closed`) | Device `mark_offline("Missing reply (...)")`, `_pending_expect=None` | Link wird als verloren markiert |

### Troubleshooting-Hinweise
- **Symptom:** Device springt nach Send regelmäßig auf offline.  
  **Prüfen:** Antwort-Policy/Opcode stimmt zur Firmware? ACK `ack_of` korrekt?
- **Symptom:** Broadcast-Sends liefern „keine“ Responses.  
  **Prüfen:** Bei Broadcast ist Sender nicht vorgefiltert; Antwort-Erwartung hängt von Rule/Policy ab.

---

## 4) Reconnect-Logik bei Transportfehlern

### Relevante Funktionen
- `_on_transport_event(ev)`
- `_notify_disconnect(reason)`
- `_schedule_reconnect(reason)`
- `discover_port({})`

### Zustände & Transitionen

| Zustand | Trigger | Transition | Ergebnis |
|---|---|---|---|
| `connected` | `EV_ERROR` | `_notify_disconnect()` + `_schedule_reconnect()` | UI/Host bekommt Disconnect-Signal |
| `reconnect_guard` | Reconnect läuft bereits ODER letzter Versuch <5s | Abbruch (kein neuer Thread) | schützt vor Reconnect-Sturm |
| `reconnecting` | Guard erlaubt neuen Versuch | Daemon-Thread: `lora.close()` → `lora=None` → `discover_port({})` | Neuinitialisierung des Ports |
| `reconnect_done` | Thread-Finally | `_reconnect_in_progress=False` | neuer Versuch wieder möglich |

### Zusätzliche Schutzmechanismen
- UI-Disconnect-Notify wird intern auf >2s gedrosselt (`_last_error_notify_ts`), um Meldungs-Spam zu vermeiden.

### Troubleshooting-Hinweise
- **Symptom:** Nach Fehler kein automatischer Wiederaufbau.  
  **Prüfen:** Guard-Bedingungen (5s Cooldown / laufender Reconnect) und ob `discover_port({})` wieder Erfolg hat.
- **Symptom:** Viele Fehler-Meldungen in kurzer Zeit.  
  **Prüfen:** Wiederholte `EV_ERROR`-Quelle im USB/Serial-Layer; Adapter drosselt nur User-Notify, nicht die Fehlerursache.
