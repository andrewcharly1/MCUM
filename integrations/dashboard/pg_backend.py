"""Read-only PostgreSQL backend for the local MCUM connectors dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from ...db.connection import get_cursor, get_db


ROOT = Path(__file__).resolve().parents[2]
PROJECT_PATH = os.environ.get("MCUM_PROJECT_PATH")
EXECUTION_POLICY = ROOT / "directives" / "execution_policy.json"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _query_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    return dict(row) if row else None


def _query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(row) for row in rows]


def _load_policy() -> dict[str, Any]:
    try:
        return json.loads(EXECUTION_POLICY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _project_filter(project_path: str | None) -> tuple[str, tuple[Any, ...]]:
    if not project_path:
        return "", ()
    return "WHERE p.project_path = %s", (project_path,)


def fetch_graph_state(project_path: str | None) -> dict[str, Any]:
    where, params = _project_filter(project_path)
    row = _query_one(
        f"""
        SELECT g.id, g.project_id, g.project_path, g.project_name, g.graph_version,
               g.status, g.mode, g.files_indexed, g.files_skipped, g.nodes_total,
               g.edges_total, g.tokens_indexed_estimate,
               g.tokens_context_saved_estimate, g.updated_at, g.finished_at
        FROM code_graph.graphs g
        JOIN project_registry.projects p ON p.id = g.project_id
        {where}
        ORDER BY g.updated_at DESC NULLS LAST
        LIMIT 1
        """,
        params,
    )
    if not row:
        return {"status": "unknown", "configured": True}
    snapshot = _query_one(
        """
        SELECT id, entity_count, relation_count, status, code_graph_version, created_at
        FROM mcum_graph.snapshots
        WHERE project_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (row["project_id"],),
    )
    snapshot_count = _query_one(
        "SELECT COUNT(*)::int AS count FROM mcum_graph.snapshots WHERE project_id = %s",
        (row["project_id"],),
    )
    analytics = _query_one(
        """
        SELECT
            ar.id,
            (SELECT COUNT(*)::int FROM mcum_graph.communities c
             WHERE c.run_id = ar.id) AS community_count,
            (SELECT COUNT(DISTINCT ec.entity_id)::int
             FROM mcum_graph.entity_communities ec
             JOIN mcum_graph.communities c ON c.id = ec.community_id
             WHERE c.run_id = ar.id) AS community_members,
            (SELECT COUNT(DISTINCT em.entity_id)::int FROM mcum_graph.entity_metrics em
             WHERE em.project_id = ar.project_id AND em.snapshot_id = ar.snapshot_id) AS metric_entities
        FROM mcum_graph.analytics_runs ar
        WHERE ar.project_id = %s AND ar.status = 'success'
        ORDER BY ar.created_at DESC
        LIMIT 1
        """,
        (row["project_id"],),
    ) or {}
    files_total = int(row.get("files_total") or row.get("files_indexed") or 0)
    files_indexed = int(row.get("files_indexed") or 0)
    entity_count = int((snapshot or {}).get("entity_count") or row.get("nodes_total") or 0)
    relation_count = int((snapshot or {}).get("relation_count") or row.get("edges_total") or 0)

    def _percentage(value: Any, total: int) -> int:
        return min(100, round((int(value or 0) / total) * 100)) if total else 0

    return {
        **row,
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "configured": True,
        "enabled": True,
        "last_invocation_at": _iso(row.get("updated_at")),
        "files": files_total,
        "nodes": entity_count,
        "relations": relation_count,
        "communities": int(analytics.get("community_count") or 0),
        "snapshots": int((snapshot_count or {}).get("count") or 0),
        "snapshot_id": str((snapshot or {}).get("id") or "") or None,
        "entity_count": entity_count,
        "relation_count": relation_count,
        "community_count": int(analytics.get("community_count") or 0),
        "snapshot_count": int((snapshot_count or {}).get("count") or 0),
        "snapshot_at": _iso((snapshot or {}).get("created_at")),
        "coverage": {
            "archivos indexados": _percentage(files_indexed, files_total),
            "metricas analiticas": _percentage(analytics.get("metric_entities"), entity_count),
            "membresia comunidades": _percentage(analytics.get("community_members"), entity_count),
        },
        "errors": (
            [{"code": "code_graph", "message": row["error_message"], "created_at": _iso(row.get("updated_at"))}]
            if row.get("error_message")
            else []
        ),
    }


def fetch_connectors() -> list[dict[str, Any]]:
    policy = _load_policy()
    runner_policy = dict(policy.get("worker_runner") or {})
    configured: dict[str, dict[str, Any]] = {
        "codex": {"connector_type": "host_agent", "display_name": "Codex", "enabled": True},
        "claude-code": {"connector_type": "host_agent", "display_name": "Claude Code", "enabled": True},
        "opencode": {"connector_type": "host_agent", "display_name": "OpenCode", "enabled": True},
        "antigravity": {"connector_type": "host_agent", "display_name": "Antigravity", "enabled": True},
        "powershell": {"connector_type": "runner", "display_name": "PowerShell", "enabled": True},
    }
    for key, value in runner_policy.items():
        if not isinstance(value, dict):
            continue
        configured[key.replace("_", "-")] = {
            "connector_type": "worker_runner",
            "display_name": key.replace("_", " ").title(),
            "enabled": bool(value.get("enabled", True)),
        }
    invocations = _query_all(
        """
        SELECT COALESCE(NULLIF(runner, ''), NULLIF(provider, ''), 'unknown') AS connector_key,
               MAX(started_at) AS last_invocation_at,
               COUNT(*) AS invocation_count,
               SUM(COALESCE(total_tokens, 0)) AS total_tokens,
               AVG(COALESCE(wall_clock_ms, 0))::float AS avg_wall_clock_ms,
               (ARRAY_AGG(outcome ORDER BY started_at DESC))[1] AS latest_outcome
        FROM project_registry.agent_invocations
        GROUP BY COALESCE(NULLIF(runner, ''), NULLIF(provider, ''), 'unknown')
        """
    )
    by_key = {str(item["connector_key"]).replace("_", "-"): item for item in invocations}
    persisted_health = _query_all(
        """
        SELECT DISTINCT ON (registry.connector_key)
               registry.connector_key, registry.connector_type, registry.display_name,
               registry.enabled, event.status, event.latency_ms, event.message,
               event.created_at AS last_health_at
        FROM project_registry.connector_registry registry
        LEFT JOIN project_registry.connector_health_events event
          ON event.connector_key = registry.connector_key
        ORDER BY registry.connector_key, event.created_at DESC NULLS LAST
        """
    )
    health_by_key = {
        str(item["connector_key"]).replace("_", "-"): item
        for item in persisted_health
    }
    items: list[dict[str, Any]] = []
    for key, item in sorted(configured.items()):
        activity = by_key.get(key, {})
        health = health_by_key.get(key, {})
        outcome = str(activity.get("latest_outcome") or "")
        items.append(
            {
                "connector_key": key,
                **item,
                "configured": True,
                "status": (
                    str(health.get("status"))
                    if health.get("status")
                    else "failed"
                    if outcome in {"failure", "failed"}
                    else "configured"
                ),
                "last_heartbeat_at": _iso(health.get("last_health_at")),
                "health_latency_ms": health.get("latency_ms"),
                "health_message": health.get("message"),
                "last_invocation_at": _iso(activity.get("last_invocation_at")),
                "invocation_count": int(activity.get("invocation_count") or 0),
                "total_tokens": int(activity.get("total_tokens") or 0),
                "avg_wall_clock_ms": float(activity.get("avg_wall_clock_ms") or 0),
            }
        )
    configured_keys = {item["connector_key"] for item in items}
    for key, health in sorted(health_by_key.items()):
        if key in configured_keys or key == "postgresql-local":
            continue
        items.append(
            {
                "connector_key": key,
                "connector_type": health.get("connector_type"),
                "display_name": health.get("display_name"),
                "enabled": bool(health.get("enabled", True)),
                "configured": True,
                "status": health.get("status") or "configured",
                "last_heartbeat_at": _iso(health.get("last_health_at")),
                "health_latency_ms": health.get("latency_ms"),
                "health_message": health.get("message"),
            }
        )
    now = datetime.now(timezone.utc).isoformat()
    postgres_health = health_by_key.get("postgresql-local", {})
    items.append(
        {
            "connector_key": "postgresql-local",
            "connector_type": "database",
            "display_name": "PostgreSQL Local",
            "enabled": True,
            "configured": True,
            "status": postgres_health.get("status") or "connected",
            "last_heartbeat_at": _iso(postgres_health.get("last_health_at")) or now,
            "health_latency_ms": postgres_health.get("latency_ms"),
            "health_message": postgres_health.get("message"),
        }
    )
    return items


def fetch_agents(project_path: str | None, limit: int = 100) -> list[dict[str, Any]]:
    where, params = _project_filter(project_path)
    rows = _query_all(
        f"""
        SELECT ai.id, ai.agent_role, ai.runner, ai.provider, ai.model, ai.outcome,
               ai.input_tokens, ai.output_tokens, ai.total_tokens, ai.wall_clock_ms,
               ai.started_at, ai.finished_at, p.project_path, p.project_name
        FROM project_registry.agent_invocations ai
        LEFT JOIN project_registry.projects p ON p.id = ai.project_id
        {where}
        ORDER BY ai.started_at DESC NULLS LAST
        LIMIT %s
        """,
        (*params, max(1, min(int(limit), 500))),
    )
    return [
        {
            **item,
            "id": str(item["id"]),
            "name": item.get("agent_role") or item.get("runner") or "worker",
            "status": "failed" if item.get("outcome") in {"failure", "failed"} else "configured",
            "last_invocation_at": _iso(item.get("started_at") or item.get("finished_at")),
        }
        for item in rows
    ]


def fetch_operational_summary() -> dict[str, int]:
    projects = _query_one(
        """
        SELECT COUNT(*) FILTER (WHERE status = 'active')::int AS projects_active
        FROM project_registry.projects
        """
    ) or {}
    tasks = _query_one(
        """
        SELECT COUNT(*)::int AS tasks_total,
               COALESCE(SUM(tokens_estimated), 0)::bigint AS tokens_total
        FROM project_registry.project_logs
        """
    ) or {}
    return {
        "projects_active": int(projects.get("projects_active") or 0),
        "tasks_total": int(tasks.get("tasks_total") or 0),
        "tokens_total": int(tasks.get("tokens_total") or 0),
    }


def build_backend(project_path: str | None = None) -> dict[str, Any]:
    selected_path = project_path or PROJECT_PATH
    graph = fetch_graph_state(selected_path)
    connectors = fetch_connectors()
    agents = fetch_agents(selected_path)
    operations = fetch_operational_summary()
    return {
        "connectors": connectors,
        "agents": agents,
        "graph": graph,
        "summary": {
            **operations,
            "project_path": selected_path,
            "graph_files": int(graph.get("files_indexed") or 0),
            "graph_nodes": int(graph.get("nodes_total") or 0),
            "graph_edges": int(graph.get("edges_total") or 0),
            "federated_entities": int(graph.get("entity_count") or 0),
            "federated_relations": int(graph.get("relation_count") or 0),
            "tokens_indexed_estimate": int(graph.get("tokens_indexed_estimate") or 0),
            "tokens_saved_estimate": int(graph.get("tokens_context_saved_estimate") or 0),
            "agent_invocations": len(agents),
            "connectors": len(connectors),
        },
    }
