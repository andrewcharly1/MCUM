"""
Operational session playbooks for high-leverage reuse across sessions.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from .connection import get_db, get_cursor
from .embedder import EMBEDDING_DIM, cosine_similarity, embed


def _normalize_json_field(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _validate_embedding(embedding: Any) -> list[float] | None:
    if embedding is None:
        return None
    try:
        values = [float(item) for item in embedding]
    except (TypeError, ValueError):
        return None
    if len(values) != EMBEDDING_DIM:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def _safe_embed(text: str) -> list[float] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _validate_embedding(embed(text))
    except Exception:
        return None


def _playbook_text(
    *,
    title: str,
    task_description: str,
    objective: str | None,
    output_summary: str | None,
    validation_summary: str | None,
    commands: list[str] | None,
    files_touched: list[str] | None,
    reusable_when: str | None,
    issues_avoided: list[str] | None,
) -> str:
    parts = [
        title,
        task_description,
        objective or "",
        output_summary or "",
        validation_summary or "",
        reusable_when or "",
        " | ".join(commands or []),
        " | ".join(files_touched or []),
        " | ".join(issues_avoided or []),
    ]
    return " | ".join(part.strip() for part in parts if part and part.strip())


def _normalize_playbook_row(row: dict) -> dict:
    normalized = dict(row)
    for field in ("commands", "files_touched", "artifacts", "issues_avoided", "embedding"):
        normalized[field] = _normalize_json_field(normalized.get(field), [])
    return normalized


def _keyword_overlap_score(query_text: str, candidate_text: str) -> float:
    query_terms = {term for term in query_text.lower().split() if len(term) > 3}
    candidate_terms = {term for term in candidate_text.lower().split() if len(term) > 3}
    if not query_terms or not candidate_terms:
        return 0.0
    overlap = query_terms & candidate_terms
    return len(overlap) / len(query_terms)


def _score_playbooks(query_text: str, rows: list[dict], min_similarity: float) -> list[dict]:
    query_embedding = _safe_embed(query_text)
    scored: list[dict] = []

    for row in rows:
        normalized = _normalize_playbook_row(row)
        candidate_text = _playbook_text(
            title=normalized.get("title", ""),
            task_description=normalized.get("task_description", ""),
            objective=normalized.get("objective"),
            output_summary=normalized.get("output_summary"),
            validation_summary=normalized.get("validation_summary"),
            commands=normalized.get("commands"),
            files_touched=normalized.get("files_touched"),
            reusable_when=normalized.get("reusable_when"),
            issues_avoided=normalized.get("issues_avoided"),
        )

        similarity = _keyword_overlap_score(query_text, candidate_text)
        candidate_embedding = _validate_embedding(normalized.get("embedding"))
        if query_embedding is not None and candidate_embedding is not None:
            similarity = cosine_similarity(query_embedding, candidate_embedding)

        if similarity < min_similarity:
            continue

        confidence = float(normalized.get("confidence_score") or 0.5)
        normalized["_similarity"] = round(similarity, 4)
        normalized["_combined_score"] = round((similarity * 0.75) + (confidence * 0.25), 4)
        scored.append(normalized)

    scored.sort(key=lambda item: (item["_combined_score"], item.get("reuse_count", 0)), reverse=True)
    return scored


def save_session_playbook(
    *,
    project_id: str,
    skill_name: str,
    task_description: str,
    title: str,
    objective: str | None = None,
    output_summary: str | None = None,
    validation_summary: str | None = None,
    commands: list[str] | None = None,
    files_touched: list[str] | None = None,
    artifacts: list[dict] | None = None,
    issues_avoided: list[str] | None = None,
    reusable_when: str | None = None,
    outcome: str = "success",
    confidence_score: float | None = None,
    source_session_id: str | None = None,
    source_task_log_id: str | None = None,
) -> str:
    playbook_text = _playbook_text(
        title=title,
        task_description=task_description,
        objective=objective,
        output_summary=output_summary,
        validation_summary=validation_summary,
        commands=commands,
        files_touched=files_touched,
        reusable_when=reusable_when,
        issues_avoided=issues_avoided,
    )
    embedding = _safe_embed(playbook_text)
    embedding_json = json.dumps(embedding) if embedding is not None else None
    commands_json = json.dumps(commands) if commands is not None else None
    files_json = json.dumps(files_touched) if files_touched is not None else None
    artifacts_json = json.dumps(artifacts) if artifacts is not None else None
    issues_json = json.dumps(issues_avoided) if issues_avoided is not None else None

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id
                FROM core_brain.session_playbooks
                WHERE project_id = %s
                  AND skill_name = %s
                  AND task_description = %s
                  AND COALESCE(output_summary, '') = COALESCE(%s, '')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, skill_name, task_description, output_summary),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """
                    UPDATE core_brain.session_playbooks
                    SET title = %s,
                        objective = COALESCE(%s, objective),
                        validation_summary = COALESCE(%s, validation_summary),
                        commands = COALESCE(%s, commands),
                        files_touched = COALESCE(%s, files_touched),
                        artifacts = COALESCE(%s, artifacts),
                        issues_avoided = COALESCE(%s, issues_avoided),
                        reusable_when = COALESCE(%s, reusable_when),
                        outcome = %s,
                        confidence_score = COALESCE(%s, confidence_score),
                        source_session_id = COALESCE(%s, source_session_id),
                        source_task_log_id = COALESCE(%s, source_task_log_id),
                        embedding = COALESCE(%s, embedding),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        title,
                        objective,
                        validation_summary,
                        commands_json,
                        files_json,
                        artifacts_json,
                        issues_json,
                        reusable_when,
                        outcome,
                        confidence_score,
                        source_session_id,
                        source_task_log_id,
                        embedding_json,
                        existing["id"],
                    ),
                )
                return str(existing["id"])

            playbook_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO core_brain.session_playbooks (
                    id, project_id, skill_name, title, task_description, objective,
                    output_summary, validation_summary, commands, files_touched,
                    artifacts, issues_avoided, reusable_when, outcome,
                    confidence_score, source_session_id, source_task_log_id,
                    embedding
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s
                )
                RETURNING id
                """,
                (
                    playbook_id,
                    project_id,
                    skill_name,
                    title,
                    task_description,
                    objective,
                    output_summary,
                    validation_summary,
                    json.dumps(commands or []),
                    json.dumps(files_touched or []),
                    json.dumps(artifacts or []),
                    json.dumps(issues_avoided or []),
                    reusable_when,
                    outcome,
                    confidence_score,
                    source_session_id,
                    source_task_log_id,
                    embedding_json,
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else playbook_id


def retrieve_session_playbooks(
    query_text: str,
    *,
    skill_name: str | None = None,
    project_id: str | None = None,
    limit: int = 3,
    min_similarity: float = 0.28,
    allow_cross_project: bool = True,
) -> dict:
    warnings: list[str] = []
    search_scope = "global"

    def _fetch(project_filter: str | None, exclude_project: str | None = None) -> list[dict]:
        conditions = ["outcome IN ('success', 'partial')"]
        params: list[Any] = []
        if skill_name:
            conditions.append("skill_name = %s")
            params.append(skill_name)
        if project_filter:
            conditions.append("project_id = %s")
            params.append(project_filter)
        if exclude_project:
            conditions.append("project_id <> %s")
            params.append(exclude_project)

        where_clause = " AND ".join(conditions)
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    f"""
                    SELECT
                        id, project_id, skill_name, title, task_description, objective,
                        output_summary, validation_summary, commands, files_touched,
                        artifacts, issues_avoided, reusable_when, outcome,
                        confidence_score, reuse_count, last_reused_at,
                        created_at, updated_at, embedding
                    FROM core_brain.session_playbooks
                    WHERE {where_clause}
                    ORDER BY reuse_count DESC, created_at DESC
                    LIMIT 50
                    """,
                    params,
                )
                return [dict(row) for row in cur.fetchall()]

    scored: list[dict] = []
    if project_id:
        scored = _score_playbooks(query_text, _fetch(project_id), min_similarity=min_similarity)
        if scored:
            search_scope = "same_project"

    if not scored and (not project_id or allow_cross_project):
        fallback_rows = _fetch(None, exclude_project=project_id) if project_id else _fetch(None)
        scored = _score_playbooks(query_text, fallback_rows, min_similarity=min_similarity)
        if scored and project_id:
            search_scope = "cross_project_fallback"
            warnings.append("Session playbooks required cross-project fallback.")

    playbooks = scored[:limit]
    if playbooks:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE core_brain.session_playbooks
                    SET reuse_count = reuse_count + 1,
                        last_reused_at = NOW(),
                        updated_at = NOW()
                    WHERE id = ANY(%s::uuid[])
                    """,
                    ([item["id"] for item in playbooks],),
                )

    return {
        "playbooks": playbooks,
        "search_scope": search_scope,
        "warnings": warnings,
    }
