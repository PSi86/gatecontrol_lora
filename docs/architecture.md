# Architektur: Core vs. Plugins

## Top-Level-Struktur

- `core/`: Domain, Application-Services und host-neutrale Ports.
- `adapters/`: host-neutrale Adapter-/Port-Definitionen (z. B. generische Config/Event/UI-Ports).
- `plugins/rotorhazard/`: sämtliche RotorHazard-spezifische Implementierungen (Bootstrap, Host-Provider, RH-WebUI, RH-UI-Extensions).
- `plugins/standalone/`: Standalone-Host-Implementierung.

## Core-vs-Plugin-Regeln

1. `core/*` darf **keine** hostspezifischen Imports enthalten.
   - Kein `RHUI`, kein `eventmanager.Evt`, kein `rhapi`-spezifischer Code.
2. RotorHazard-spezifische Integrationen liegen ausschließlich in `plugins/rotorhazard/*`.
3. Host-neutrale Adapter/Ports liegen in `adapters/*`.
4. Außerhalb von `plugins/rotorhazard/*` sind RotorHazard-UI-Imports verboten.
   - Beispiel: kein `RHUI`-Import außerhalb `plugins/rotorhazard`.
5. Feature-Wiring für einen Host erfolgt im jeweiligen Plugin-Bootstrap/Runtime, nicht im Core.

## Migrationshinweise

- RH-Race-Provider und RH-Event-Adapter wurden nach `plugins/rotorhazard/host/` verschoben.
- Die produktive Presentation-Schicht (Blueprint + Templates + Static Assets) liegt unter `plugins/rotorhazard/presentation/`.
- Das Top-Level-Verzeichnis `presentation/` ist **kein** Laufzeit- oder Packaging-Entry-Point und wurde entfernt, um den Importpfad eindeutig auf `plugins.rotorhazard.presentation` zu halten.
- Die frühere Kompatibilitätsschicht `platform/*` wurde im Major-Cleanup am 2026-04-02 entfernt; gültige Importpfade sind `plugins.rotorhazard.*`, `plugins.standalone.*` und `adapters.ports`.

## Ist-Stand der Abhängigkeiten

Der aktuelle Stand ist bewusst nicht vollständig „clean architecture“-rein, sondern enthält noch pragmatische Kopplungen:

- `core/app/racelink_app.py` nutzt aktuell transport-spezifische Konstanten/Funktionen.
  - Das ist funktional stabil, aber eine direkte Abhängigkeit von konkreter Protokoll-/Transportlogik.
- `core/services/*` sind derzeit nicht durchgängig port-basiert.
  - Einzelne Services arbeiten teilweise direkt mit konkreten Transport- bzw. Domainobjekten statt ausschließlich über abstrahierte Ports.
- `controller.py` dient als Fassade plus Kompatibilitätsschicht.
  - Die Datei delegiert an die neuen Komponenten, bündelt aber weiterhin Legacy-Einstiegspunkte und Aufrufmuster.

Diese Punkte sind bekannt und werden schrittweise reduziert, ohne die Betriebsstabilität zu gefährden.

## Target Architecture

### Kurzfristig pragmatisch erlaubt

- `controller.py` bleibt vorerst als stabile Fassade/Kompatibilitätsschicht bestehen.
- Bestehende direkte Aufrufe in `core/app/racelink_app.py` und ausgewählten Services dürfen temporär bestehen bleiben, wenn sie Release-Risiko senken.
- Priorität bleibt: keine host-spezifischen Imports in `core/*`; interne technische Schulden werden inkrementell abgebaut.

### In Ports zu verschiebende Abhängigkeiten

Folgende fachlich-technischen Abhängigkeiten sollen aus Core-Implementierungen in Port-Verträge/Adapter verschoben werden:

- ACK-/Opcode-Logik (Protokolldetails statt Anwendungskern).
- Address-Normalisierung (kanonische Adressaufbereitung als klar definierter Port/Utility-Vertrag).
- Weitere transport-spezifische Hilfsfunktionen/Konstanten, sobald sie klar als Infrastruktur-Verantwortung isoliert werden können.

Ziel: Services und App-Layer arbeiten primär gegen Port-Interfaces; konkrete Transport-/Host-Details verbleiben in Adaptern/Plugins.

### Migrationsreihenfolge (kleine Schritte)

1. **Bestandsaufnahme pro Use Case**
   - Direkte Transport-/Domainkopplungen in `core/app` und `core/services` identifizieren und markieren.
2. **Port-Schnittstellen ergänzen**
   - Für ACK/Opcode, Address-Normalisierung und ähnliche Querschnittslogik minimale Ports definieren.
3. **Adapter inkrementell einführen**
   - Zuerst read-only bzw. risikoarme Pfade auf neue Ports umstellen, dann schreibende/zeitkritische Pfade.
4. **Fassade stabil halten**
   - `controller.py` delegiert weiter; Aufrufer bleiben unverändert, während intern schrittweise umgestellt wird.
5. **Legacy-Pfade entfernen**
   - Nach erfolgreicher Nutzung der Ports direkte Kopplungen und Übergangscode in kleinen PRs abbauen.
