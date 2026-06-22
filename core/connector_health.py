"""Bounded local connector registration and health probes."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..db.connection import get_cursor, get_db
from ..db.project_registry import register_connector, record_connector_health_event


ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONNECTORS = (
    {
        "connector_key": "dashboard-local",
        "connector_type": "dashboard",
        "display_name": "MCUM Dashboard Local",
        "relative_path": "integrations/dashboard/server.py",
    },
    {
        "connector_key": "openclaw-local",
        "connector_type": "bridge",
        "display_name": "OpenClaw Local Bridge",
        "relative_path": "integrations/openclaw/openclaw_bridge.py",
    },
    {
        "connector_key": "antigravity-local",
        "connector_type": "mcp",
        "display_name": "Antigravity Local MCP",
        "relative_path": "integrations/antigravity/mcum_local_mcp_stdio.mjs",
    },
)


def probe_local_connector(spec: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    started = time.perf_counter()
    path = root / str(spec["relative_path"])
    exists = path.is_file()
    return {
        "connector_key": str(spec["connector_key"]),
        "status": "configured" if exists else "failed",
        "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "message": "local connector artifact available" if exists else "local connector artifact missing",
        "metadata": {"path": str(path), "exists": exists},
    }


def probe_postgresql() -> dict[str, Any]:
    started = time.perf_counter()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT 1 AS ok")
            ok = bool((cur.fetchone() or {}).get("ok"))
    return {
        "connector_key": "postgresql-local",
        "status": "connected" if ok else "failed",
        "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "message": "PostgreSQL responded" if ok else "PostgreSQL probe failed",
        "metadata": {},
    }


def sync_local_connector_health(*, project_id: str | None = None, root: Path = ROOT) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    specs = [
        *LOCAL_CONNECTORS,
        {
            "connector_key": "postgresql-local",
            "connector_type": "database",
            "display_name": "PostgreSQL Local",
            "relative_path": "",
        },
    ]
    for spec in specs:
        register_connector(
            connector_key=str(spec["connector_key"]),
            connector_type=str(spec["connector_type"]),
            display_name=str(spec["display_name"]),
            health_mode="probe",
            metadata={"relative_path": str(spec.get("relative_path") or "")},
        )
        try:
            probe = (
                probe_postgresql()
                if spec["connector_key"] == "postgresql-local"
                else probe_local_connector(spec, root=root)
            )
        except Exception as exc:
            probe = {
                "connector_key": str(spec["connector_key"]),
                "status": "failed",
                "latency_ms": None,
                "message": str(exc),
                "metadata": {"probe_error": type(exc).__name__},
            }
        record_connector_health_event(project_id=project_id, **probe)
        probes.append(probe)
    return {
        "status": "success",
        "connectors_checked": len(probes),
        "failed_count": sum(1 for item in probes if item["status"] == "failed"),
        "probes": probes,
    }
