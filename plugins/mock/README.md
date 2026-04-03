# Mock Plugin (Reference)

Dieses Plugin ist eine **bewusst minimale, funktional inerte Referenz** für neue Plugin-Entwicklung.

## Ziel

- API-kompatibler `MockRaceProvider` zum `RaceProviderPort`
- Minimale Runtime mit `build()`, `start()` und `stop()`
- Keine produktive Host-Integration und keine Seiteneffekte
- Bei `start()`/`stop()` nur Logging-Ausgaben

## Verwendung

```python
from plugins.mock import MockPluginRuntime

runtime = MockPluginRuntime.build()
runtime.start()  # logs: "Mock plugin started"
runtime.stop()   # logs: "Mock plugin stopped"
```

Damit kann das Plugin importiert und instanziiert werden, ohne echte Host-Abhängigkeiten.
