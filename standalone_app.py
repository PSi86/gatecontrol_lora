from __future__ import annotations

try:
    from .platform.flask_adapter import FlaskStandaloneAdapter
except Exception:  # pragma: no cover
    from platform.flask_adapter import FlaskStandaloneAdapter


def create_app():
    adapter = FlaskStandaloneAdapter()
    adapter.initialize()
    app = adapter.create_app()
    app.config["RACELINK_ADAPTER"] = adapter
    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5055, debug=True)
