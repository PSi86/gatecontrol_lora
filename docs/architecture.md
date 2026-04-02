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
- RH-WebUI (Blueprint + Templates + Static Assets) liegt unter `plugins/rotorhazard/presentation/`.
- `platform/*` bleibt nur als Kompatibilitätsschicht bestehen und re-exportiert neue Pfade.
