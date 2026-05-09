"""
Project registry and immutable log helpers for MCUM.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .connection import get_db, get_cursor


def normalize_project_path(project_path: str) -> str:
    return str(Path(project_path)).replace("\\", "/")


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: (str(value) if hasattr(value, "hex") else value) for key, value in row.items()}


def estimate_tokens(payload: Any) -> int:
    """Cheap token estimate for operational telemetry and retrieval budgets."""
    if payload is None:
        return 0

    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(payload)

    text = text.strip()
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def get_or_create_project(
    project_path: str,
    project_name: str | None = None,
    description: str | None = None,
    tech_stack: dict | None = None,
    client_or_context: str | None = None,
) -> dict:
    normalized_path = normalize_project_path(project_path)
    if not project_name:
        project_name = Path(project_path).name

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM project_registry.projects WHERE project_path = %s",
                (normalized_path,),
            )
            existing = _row_to_dict(cur.fetchone())
            if existing:
                cur.execute(
                    """
                    UPDATE project_registry.projects
                    SET updated_at = NOW(), last_activity_at = NOW()
                    WHERE id = %s
                    """,
                    (existing["id"],),
                )
                return existing

            project_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO project_registry.projects (
                    id, project_name, project_path, description,
                    tech_stack, client_or_context, status, phase
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', 'development')
                RETURNING *
                """,
                (
                    project_id,
                    project_name,
                    normalized_path,
                    description
                    or f"Project auto-registered by MCUM on {datetime.now().strftime('%Y-%m-%d')}",
                    json.dumps(tech_stack or {}),
                    client_or_context or "personal",
                ),
            )
            return _row_to_dict(cur.fetchone()) or {"id": project_id, "project_path": normalized_path}


def update_project_info(project_id: str, **kwargs: Any) -> bool:
    allowed_fields = {
        "project_name",
        "description",
        "tech_stack",
        "status",
        "phase",
        "client_or_context",
        "primary_language",
        "frameworks",
    }
    updates = {key: value for key, value in kwargs.items() if key in allowed_fields}
    if not updates:
        return False

    if "tech_stack" in updates and isinstance(updates["tech_stack"], dict):
        updates["tech_stack"] = json.dumps(updates["tech_stack"])

    set_clause = ", ".join(f"{key} = %s" for key in updates)
    values = list(updates.values()) + [project_id]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                UPDATE project_registry.projects
                SET {set_clause}, updated_at = NOW(), last_activity_at = NOW()
                WHERE id = %s
                """,
                values,
            )
            return cur.rowcount > 0


def get_project_by_path(project_path: str) -> dict | None:
    normalized_path = normalize_project_path(project_path)
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM project_registry.projects WHERE project_path = %s",
                (normalized_path,),
            )
            return _row_to_dict(cur.fetchone())


def list_projects(status: str = "active") -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if status == "all":
                cur.execute(
                    "SELECT * FROM project_registry.v_project_summary ORDER BY last_activity_at DESC"
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM project_registry.v_project_summary
                    WHERE status = %s
                    ORDER BY last_activity_at DESC
                    """,
                    (status,),
                )
            return [dict(row) for row in cur.fetchall()]


def log_entry(
    project_id: str,
    log_type: str,
    title: str,
    description: str | None = None,
    skill_used: str | None = None,
    skills_orchestrated: list[str] | None = None,
    outcome: str | None = None,
    outcome_details: str | None = None,
    artifacts_generated: list[dict] | None = None,
    experience_ids: list[str] | None = None,
    pattern_ids_used: list[str] | None = None,
    retrieval_run_id: str | None = None,
    session_duration_sec: int | None = None,
    confidence_score: float | None = None,
    tokens_estimated: int | None = None,
    context_tokens_in: int | None = None,
    context_tokens_out: int | None = None,
    task_wall_clock_ms: int | None = None,
    retrieval_latency_ms: int | None = None,
    git_commit: str | None = None,
    log_metadata: dict | None = None,
) -> str:
    log_id = str(uuid.uuid4())
    if tokens_estimated is None:
        combined_tokens = (context_tokens_in or 0) + (context_tokens_out or 0)
        tokens_estimated = combined_tokens or None

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.project_logs (
                    id, project_id, log_type, title, description,
                    skill_used, skills_orchestrated, outcome, outcome_details,
                    artifacts_generated, experience_ids, pattern_ids_used,
                    retrieval_run_id, session_duration_sec, confidence_score,
                    tokens_estimated, context_tokens_in, context_tokens_out,
                    task_wall_clock_ms, retrieval_latency_ms,
                    git_commit, log_metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s
                )
                RETURNING id
                """,
                (
                    log_id,
                    project_id,
                    log_type,
                    title,
                    description,
                    skill_used,
                    skills_orchestrated or [],
                    outcome,
                    outcome_details,
                    json.dumps(artifacts_generated or []),
                    experience_ids or [],
                    pattern_ids_used or [],
                    retrieval_run_id,
                    session_duration_sec,
                    confidence_score,
                    tokens_estimated,
                    context_tokens_in,
                    context_tokens_out,
                    task_wall_clock_ms,
                    retrieval_latency_ms,
                    git_commit,
                    json.dumps(log_metadata or {}),
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else log_id


def get_project_logs(
    project_id: str,
    log_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if log_type:
                cur.execute(
                    """
                    SELECT * FROM project_registry.project_logs
                    WHERE project_id = %s AND log_type = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (project_id, log_type, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM project_registry.project_logs
                    WHERE project_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (project_id, limit, offset),
                )
            return [dict(row) for row in cur.fetchall()]


def get_recent_logs(project_id: str, limit: int = 10) -> list[dict]:
    return get_project_logs(project_id, limit=limit)


def log_session_start(
    project_path: str,
    skill_used: str = "mcum-orchestrator",
    task_description: str | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    project = get_or_create_project(project_path)
    metadata = {
        "session_start": datetime.now().isoformat(),
        "project_path": normalize_project_path(project_path),
    }
    if task_description:
        metadata["task_description"] = task_description
    if extra_metadata:
        metadata.update(extra_metadata)

    log_id = log_entry(
        project_id=project["id"],
        log_type="session_start",
        title=f"Session started - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        skill_used=skill_used,
        log_metadata=metadata,
    )
    return {"project": project, "log_id": log_id}


def log_session_end(
    project_id: str,
    session_duration_sec: int,
    tasks_completed: int = 0,
    skill_used: str = "mcum-orchestrator",
    outcome: str | None = None,
    context_tokens_in: int | None = None,
    context_tokens_out: int | None = None,
    task_wall_clock_ms: int | None = None,
    retrieval_latency_ms: int | None = None,
    extra_metadata: dict | None = None,
) -> str:
    metadata = {
        "tasks_completed": tasks_completed,
        "duration_minutes": round(session_duration_sec / 60, 1),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    return log_entry(
        project_id=project_id,
        log_type="session_end",
        title=f"Session finished - {tasks_completed} task(s)",
        skill_used=skill_used,
        session_duration_sec=session_duration_sec,
        outcome=outcome or ("success" if tasks_completed > 0 else "partial"),
        context_tokens_in=context_tokens_in,
        context_tokens_out=context_tokens_out,
        task_wall_clock_ms=task_wall_clock_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        log_metadata=metadata,
    )


if __name__ == "__main__":
    test_path = "C:/Users/carlo/OneDrive/Escritorio/CERTIFICACION LABORAL"
    project = get_or_create_project(
        project_path=test_path,
        project_name="CERTIFICACION LABORAL",
        description="MCUM project registry smoke test",
        tech_stack={"language": "Python", "db": "PostgreSQL"},
        client_or_context="Carlos",
    )
    session = log_session_start(test_path, task_description="Registry smoke test")
    task_log = log_entry(
        project_id=project["id"],
        log_type="task",
        title="MCUM registry smoke test",
        description="Registry helper functions executed successfully.",
        skill_used="mcum-orchestrator",
        outcome="success",
        confidence_score=0.95,
    )
    end_log = log_session_end(project["id"], session_duration_sec=1, tasks_completed=1)
    print(project["project_name"])
    print(session["log_id"])
    print(task_log)
    print(end_log)
