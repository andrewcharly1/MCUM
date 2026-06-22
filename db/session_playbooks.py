"""
Operational session playbooks for high-leverage reuse across sessions.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from ..memory_freshness import apply_memory_freshness, build_source_snapshots, summarize_freshness_warnings
from ..memory_governor import apply_memory_governor
from . import pgvector_util
from .connection import get_db, get_cursor
from .embedder import EMBEDDING_DIM, cosine_similarity, embed


def _playbook_embedding_is_vector() -> bool:
    return pgvector_util.column_is_vector("core_brain", "session_playbooks")


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


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_pattern_ids(pattern_ids: list[str] | None) -> list[str]:
    """Normaliza pattern_ids: dedupe, preserva orden, descarta vacios/no-strings."""
    if not pattern_ids:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in pattern_ids:
        if raw is None:
            continue
        cleaned = str(raw).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _merge_unique_ordered(*lists: list[str]) -> list[str]:
    """Une varias listas preservando orden y eliminando duplicados."""
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst or []:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def _apply_requested_pattern_alignment(
    rows: list[dict[str, Any]], active_pattern_ids: list[str] | None
) -> list[dict[str, Any]]:
    requested = set(_normalize_pattern_ids(active_pattern_ids))
    if not requested:
        return rows
    aligned: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        linked = set(_normalize_pattern_ids(normalized.get("pattern_ids")))
        normalized["pattern_alignment_score"] = (
            round(len(linked & requested) / len(linked), 4) if linked else None
        )
        aligned.append(normalized)
    return aligned


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
    normalized["pattern_ids"] = _normalize_pattern_ids(normalized.get("pattern_ids"))
    return normalized


def _keyword_overlap_score(query_text: str, candidate_text: str) -> float:
    query_terms = {term for term in query_text.lower().split() if len(term) > 3}
    candidate_terms = {term for term in candidate_text.lower().split() if len(term) > 3}
    if not query_terms or not candidate_terms:
        return 0.0
    overlap = query_terms & candidate_terms
    return len(overlap) / len(query_terms)


def _playbook_compactness_metrics(row: dict[str, Any]) -> dict[str, float | bool | int]:
    output_summary = _clean_text(row.get("output_summary"))
    validation_summary = _clean_text(row.get("validation_summary"))
    objective = _clean_text(row.get("objective"))
    reusable_when = _clean_text(row.get("reusable_when"))
    commands = list(row.get("commands") or [])
    files_touched = list(row.get("files_touched") or [])

    output_chars = len(output_summary)
    validation_chars = len(validation_summary)
    coverage_parts = sum(1 for value in (objective, output_summary, validation_summary, reusable_when) if value)
    coverage_score = coverage_parts / 4

    if 48 <= output_chars <= 320:
        output_fit = 1.0
    elif 24 <= output_chars <= 420:
        output_fit = 0.82
    elif 1 <= output_chars <= 600:
        output_fit = 0.45
    else:
        output_fit = 0.0

    if 20 <= validation_chars <= 220:
        validation_fit = 1.0
    elif 1 <= validation_chars <= 320:
        validation_fit = 0.65
    else:
        validation_fit = 0.0

    if commands or files_touched:
        if len(commands) <= 3 and len(files_touched) <= 4:
            execution_fit = 1.0
        elif len(commands) <= 5 and len(files_touched) <= 6:
            execution_fit = 0.72
        else:
            execution_fit = 0.38
    else:
        execution_fit = 0.18

    bloat_penalty = 0.0
    if output_chars > 420:
        bloat_penalty += min(0.24, (output_chars - 420) / 1800)
    if validation_chars > 240:
        bloat_penalty += min(0.12, (validation_chars - 240) / 1200)

    compactness_score = max(
        0.0,
        min(
            1.0,
            (
                (coverage_score * 0.30)
                + (output_fit * 0.30)
                + (validation_fit * 0.18)
                + (execution_fit * 0.16)
                + (0.06 if reusable_when else 0.0)
            )
            - bloat_penalty,
        ),
    )
    return {
        "compactness_score": round(compactness_score, 4),
        "coverage_score": round(coverage_score, 4),
        "output_fit": round(output_fit, 4),
        "validation_fit": round(validation_fit, 4),
        "execution_fit": round(execution_fit, 4),
        "bloat_penalty": round(bloat_penalty, 4),
        "has_validation_summary": bool(validation_summary),
        "has_reusable_when": bool(reusable_when),
        "output_chars": output_chars,
        "validation_chars": validation_chars,
    }


def _score_playbooks(
    query_text: str,
    rows: list[dict],
    min_similarity: float,
    *,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    if query_embedding is None:
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

        # pgvector mode: similarity was computed in SQL (sp.embedding <=> query).
        # JSONB mode: keyword baseline, upgraded to Python cosine when both
        # the query and the candidate carry a usable embedding.
        sql_similarity = row.get("_sql_similarity")
        if sql_similarity is not None:
            similarity = float(sql_similarity)
        else:
            similarity = _keyword_overlap_score(query_text, candidate_text)
            candidate_embedding = _validate_embedding(normalized.get("embedding"))
            if query_embedding is not None and candidate_embedding is not None:
                similarity = cosine_similarity(query_embedding, candidate_embedding)

        if similarity < min_similarity:
            continue

        confidence = float(normalized.get("confidence_score") or 0.5)
        compactness_profile = _playbook_compactness_metrics(normalized)
        compactness = float(compactness_profile.get("compactness_score") or 0.0)
        normalized["_similarity"] = round(similarity, 4)
        normalized["_compactness_score"] = round(compactness, 4)
        normalized["_compactness_profile"] = compactness_profile
        normalized["_combined_score"] = round(
            (similarity * 0.70) + (confidence * 0.22) + (compactness * 0.08),
            4,
        )
        scored.append(normalized)

    scored.sort(
        key=lambda item: (
            item["_combined_score"],
            float(item.get("pattern_alignment_score") or 0.0),
            item.get("_compactness_score", 0.0),
            item.get("reuse_count", 0),
        ),
        reverse=True,
    )
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
    project_path: str | None = None,
    pattern_ids: list[str] | None = None,
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
    source_snapshots = build_source_snapshots(files_touched, project_path=project_path)
    stored_artifacts = list(artifacts or [])
    stored_artifacts.extend(
        snapshot for snapshot in source_snapshots if snapshot not in stored_artifacts
    )
    commands_json = json.dumps(commands) if commands is not None else None
    files_json = json.dumps(files_touched) if files_touched is not None else None
    artifacts_json = json.dumps(stored_artifacts) if stored_artifacts else None
    issues_json = json.dumps(issues_avoided) if issues_avoided is not None else None
    normalized_pattern_ids = _normalize_pattern_ids(pattern_ids)

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, pattern_ids
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
                existing_pattern_ids = [
                    str(item) for item in (existing.get("pattern_ids") or [])
                ]
                merged_pattern_ids = _merge_unique_ordered(
                    existing_pattern_ids, normalized_pattern_ids
                )
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
                        pattern_ids = %s,
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
                        merged_pattern_ids,
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
                    pattern_ids, embedding
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
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
                    json.dumps(stored_artifacts),
                    json.dumps(issues_avoided or []),
                    reusable_when,
                    outcome,
                    confidence_score,
                    source_session_id,
                    source_task_log_id,
                    normalized_pattern_ids,
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
    policy: dict[str, Any] | None = None,
    active_pattern_ids: list[str] | None = None,
) -> dict:
    warnings: list[str] = []
    search_scope = "global"

    # Embed the query once. In pgvector mode the candidate set is selected by
    # vector proximity (HNSW-backed `<=>`) instead of raw popularity, so the
    # most semantically relevant playbooks reach the re-ranker even if they are
    # rarely reused. In JSONB mode we keep the popularity-ordered candidate set.
    query_embedding = _safe_embed(query_text)
    use_vector = bool(query_embedding) and _playbook_embedding_is_vector()
    query_literal = pgvector_util.to_vector_literal(query_embedding) if use_vector else None

    def _fetch(project_filter: str | None, exclude_project: str | None = None) -> list[dict]:
        conditions = ["sp.outcome IN ('success', 'partial')"]
        params: list[Any] = []
        if use_vector:
            sim_select = "(1 - (sp.embedding <=> %s::vector)) AS _sql_similarity"
            embedding_select = "NULL::text AS embedding"
            params.append(query_literal)
        else:
            sim_select = "NULL::float AS _sql_similarity"
            embedding_select = "sp.embedding"
        if skill_name:
            conditions.append("sp.skill_name = %s")
            params.append(skill_name)
        if project_filter:
            conditions.append("sp.project_id = %s")
            params.append(project_filter)
        if exclude_project:
            conditions.append("sp.project_id <> %s")
            params.append(exclude_project)

        where_clause = " AND ".join(conditions)
        if use_vector:
            order_clause = "ORDER BY sp.embedding <=> %s::vector"
            order_params: list[Any] = [query_literal]
        else:
            order_clause = "ORDER BY sp.reuse_count DESC, sp.created_at DESC"
            order_params = []
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    f"""
                    SELECT
                        sp.id, sp.project_id, sp.skill_name, sp.title, sp.task_description, sp.objective,
                        sp.output_summary, sp.validation_summary, sp.commands, sp.files_touched,
                        sp.artifacts, sp.issues_avoided, sp.reusable_when, sp.outcome,
                        sp.confidence_score, sp.reuse_count, sp.last_reused_at,
                        sp.created_at, sp.updated_at, sp.pattern_ids, {embedding_select},
                        {sim_select},
                        CASE
                            WHEN cardinality(COALESCE(sp.pattern_ids, ARRAY[]::uuid[])) = 0 THEN NULL
                            ELSE (
                                SELECT COUNT(*)::real
                                       / cardinality(COALESCE(sp.pattern_ids, ARRAY[]::uuid[]))
                                FROM core_brain.patterns p
                                WHERE p.id = ANY(sp.pattern_ids)
                                  AND p.status = 'active'
                                  AND (
                                      p.scope_skill_name IS NULL
                                      OR p.scope_skill_name = sp.skill_name
                                  )
                            )
                        END AS pattern_alignment_score
                    FROM core_brain.session_playbooks sp
                    WHERE {where_clause}
                    {order_clause}
                    LIMIT 50
                    """,
                    params + order_params,
                )
                return _apply_requested_pattern_alignment(
                    [dict(row) for row in cur.fetchall()],
                    active_pattern_ids,
                )

    scored: list[dict] = []
    if project_id:
        scored = apply_memory_freshness(
            _score_playbooks(
                query_text, _fetch(project_id), min_similarity=min_similarity,
                query_embedding=query_embedding,
            ),
            kind="playbook",
            score_key="_combined_score",
        )
        if scored:
            search_scope = "same_project"

    if not scored and (not project_id or allow_cross_project):
        fallback_rows = _fetch(None, exclude_project=project_id) if project_id else _fetch(None)
        scored = apply_memory_freshness(
            _score_playbooks(
                query_text, fallback_rows, min_similarity=min_similarity,
                query_embedding=query_embedding,
            ),
            kind="playbook",
            score_key="_combined_score",
        )
        if scored and project_id:
            search_scope = "cross_project_fallback"
            warnings.append("Session playbooks required cross-project fallback.")

    scored, memory_governance = apply_memory_governor(
        scored,
        item_kind="playbook",
        policy=policy,
        active_project_id=project_id,
    )
    warnings.extend(memory_governance.get("warnings", []))

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
    warnings.extend(summarize_freshness_warnings(playbooks, label="Session playbooks"))

    return {
        "playbooks": playbooks,
        "search_scope": search_scope,
        "warnings": warnings,
        "memory_governance": memory_governance,
    }
