from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from MCUM.integrations.dashboard.data_service import (
    DashboardDataService,
    normalize_status,
    redact_secrets,
)
from MCUM.integrations.dashboard.server import create_server
from MCUM.integrations.dashboard import pg_backend


NOW = datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)


def test_redact_secrets_handles_nested_keys_and_inline_values() -> None:
    source = {
        "connector_key": "minimax",
        "api_key": "secret-value",
        "nested": {
            "authorization": "Bearer abc.def",
            "url": "postgresql://user:pass@localhost/db?token=visible",
            "message": "request failed api_key=also-visible",
        },
    }

    result = redact_secrets(source)

    assert result["connector_key"] == "minimax"
    assert result["api_key"] == "[redacted]"
    assert result["nested"]["authorization"] == "[redacted]"
    assert "user:pass" not in result["nested"]["url"]
    assert "visible" not in result["nested"]["url"]
    assert "also-visible" not in result["nested"]["message"]
    assert source["api_key"] == "secret-value"


def test_normalize_status_requires_recent_activity_for_connected() -> None:
    recent = (NOW - timedelta(seconds=30)).isoformat()
    stale = (NOW - timedelta(minutes=20)).isoformat()

    assert normalize_status({"status": "connected", "last_heartbeat_at": recent}, now=NOW) == "connected"
    assert normalize_status({"status": "connected", "last_heartbeat_at": stale}, now=NOW) == "configured"
    assert normalize_status({"enabled": False, "last_heartbeat_at": recent}, now=NOW) == "disabled"
    assert normalize_status({"status": "failed", "last_heartbeat_at": recent}, now=NOW) == "failed"
    assert normalize_status({"status": "degraded", "last_heartbeat_at": recent}, now=NOW) == "degraded"
    assert normalize_status({"status": "connected", "updated_at": recent}, now=NOW) == "configured"
    assert normalize_status({}, now=NOW) == "unknown"


def test_data_service_uses_injected_backend_and_redacts_output() -> None:
    service = DashboardDataService(
        {
            "connectors": [
                {
                    "connector_key": "codex",
                    "configured": True,
                    "last_invocation_at": NOW.isoformat(),
                    "token": "do-not-return",
                }
            ],
            "agents": [{"name": "builder", "enabled": True}],
            "graph": {"status": "healthy", "last_activity_at": NOW.isoformat(), "password": "hidden"},
        },
        now=lambda: NOW,
    )

    assert service.get_connectors()["items"][0]["status"] == "connected"
    assert service.get_connectors()["items"][0]["token"] == "[redacted]"
    assert service.get_agents()["items"][0]["status"] == "configured"
    assert service.get_graph()["password"] == "[redacted]"
    assert service.get_summary()["connector_statuses"]["connected"] == 1


def test_data_service_accepts_backend_object_methods() -> None:
    class Backend:
        def get_connectors(self):
            return [{"name": "PowerShell", "configured": True}]

    service = DashboardDataService(Backend(), now=lambda: NOW)

    assert service.get_connectors()["items"][0]["name"] == "PowerShell"


def test_pg_backend_builds_the_dashboard_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        pg_backend,
        "fetch_graph_state",
        lambda project_path: {
            "status": "active",
            "files": 10,
            "nodes": 20,
            "relations": 30,
            "communities": 2,
            "snapshots": 4,
        },
    )
    monkeypatch.setattr(pg_backend, "fetch_connectors", lambda: [{"connector_key": "postgresql-local"}])
    monkeypatch.setattr(pg_backend, "fetch_agents", lambda project_path: [{"name": "builder"}])
    monkeypatch.setattr(
        pg_backend,
        "fetch_operational_summary",
        lambda: {"projects_active": 3, "tasks_total": 8, "tokens_total": 1200},
    )

    backend = pg_backend.build_backend("C:/repo")

    assert backend["graph"]["files"] == 10
    assert backend["graph"]["communities"] == 2
    assert backend["summary"]["projects_active"] == 3
    assert backend["summary"]["tasks_total"] == 8
    assert backend["summary"]["tokens_total"] == 1200


def _get_json(base_url: str, path: str) -> tuple[int, dict]:
    with urlopen(f"{base_url}{path}", timeout=3) as response:
        return response.status, json.loads(response.read())


def test_server_exposes_basic_read_only_endpoints() -> None:
    service = DashboardDataService(
        {
            "summary": {"tasks_total": 4},
            "connectors": [{"name": "Codex", "configured": True}],
            "agents": [],
            "graph": {"status": "stale"},
        },
        now=lambda: NOW,
    )
    server = create_server(data_service=service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ("/api/health", "/api/summary", "/api/connectors", "/api/agents", "/api/graph"):
            status, payload = _get_json(base_url, path)
            assert status == 200
            assert isinstance(payload, dict)

        request = Request(f"{base_url}/api/connectors", method="POST", data=b"{}")
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            assert exc.code == 405
            assert exc.headers["Allow"] == "GET"
        else:
            raise AssertionError("POST endpoint must be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_server_does_not_expose_backend_exception_details() -> None:
    service = DashboardDataService(lambda name: (_ for _ in ()).throw(RuntimeError("api_key=secret")))
    server = create_server(data_service=service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/api/connectors", timeout=3)
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            assert exc.code == 503
            assert "secret" not in body
        else:
            raise AssertionError("Failing backend must return 503")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
