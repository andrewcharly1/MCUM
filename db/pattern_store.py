"""Persistence helpers for governed Pattern Intelligence."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .connection import get_cursor, get_db


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def start_discovery_run(
    *,
    scope_type: str,
    project_id: str | None,
    mode: str,
    policy_version: str,
    algorithm_version: str,
    embedding_model: str,
) -> str:
    run_id = str(uuid.uuid4())
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.pattern_discovery_runs (
                    id, scope_type, project_id, status, mode, policy_version,
                    algorithm_version, embedding_model
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s)
                """,
                (
                    run_id,
                    scope_type,
                    project_id,
                    mode,
                    policy_version,
                    algorithm_version,
                    embedding_model,
                ),
            )
    return run_id


def finish_discovery_run(
    discovery_run_id: str,
    *,
    status: str,
    metrics: dict[str, Any] | None = None,
    findings: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> bool:
    metrics = dict(metrics or {})
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.pattern_discovery_runs
                SET status = %s,
                    experiences_scanned = %s,
                    embeddings_generated = %s,
                    embeddings_reused = %s,
                    groups_analyzed = %s,
                    candidates_observed = %s,
                    candidates_review_ready = %s,
                    findings = %s,
                    error_message = %s,
                    finished_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    int(metrics.get("experiences_scanned") or 0),
                    int(metrics.get("embeddings_generated") or 0),
                    int(metrics.get("embeddings_reused") or 0),
                    int(metrics.get("groups_analyzed") or 0),
                    int(metrics.get("candidates_observed") or 0),
                    int(metrics.get("candidates_review_ready") or 0),
                    _json(findings or {}),
                    error_message,
                    discovery_run_id,
                ),
            )
            return cur.rowcount > 0


def fetch_eligible_experiences(
    *,
    included_categories: list[str],
    excluded_categories: list[str],
    min_confidence: float,
    exclude_synthetic: bool,
    project_id: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    conditions = [
        "superseded_by IS NULL",
        "current_confidence >= %s",
        "category::text = ANY(%s)",
        "NOT (category::text = ANY(%s))",
    ]
    params: list[Any] = [min_confidence, included_categories, excluded_categories or [""]]
    if exclude_synthetic:
        conditions.append("NOT is_synthetic")
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    params.append(max(1, min(int(limit), 5000)))

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT
                    id, category::text AS category, title, content, applicability,
                    not_applicable_cases, conditions, current_confidence,
                    unique_context_count, contradiction_penalty, conflict_refs,
                    evidence_refs, project_id, skill_name, task_description,
                    created_at, last_validated_at
                FROM core_brain.experiences
                WHERE {' AND '.join(conditions)}
                ORDER BY category, skill_name, created_at, id
                LIMIT %s
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def get_cached_experience_embeddings(
    *,
    experience_ids: list[str],
    model_name: str,
) -> dict[str, dict[str, Any]]:
    if not experience_ids:
        return {}
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT experience_id, source_hash, embedding::text AS embedding
                FROM core_brain.pattern_experience_embeddings
                WHERE model_name = %s
                  AND experience_id = ANY(%s::uuid[])
                """,
                (model_name, experience_ids),
            )
            return {str(row["experience_id"]): dict(row) for row in cur.fetchall()}


def upsert_experience_embeddings(
    *,
    rows: list[dict[str, Any]],
    model_name: str,
) -> int:
    if not rows:
        return 0
    values = [
        (
            row["experience_id"],
            model_name,
            row["source_hash"],
            _json(row["embedding"]),
        )
        for row in rows
    ]
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.executemany(
                """
                INSERT INTO core_brain.pattern_experience_embeddings (
                    experience_id, model_name, source_hash, embedding
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (experience_id, model_name) DO UPDATE
                SET source_hash = EXCLUDED.source_hash,
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
                """,
                values,
            )
    return len(values)


def upsert_pattern_candidate(
    *,
    candidate: dict[str, Any],
    evidence: list[dict[str, Any]],
    centroid_embedding: list[float],
    discovery_run_id: str,
) -> str:
    candidate_id = str(uuid.uuid4())
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.pattern_candidates (
                    id, candidate_key, category, skill_name, scope_type,
                    scope_project_id, label, summary, status, support_count,
                    distinct_project_count, context_diversity, cohesion_score,
                    contradiction_count, avg_confidence, quality_score,
                    quality_ready, seed_experience_id, discovery_run_id,
                    embedding_model, algorithm_version, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (candidate_key) DO UPDATE
                SET category = EXCLUDED.category,
                    skill_name = EXCLUDED.skill_name,
                    scope_type = EXCLUDED.scope_type,
                    scope_project_id = EXCLUDED.scope_project_id,
                    label = EXCLUDED.label,
                    summary = EXCLUDED.summary,
                    status = CASE
                        WHEN core_brain.pattern_candidates.status IN ('accepted','rejected')
                            THEN core_brain.pattern_candidates.status
                        ELSE EXCLUDED.status
                    END,
                    support_count = EXCLUDED.support_count,
                    distinct_project_count = EXCLUDED.distinct_project_count,
                    context_diversity = EXCLUDED.context_diversity,
                    cohesion_score = EXCLUDED.cohesion_score,
                    contradiction_count = EXCLUDED.contradiction_count,
                    avg_confidence = EXCLUDED.avg_confidence,
                    quality_score = EXCLUDED.quality_score,
                    quality_ready = EXCLUDED.quality_ready,
                    discovery_run_id = EXCLUDED.discovery_run_id,
                    embedding_model = EXCLUDED.embedding_model,
                    algorithm_version = EXCLUDED.algorithm_version,
                    last_seen_at = NOW(),
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    candidate_id,
                    candidate["candidate_key"],
                    candidate["category"],
                    candidate["skill_name"],
                    candidate["scope_type"],
                    candidate.get("scope_project_id"),
                    candidate["label"],
                    candidate["summary"],
                    candidate["status"],
                    candidate["support_count"],
                    candidate["distinct_project_count"],
                    candidate["context_diversity"],
                    candidate["cohesion_score"],
                    candidate["contradiction_count"],
                    candidate["avg_confidence"],
                    candidate["quality_score"],
                    candidate["quality_ready"],
                    candidate["seed_experience_id"],
                    discovery_run_id,
                    candidate["embedding_model"],
                    candidate["algorithm_version"],
                    _json(candidate.get("metadata") or {}),
                ),
            )
            row = cur.fetchone()
            persisted_id = str(row["id"]) if row else candidate_id

            cur.execute(
                "DELETE FROM core_brain.pattern_candidate_evidence WHERE candidate_id = %s",
                (persisted_id,),
            )
            if evidence:
                cur.executemany(
                    """
                    INSERT INTO core_brain.pattern_candidate_evidence (
                        candidate_id, experience_id, evidence_role, similarity, weight, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (candidate_id, experience_id) DO UPDATE
                    SET evidence_role = EXCLUDED.evidence_role,
                        similarity = EXCLUDED.similarity,
                        weight = EXCLUDED.weight,
                        metadata = EXCLUDED.metadata,
                        added_at = NOW()
                    """,
                    [
                        (
                            persisted_id,
                            item["experience_id"],
                            item.get("evidence_role", "support"),
                            item.get("similarity"),
                            item.get("weight", 1.0),
                            _json(item.get("metadata") or {}),
                        )
                        for item in evidence
                    ],
                )

            cur.execute(
                """
                INSERT INTO core_brain.pattern_embeddings (
                    candidate_id, model_name, source_hash, embedding
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (candidate_id, model_name) WHERE candidate_id IS NOT NULL DO UPDATE
                SET source_hash = EXCLUDED.source_hash,
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
                """,
                (
                    persisted_id,
                    candidate["embedding_model"],
                    candidate["candidate_key"],
                    _json(centroid_embedding),
                ),
            )
    return persisted_id


def expire_unseen_candidates(*, ttl_days: int) -> int:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.pattern_candidates
                SET status = 'expired', updated_at = NOW()
                WHERE status IN ('shadow','review')
                  AND last_seen_at < NOW() - (%s || ' days')::interval
                """,
                (max(1, int(ttl_days)),),
            )
            return cur.rowcount


def materialize_candidate_to_draft(
    *,
    candidate_id: str,
    reviewed_by: str,
    review_notes: str | None = None,
) -> dict[str, Any]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT *
                FROM core_brain.pattern_candidates
                WHERE id = %s
                FOR UPDATE
                """,
                (candidate_id,),
            )
            candidate = cur.fetchone()
            if not candidate:
                raise ValueError(f"Pattern candidate not found: {candidate_id}")
            candidate = dict(candidate)
            if not bool(candidate.get("quality_ready")):
                raise ValueError("Pattern candidate does not satisfy the configured quality gates.")
            if candidate.get("materialized_pattern_id"):
                return {
                    "status": "already_materialized",
                    "candidate_id": candidate_id,
                    "pattern_id": str(candidate["materialized_pattern_id"]),
                }
            if str(candidate.get("status") or "") != "review":
                raise ValueError("Only review-ready pattern candidates may be materialized.")

            pattern_id = str(uuid.uuid4())
            pattern_key = f"candidate:{candidate['candidate_key']}"
            name = f"{candidate['label']} [{str(candidate['candidate_key'])[:8]}]"
            cur.execute(
                """
                INSERT INTO core_brain.patterns (
                    id, pattern_key, name, description, category, status,
                    promotion_criteria_met, scope_type, scope_project_id,
                    scope_skill_name, cohesion_score, experience_count, support_count,
                    context_diversity, contradiction_count, avg_score,
                    health_state, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, 'draft',
                    FALSE, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'observing', %s
                )
                RETURNING id
                """,
                (
                    pattern_id,
                    pattern_key,
                    name,
                    candidate["summary"],
                    candidate["category"],
                    candidate["scope_type"],
                    candidate.get("scope_project_id"),
                    candidate["skill_name"],
                    candidate["cohesion_score"],
                    int(candidate.get("support_count") or 0),
                    int(candidate.get("support_count") or 0),
                    int(candidate.get("context_diversity") or 0),
                    int(candidate.get("contradiction_count") or 0),
                    float(candidate.get("avg_confidence") or 0.0),
                    _json(
                        {
                            "source": "pattern_candidate",
                            "candidate_id": candidate_id,
                            "reviewed_by": reviewed_by,
                        }
                    ),
                ),
            )
            cur.execute(
                """
                INSERT INTO core_brain.pattern_evidence (
                    pattern_id, experience_id, evidence_role, source,
                    similarity, weight, metadata
                )
                SELECT %s, experience_id, evidence_role, 'candidate_review',
                       similarity, weight, metadata
                FROM core_brain.pattern_candidate_evidence
                WHERE candidate_id = %s
                ON CONFLICT (pattern_id, experience_id) DO NOTHING
                """,
                (pattern_id, candidate_id),
            )
            cur.execute(
                """
                INSERT INTO core_brain.pattern_embeddings (
                    pattern_id, model_name, source_hash, embedding
                )
                SELECT %s, model_name, source_hash, embedding
                FROM core_brain.pattern_embeddings
                WHERE candidate_id = %s
                ON CONFLICT (pattern_id, model_name) WHERE pattern_id IS NOT NULL DO NOTHING
                """,
                (pattern_id, candidate_id),
            )
            cur.execute(
                """
                UPDATE core_brain.pattern_candidates
                SET status = 'accepted',
                    reviewed_at = NOW(),
                    reviewed_by = %s,
                    review_notes = %s,
                    materialized_pattern_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (reviewed_by, review_notes, pattern_id, candidate_id),
            )
    return {"status": "draft_materialized", "candidate_id": candidate_id, "pattern_id": pattern_id}


def activate_pattern(
    *,
    pattern_id: str,
    reviewed_by: str,
    quality_gates: dict[str, Any],
    review_notes: str | None = None,
) -> dict[str, Any]:
    min_support = max(3, int(quality_gates.get("min_support", 3) or 3))
    min_context_diversity = max(
        2, int(quality_gates.get("min_context_diversity", 2) or 2)
    )
    min_cohesion = float(quality_gates.get("min_cohesion", 0.80) or 0.80)
    min_avg_confidence = float(quality_gates.get("min_avg_confidence", 0.75) or 0.75)
    max_open_conflicts = max(
        0, int(quality_gates.get("max_open_conflicts", 0) or 0)
    )

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT *
                FROM core_brain.patterns
                WHERE id = %s
                FOR UPDATE
                """,
                (pattern_id,),
            )
            pattern = cur.fetchone()
            if not pattern:
                raise ValueError(f"Pattern not found: {pattern_id}")
            pattern = dict(pattern)
            if str(pattern.get("status") or "") == "active":
                return {"status": "already_active", "pattern_id": pattern_id}
            if str(pattern.get("status") or "") != "draft":
                raise ValueError("Only draft patterns may be activated.")

            failures: list[str] = []
            if int(pattern.get("support_count") or 0) < min_support:
                failures.append("support_count")
            if int(pattern.get("context_diversity") or 0) < min_context_diversity:
                failures.append("context_diversity")
            if float(pattern.get("cohesion_score") or 0.0) < min_cohesion:
                failures.append("cohesion_score")
            if float(pattern.get("avg_score") or 0.0) < min_avg_confidence:
                failures.append("avg_score")
            if int(pattern.get("contradiction_count") or 0) > max_open_conflicts:
                failures.append("open_conflicts")
            if failures:
                raise ValueError(
                    "Pattern activation blocked by quality gates: " + ", ".join(failures)
                )

            metadata = pattern.get("metadata") if isinstance(pattern.get("metadata"), dict) else {}
            metadata = {
                **metadata,
                "activation_review": {
                    "reviewed_by": reviewed_by,
                    "review_notes": review_notes,
                },
            }
            cur.execute(
                """
                UPDATE core_brain.patterns
                SET status = 'active',
                    promotion_criteria_met = TRUE,
                    health_state = 'observing',
                    metadata = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (_json(metadata), pattern_id),
            )
    return {"status": "active", "pattern_id": pattern_id, "reviewed_by": reviewed_by}


def record_pattern_usage_events(
    *,
    pattern_ids: list[str],
    project_id: str | None,
    session_id: str | None,
    log_id: str | None,
    outcome: str,
    user_feedback: int | None,
    metadata: dict[str, Any] | None = None,
    min_usage_before_health_decision: int = 5,
    degraded_success_rate: float = 0.50,
) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(str(item) for item in pattern_ids if item))
    if not unique_ids:
        return {"status": "not_applicable", "events_recorded": 0}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            for pattern_id in unique_ids:
                cur.execute(
                    """
                    INSERT INTO core_brain.pattern_usage_events (
                        pattern_id, project_id, session_id, log_id, outcome, user_feedback, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (pattern_id, project_id, session_id, log_id, outcome, user_feedback, _json(metadata or {})),
                )
                cur.execute(
                    """
                    UPDATE core_brain.patterns
                    SET usage_count = usage_count + 1,
                        success_count = success_count + CASE WHEN %s = 'success' THEN 1 ELSE 0 END,
                        failure_count = failure_count + CASE WHEN %s = 'failure' THEN 1 ELSE 0 END,
                        utility_score = (
                            (success_count + CASE WHEN %s = 'success' THEN 1 ELSE 0 END)
                            + 0.5 * (
                                (usage_count + 1)
                                - (success_count + CASE WHEN %s = 'success' THEN 1 ELSE 0 END)
                                - (failure_count + CASE WHEN %s = 'failure' THEN 1 ELSE 0 END)
                            )
                        ) / NULLIF(usage_count + 1, 0),
                        health_state = CASE
                            WHEN usage_count + 1 < %s THEN health_state
                            WHEN (
                                (success_count + CASE WHEN %s = 'success' THEN 1 ELSE 0 END)::float
                                / NULLIF(usage_count + 1, 0)
                            ) < %s THEN 'degraded'
                            ELSE 'healthy'
                        END,
                        last_used_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        outcome,
                        outcome,
                        outcome,
                        outcome,
                        outcome,
                        max(1, int(min_usage_before_health_decision)),
                        outcome,
                        float(degraded_success_rate),
                        pattern_id,
                    ),
                )
    return {"status": "success", "events_recorded": len(unique_ids)}


def get_pattern_health(*, project_id: str | None = None, candidate_limit: int = 20) -> dict[str, Any]:
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute("SELECT to_regclass('core_brain.pattern_candidates') AS table_name")
                if not (cur.fetchone() or {}).get("table_name"):
                    return {"available": False, "reason": "pattern_intelligence_schema_not_installed"}

                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE status = 'active') AS active,
                        COUNT(*) FILTER (WHERE status = 'draft') AS draft,
                        COUNT(*) FILTER (WHERE status = 'deprecated') AS deprecated,
                        COUNT(*) FILTER (WHERE health_state = 'degraded') AS degraded,
                        COALESCE(SUM(usage_count), 0) AS usages,
                        COALESCE(SUM(contradiction_count), 0) AS open_contradictions
                    FROM core_brain.patterns
                    WHERE (%s::uuid IS NULL OR scope_project_id = %s::uuid OR scope_project_id IS NULL)
                    """,
                    (project_id, project_id),
                )
                patterns = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE status = 'shadow') AS shadow,
                        COUNT(*) FILTER (WHERE status = 'review') AS review,
                        COUNT(*) FILTER (WHERE status = 'accepted') AS accepted,
                        COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                        COUNT(*) FILTER (WHERE status = 'expired') AS expired,
                        COUNT(*) FILTER (WHERE quality_ready) AS quality_ready
                    FROM core_brain.pattern_candidates
                    WHERE (%s::uuid IS NULL OR scope_project_id = %s::uuid OR scope_project_id IS NULL)
                    """,
                    (project_id, project_id),
                )
                candidates = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT *
                    FROM core_brain.v_pattern_candidate_health
                    WHERE (%s::uuid IS NULL OR scope_project_id = %s::uuid OR scope_project_id IS NULL)
                    ORDER BY quality_ready DESC, quality_score DESC, last_seen_at DESC
                    LIMIT %s
                    """,
                    (project_id, project_id, max(1, min(int(candidate_limit), 100))),
                )
                top_candidates = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT *
                    FROM core_brain.pattern_discovery_runs
                    WHERE (%s::uuid IS NULL OR project_id = %s::uuid OR project_id IS NULL)
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (project_id, project_id),
                )
                latest_run = cur.fetchone()
        return {
            "available": True,
            "patterns": patterns,
            "candidates": candidates,
            "top_candidates": top_candidates,
            "latest_discovery_run": dict(latest_run) if latest_run else None,
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def get_activation_backlog(
    *, project_id: str | None = None, max_age_days: int = 90, limit: int = 50
) -> dict[str, Any]:
    """Devuelve el backlog de candidatos review-ready esperando
    aceptacion humana. Usado por la superficie de revision y por
    el maintenance cycle para reportar estado.

    Returns:
        dict con claves: count, oldest_age_days, items (lista resumida).
    """
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    "SELECT to_regclass('core_brain.pattern_candidates') AS table_name"
                )
                if not (cur.fetchone() or {}).get("table_name"):
                    return {
                        "available": False,
                        "count": 0,
                        "oldest_age_days": 0,
                        "items": [],
                        "reason": "pattern_intelligence_schema_not_installed",
                    }
                age_days = max(1, int(max_age_days))
                item_limit = max(1, min(int(limit), 100))
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS count,
                        COALESCE(
                            MAX(EXTRACT(DAY FROM (NOW() - last_seen_at))::int),
                            0
                        ) AS oldest_age_days
                    FROM core_brain.pattern_candidates
                    WHERE status = 'review'
                      AND quality_ready = TRUE
                      AND materialized_pattern_id IS NULL
                      AND last_seen_at >= NOW() - (%s || ' days')::interval
                      AND (%s::uuid IS NULL OR scope_project_id = %s::uuid OR scope_project_id IS NULL)
                    """,
                    (age_days, project_id, project_id),
                )
                summary = dict(cur.fetchone() or {})
                cur.execute(
                    """
                    SELECT
                        id, candidate_key, label, skill_name, status,
                        quality_score, support_count, context_diversity,
                        distinct_project_count, cohesion_score,
                        contradiction_count, last_seen_at,
                        EXTRACT(DAY FROM (NOW() - last_seen_at))::int AS age_days
                    FROM core_brain.pattern_candidates
                    WHERE status = 'review'
                      AND quality_ready = TRUE
                      AND materialized_pattern_id IS NULL
                      AND last_seen_at >= NOW() - (%s || ' days')::interval
                      AND (%s::uuid IS NULL OR scope_project_id = %s::uuid OR scope_project_id IS NULL)
                    ORDER BY quality_score DESC, last_seen_at ASC
                    LIMIT %s
                    """,
                    (age_days, project_id, project_id, item_limit),
                )
                items = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        return {
            "available": False,
            "count": 0,
            "oldest_age_days": 0,
            "items": [],
            "reason": str(exc),
        }
    return {
        "available": True,
        "count": int(summary.get("count") or 0),
        "oldest_age_days": int(summary.get("oldest_age_days") or 0),
        "items": items,
        "listed_count": len(items),
    }


def _candidate_review_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    gates = metadata.get("quality_gates") if isinstance(metadata.get("quality_gates"), dict) else {}
    passed_gates = sorted(str(name) for name, passed in gates.items() if passed is True)
    failed_gates = sorted(str(name) for name, passed in gates.items() if passed is False)
    risks: list[str] = []
    if int(candidate.get("contradiction_count") or 0) > 0:
        risks.append("open_contradictions")
    if float(candidate.get("avg_confidence") or 0.0) < 0.75:
        risks.append("low_avg_confidence")
    if float(candidate.get("cohesion_score") or 0.0) < 0.80:
        risks.append("low_cohesion")
    if (
        str(candidate.get("scope_type") or "") != "project"
        and int(candidate.get("distinct_project_count") or 0) < 2
    ):
        risks.append("single_project_evidence")
    return {
        "passed_gates": passed_gates,
        "failed_gates": failed_gates,
        "risks": risks,
    }


def list_review_ready_candidates(
    *, project_id: str | None = None, limit: int = 20, max_age_days: int = 90
) -> list[dict[str, Any]]:
    """Lista candidatos review-ready ordenados por quality_score desc.
    Usado por el CLI `pattern-review` para supervision humana."""
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                        c.id, c.candidate_key, c.label, c.summary, c.skill_name,
                        c.category, c.scope_type, c.scope_project_id,
                        c.quality_score, c.support_count, c.context_diversity,
                        c.distinct_project_count, c.cohesion_score,
                        c.contradiction_count, c.avg_confidence, c.last_seen_at,
                        c.metadata, COALESCE(evidence.top_evidence, '[]'::jsonb) AS top_evidence
                    FROM core_brain.pattern_candidates c
                    LEFT JOIN LATERAL (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'experience_id', ranked.experience_id,
                                'title', ranked.title,
                                'conclusion', ranked.conclusion,
                                'evidence_role', ranked.evidence_role,
                                'similarity', ranked.similarity,
                                'weight', ranked.weight
                            )
                            ORDER BY ranked.rank_order
                        ) AS top_evidence
                        FROM (
                            SELECT
                                pce.experience_id,
                                e.title,
                                LEFT(
                                    COALESCE(
                                        e.content ->> 'conclusion',
                                        e.task_description,
                                        ''
                                    ),
                                    320
                                ) AS conclusion,
                                pce.evidence_role,
                                pce.similarity,
                                pce.weight,
                                ROW_NUMBER() OVER (
                                    ORDER BY
                                        CASE pce.evidence_role
                                            WHEN 'support' THEN 0
                                            WHEN 'neutral' THEN 1
                                            ELSE 2
                                        END,
                                        pce.similarity DESC NULLS LAST,
                                        pce.weight DESC
                                ) AS rank_order
                            FROM core_brain.pattern_candidate_evidence pce
                            JOIN core_brain.experiences e ON e.id = pce.experience_id
                            WHERE pce.candidate_id = c.id
                            ORDER BY rank_order
                            LIMIT 3
                        ) ranked
                    ) evidence ON TRUE
                    WHERE c.status = 'review'
                      AND c.quality_ready = TRUE
                      AND c.materialized_pattern_id IS NULL
                      AND c.last_seen_at >= NOW() - (%s || ' days')::interval
                      AND (%s::uuid IS NULL
                           OR c.scope_project_id = %s::uuid
                           OR c.scope_project_id IS NULL)
                    ORDER BY c.quality_score DESC, c.last_seen_at ASC
                    LIMIT %s
                    """,
                    (
                        max(1, int(max_age_days)),
                        project_id,
                        project_id,
                        max(1, min(int(limit), 100)),
                    ),
                )
                candidates = [dict(row) for row in cur.fetchall()]
                for candidate in candidates:
                    candidate["review_summary"] = _candidate_review_summary(candidate)
                return candidates
    except Exception:
        return []
