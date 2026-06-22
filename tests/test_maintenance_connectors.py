from __future__ import annotations

from pathlib import Path

from MCUM.core import connector_health
from MCUM.db import project_registry


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


def _wire(monkeypatch, cursor):
    monkeypatch.setattr(project_registry, "get_db", lambda: Context(object()))
    monkeypatch.setattr(project_registry, "get_cursor", lambda _conn: Context(cursor))


def test_reaper_closes_only_expired_queued_or_running_rows(monkeypatch) -> None:
    cursor = Cursor([{"id": "run-1", "status": "failure"}])
    _wire(monkeypatch, cursor)

    result = project_registry.reap_stale_maintenance_runs(max_age_minutes=30)

    assert result["reaped_count"] == 1
    assert "status IN ('queued', 'running')" in cursor.sql
    assert "make_interval(mins => %s)" in cursor.sql
    assert cursor.params == (30, 30)


def test_register_connector_is_idempotent(monkeypatch) -> None:
    cursor = Cursor([{"connector_key": "dashboard-local"}])
    _wire(monkeypatch, cursor)

    key = project_registry.register_connector(
        connector_key="dashboard-local",
        connector_type="dashboard",
        display_name="Dashboard",
    )

    assert key == "dashboard-local"
    assert "ON CONFLICT (connector_key) DO UPDATE" in cursor.sql


def test_record_connector_health_event_persists_normalized_payload(monkeypatch) -> None:
    cursor = Cursor([{"id": "event-1"}])
    _wire(monkeypatch, cursor)

    event_id = project_registry.record_connector_health_event(
        connector_key="dashboard-local",
        status="configured",
        latency_ms=12,
        message="ok",
        metadata={"probe": True},
    )

    assert event_id == "event-1"
    assert "connector_health_events" in cursor.sql
    assert cursor.params[0] == "dashboard-local"
    assert cursor.params[2] == "configured"


def test_get_connector_health_summary_returns_latest_rows(monkeypatch) -> None:
    cursor = Cursor([{"connector_key": "dashboard-local", "status": "configured"}])
    _wire(monkeypatch, cursor)

    rows = project_registry.get_connector_health_summary()

    assert rows[0]["connector_key"] == "dashboard-local"
    assert "DISTINCT ON (registry.connector_key)" in cursor.sql


def test_local_probe_reports_configured_and_missing(tmp_path: Path) -> None:
    existing = tmp_path / "bridge.py"
    existing.write_text("pass", encoding="utf-8")
    spec = {"connector_key": "bridge", "relative_path": "bridge.py"}

    assert connector_health.probe_local_connector(spec, root=tmp_path)["status"] == "configured"
    existing.unlink()
    assert connector_health.probe_local_connector(spec, root=tmp_path)["status"] == "failed"


def test_connector_sync_records_failures_without_raising(monkeypatch, tmp_path: Path) -> None:
    registered = []
    events = []
    monkeypatch.setattr(
        connector_health,
        "LOCAL_CONNECTORS",
        (
            {
                "connector_key": "missing",
                "connector_type": "bridge",
                "display_name": "Missing",
                "relative_path": "missing.py",
            },
        ),
    )
    monkeypatch.setattr(
        connector_health,
        "register_connector",
        lambda **kwargs: registered.append(kwargs) or kwargs["connector_key"],
    )
    monkeypatch.setattr(
        connector_health,
        "record_connector_health_event",
        lambda **kwargs: events.append(kwargs) or "event",
    )
    monkeypatch.setattr(
        connector_health,
        "probe_postgresql",
        lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    result = connector_health.sync_local_connector_health(root=tmp_path)

    assert result["connectors_checked"] == 2
    assert result["failed_count"] == 2
    assert len(registered) == len(events) == 2


def test_postgresql_probe_reports_connected(monkeypatch) -> None:
    cursor = Cursor([{"ok": 1}])
    monkeypatch.setattr(connector_health, "get_db", lambda: Context(object()))
    monkeypatch.setattr(connector_health, "get_cursor", lambda _conn: Context(cursor))

    result = connector_health.probe_postgresql()

    assert result["status"] == "connected"
    assert result["connector_key"] == "postgresql-local"
