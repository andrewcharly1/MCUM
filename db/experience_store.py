"""
Experience store and retrieval helpers for MCUM.
"""

from __future__ import annotations

import json
import math
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

from ..memory_freshness import apply_memory_freshness, summarize_freshness_warnings
from ..memory_governor import apply_memory_governor, summarize_governor_sections
from .connection import get_db, get_cursor
from .embedder import EMBEDDING_DIM, cosine_similarity, embed
from .project_registry import estimate_tokens


ROOT = Path(__file__).resolve().parent.parent
RETRIEVAL_POLICY_FILE = ROOT / "directives" / "retrieval_policy.json"

VALID_CATEGORIES = {
    "stack_decision",
    "architecture_pattern",
    "implementation_recipe",
    "testing_strategy",
    "prompting_heuristic",
    "failure_pattern",
    "regulatory_rule",
    "evaluation_policy",
}

DEFAULT_RETRIEVAL_POLICY = {
    "max_experiences": 5,
    "min_confidence": 0.30,
    "top_relevant_slots": 3,
    "failure_pattern_slot": 1,
    "conflict_slot": 1,
    "pattern_slot": 2,
    "feedback_signal_slot": 5,
    "min_semantic_score": 0.30,
    "semantic_weight": 0.60,
    "confidence_weight": 0.40,
    "diversity_enforced": True,
    "max_token_budget": 4000,
    "project_first": True,
    "allow_cross_project_fallback": True,
    "cross_project_fallback_only_if_no_project_hits": True,
    "max_cross_project_memories": 1,
    "memory_governor": {
        "enabled": True,
        "mode": "assist",
        "preserve_at_least": 1,
    },
}

QUERY_EMBED_CACHE_LIMIT = 128
_QUERY_EMBED_CACHE: dict[str, list[float]] = {}

# pgvector runtime detection (lazy, cached)
_PGVECTOR_ENABLED: bool | None = None
_PGVECTOR_LAST_CHECK_TS = 0.0
_PGVECTOR_CACHE_TTL_SEC = 300


def _read_pgvector_enabled_state() -> bool:
    """Read the current storage mode from PostgreSQL."""
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute("""
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'core_brain'
                      AND table_name = 'experiences'
                      AND column_name = 'embedding'
                """)
                row = cur.fetchone()
                return row is not None and row["data_type"] == "USER-DEFINED"
    except Exception:
        return False


def _is_pgvector_enabled(force_refresh: bool = False) -> bool:
    """Check if the embedding column is vector(384) vs JSONB."""
    global _PGVECTOR_ENABLED, _PGVECTOR_LAST_CHECK_TS
    now = time.time()
    cache_is_fresh = (
        not force_refresh
        and _PGVECTOR_ENABLED is not None
        and (now - _PGVECTOR_LAST_CHECK_TS) < _PGVECTOR_CACHE_TTL_SEC
    )
    if cache_is_fresh:
        return _PGVECTOR_ENABLED

    _PGVECTOR_ENABLED = _read_pgvector_enabled_state()
    _PGVECTOR_LAST_CHECK_TS = now
    return _PGVECTOR_ENABLED


def _validate_embedding(embedding: Any) -> list[float]:
    """Validate and normalize an embedding before persistence or search."""
    try:
        values = list(embedding)
    except TypeError as exc:
        raise ValueError("embedding must be an iterable of numeric values") from exc

    if len(values) != EMBEDDING_DIM:
        raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions, got {len(values)}")

    normalized: list[float] = []
    for index, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"embedding[{index}] is not numeric: {value!r}") from exc
        if not math.isfinite(number):
            raise ValueError(f"embedding[{index}] must be finite, got {number!r}")
        normalized.append(number)
    return normalized


def _embedding_to_vector_literal(embedding: Any) -> str:
    normalized = _validate_embedding(embedding)
    return "[" + ",".join(str(x) for x in normalized) + "]"


def _embedding_to_sql(embedding: Any) -> str:
    """Format embedding for SQL storage based on active mode."""
    if _is_pgvector_enabled():
        return _embedding_to_vector_literal(embedding)
    return json.dumps(_validate_embedding(embedding))


def _normalize_json_field(value: Any, default: Any = None) -> Any:
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default if default is not None else value
    return value


def _normalize_experience_row(row: dict) -> dict:
    normalized = dict(row)
    dict_fields = ("content", "applicability", "not_applicable_cases", "conditions")
    list_fields = ("evidence_refs", "source_artifacts")
    for field in dict_fields:
        if field in normalized:
            normalized[field] = _normalize_json_field(normalized[field], {})
    for field in list_fields:
        if field in normalized:
            normalized[field] = _normalize_json_field(normalized[field], [])
    return normalized


def _normalize_pattern_row(row: dict) -> dict:
    normalized = dict(row)
    list_fields = ("evidence_ids", "evidence_projects", "evidence_skills")
    for field in list_fields:
        if field in normalized:
            normalized[field] = _normalize_json_field(normalized[field], [])
    return normalized


def _normalize_retrieval_score_rows(rows: Any) -> list[dict]:
    if not rows:
        return []
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except json.JSONDecodeError:
            return []
    if not isinstance(rows, list):
        return []
    normalized: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(row)
    return normalized


def _keyword_overlap_score(left_tokens: set[str], right_tokens: set[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _unique_id_list(items: list[dict], key: str = "id") -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        item_id = str(item.get(key) or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        ordered.append(item_id)
    return ordered


def _load_retrieval_policy(policy: dict | None = None) -> dict:
    resolved = dict(DEFAULT_RETRIEVAL_POLICY)
    if policy:
        resolved.update(policy)
        return resolved

    if not RETRIEVAL_POLICY_FILE.exists():
        return resolved

    raw = json.loads(RETRIEVAL_POLICY_FILE.read_text(encoding="utf-8"))
    limits = raw.get("limits", {})
    slots = raw.get("slot_allocation", {})
    ranking = raw.get("ranking", {})
    semantic = raw.get("semantic", {})

    resolved.update(
        {
            "max_experiences": limits.get("max_total_experiences", resolved["max_experiences"]),
            "min_confidence": limits.get(
                "min_confidence_threshold",
                raw.get("filters", {}).get("exclude_confidence_below", resolved["min_confidence"]),
            ),
            "top_relevant_slots": slots.get("top_relevant", resolved["top_relevant_slots"]),
            "failure_pattern_slot": slots.get(
                "failure_pattern", resolved["failure_pattern_slot"]
            ),
            "conflict_slot": slots.get("active_conflict", resolved["conflict_slot"]),
            "pattern_slot": slots.get("active_pattern", resolved["pattern_slot"]),
            "max_token_budget": limits.get("max_token_budget", resolved["max_token_budget"]),
            "diversity_enforced": raw.get("diversity", {}).get(
                "enforce_category_diversity", resolved["diversity_enforced"]
            ),
            "min_semantic_score": semantic.get(
                "min_similarity", resolved["min_semantic_score"]
            ),
        }
    )

    weights = ranking.get("weights", {})
    confidence_weight = semantic.get("confidence_weight", weights.get("confidence"))
    semantic_weight = semantic.get("semantic_weight", weights.get("semantic"))

    if confidence_weight is not None:
        resolved["confidence_weight"] = min(max(confidence_weight, 0.0), 1.0)
    if semantic_weight is not None:
        resolved["semantic_weight"] = min(max(semantic_weight, 0.0), 1.0)
    elif confidence_weight is not None:
        resolved["semantic_weight"] = round(1.0 - resolved["confidence_weight"], 2)

    return resolved


def _default_applicability(title: str, task_description: str | None) -> dict:
    return {"when": f"When solving the same class of problem as: {task_description or title}"}


def _default_not_applicable() -> dict:
    return {
        "when_not": "When the stack, business rule, runtime assumptions, or user goal differ materially."
    }


def _build_experience_filters(
    min_confidence: float,
    category: str | None = None,
    skill_name: str | None = None,
    project_id: str | None = None,
    alias: str | None = None,
    require_embedding: bool = False,
) -> tuple[list[str], list[Any]]:
    prefix = f"{alias}." if alias else ""
    conditions = [f"{prefix}current_confidence >= %s", f"{prefix}superseded_by IS NULL"]
    if require_embedding:
        conditions.append(f"{prefix}embedding IS NOT NULL")

    params: list[Any] = [min_confidence]
    if category:
        conditions.append(f"{prefix}category = %s")
        params.append(category)
    if skill_name:
        conditions.append(f"{prefix}skill_name = %s")
        params.append(skill_name)
    if project_id:
        conditions.append(f"{prefix}project_id = %s")
        params.append(project_id)
    return conditions, params


def _embed_query_cached(query_text: str) -> list[float]:
    key = (query_text or "").strip()
    if not key:
        return _validate_embedding(embed(""))

    cached = _QUERY_EMBED_CACHE.get(key)
    if cached is not None:
        return cached

    vector = _validate_embedding(embed(key))
    if len(_QUERY_EMBED_CACHE) >= QUERY_EMBED_CACHE_LIMIT:
        _QUERY_EMBED_CACHE.clear()
    _QUERY_EMBED_CACHE[key] = vector
    return vector


def _find_duplicate_experience(
    category: str,
    title: str,
    skill_name: str,
    task_description: str | None,
    conclusion: str,
) -> str | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id
                FROM core_brain.experiences
                WHERE category = %s
                  AND skill_name = %s
                  AND title = %s
                  AND COALESCE(task_description, '') = COALESCE(%s, '')
                  AND COALESCE(content->>'conclusion', '') = %s
                  AND superseded_by IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (category, skill_name, title, task_description, conclusion),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else None


def save_experience(
    category: str,
    title: str,
    content: dict,
    skill_name: str,
    project_id: str | None = None,
    task_description: str | None = None,
    applicability: dict | None = None,
    not_applicable_cases: dict | None = None,
    conditions: dict | None = None,
    evidence_refs: list[dict] | None = None,
    source_artifacts: list[dict] | None = None,
    review_notes: str | None = None,
    initial_score: float = 0.80,
    tested_by: str = "agent",
    skill_version: str | None = None,
    is_synthetic: bool = False,
) -> str:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: {category}")
    if not 0.0 <= initial_score <= 1.0:
        raise ValueError("initial_score must be in [0.0, 1.0]")

    content = content or {"conclusion": "Task completed"}
    conclusion = str(content.get("conclusion", "")).strip()
    applicability = applicability or _default_applicability(title, task_description)
    not_applicable_cases = not_applicable_cases or _default_not_applicable()
    raw_embedding = embed(f"{title} | {conclusion} | {task_description or ''}")
    embedding_value = _embedding_to_sql(raw_embedding)

    duplicate_id = _find_duplicate_experience(
        category=category,
        title=title,
        skill_name=skill_name,
        task_description=task_description,
        conclusion=conclusion,
    )

    with get_db() as conn:
        with get_cursor(conn) as cur:
            if duplicate_id:
                cur.execute(
                    """
                    UPDATE core_brain.experiences
                    SET content = %s,
                        applicability = %s,
                        not_applicable_cases = %s,
                        conditions = %s,
                        evidence_refs = %s,
                        source_artifacts = %s,
                        review_notes = %s,
                        current_confidence = GREATEST(current_confidence, %s),
                        initial_score = GREATEST(initial_score, %s),
                        project_id = COALESCE(project_id, %s),
                        skill_version = COALESCE(%s, skill_version),
                        embedding = %s,
                        last_validated_at = NOW(),
                        revalidation_count = revalidation_count + 1
                    WHERE id = %s
                    """,
                    (
                        json.dumps(content),
                        json.dumps(applicability),
                        json.dumps(not_applicable_cases),
                        json.dumps(conditions or {}),
                        json.dumps(evidence_refs or []),
                        json.dumps(source_artifacts or []),
                        review_notes,
                        initial_score,
                        initial_score,
                        project_id,
                        skill_version,
                        embedding_value,
                        duplicate_id,
                    ),
                )
                return duplicate_id

            exp_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO core_brain.experiences (
                    id, category, title, content,
                    applicability, not_applicable_cases, conditions,
                    initial_score, current_confidence,
                    evidence_refs, source_artifacts, review_notes,
                    is_synthetic, tested_by,
                    project_id, skill_name, skill_version, task_description,
                    embedding
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s
                )
                RETURNING id
                """,
                (
                    exp_id,
                    category,
                    title,
                    json.dumps(content),
                    json.dumps(applicability),
                    json.dumps(not_applicable_cases),
                    json.dumps(conditions or {}),
                    initial_score,
                    initial_score,
                    json.dumps(evidence_refs or []),
                    json.dumps(source_artifacts or []),
                    review_notes,
                    is_synthetic,
                    tested_by,
                    project_id,
                    skill_name,
                    skill_version,
                    task_description,
                    embedding_value,
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else exp_id


def find_duplicate_experience_groups(
    *,
    project_id: str | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    policy = dict(policy or {})
    duplicate_policy = dict(policy.get("duplicate_consolidation") or {})
    min_group_size = max(2, int(duplicate_policy.get("min_group_size", 2) or 2))
    max_groups = max(1, int(duplicate_policy.get("max_groups_per_run", 20) or 20))

    conditions = ["superseded_by IS NULL"]
    params: list[Any] = []
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)

    params.extend([min_group_size, max_groups])
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                WITH grouped AS (
                    SELECT
                        project_id,
                        category,
                        skill_name,
                        LOWER(TRIM(COALESCE(title, ''))) AS normalized_title,
                        COALESCE(task_description, '') AS normalized_task_description,
                        COALESCE(content->>'conclusion', '') AS normalized_conclusion,
                        COUNT(*) AS group_size,
                        ARRAY_AGG(
                            id
                            ORDER BY
                                COALESCE(revalidation_count, 0) DESC,
                                current_confidence DESC,
                                COALESCE(unique_context_count, 0) DESC,
                                created_at DESC,
                                id
                        ) AS ids
                    FROM core_brain.experiences
                    WHERE {' AND '.join(conditions)}
                    GROUP BY 1, 2, 3, 4, 5, 6
                    HAVING COUNT(*) >= %s
                )
                SELECT *
                FROM grouped
                ORDER BY group_size DESC, normalized_title ASC
                LIMIT %s
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]

    groups: list[dict[str, Any]] = []
    for row in rows:
        raw_ids = [str(value) for value in row.get("ids") or [] if value]
        if len(raw_ids) < min_group_size:
            continue
        canonical_id = raw_ids[0]
        duplicate_ids = raw_ids[1:]
        if not canonical_id or not duplicate_ids:
            continue
        groups.append(
            {
                "project_id": row.get("project_id"),
                "category": row.get("category"),
                "skill_name": row.get("skill_name"),
                "normalized_title": row.get("normalized_title") or "",
                "normalized_task_description": row.get("normalized_task_description") or "",
                "normalized_conclusion": row.get("normalized_conclusion") or "",
                "group_size": int(row.get("group_size") or len(raw_ids)),
                "canonical_id": canonical_id,
                "duplicate_ids": duplicate_ids,
                "ids": raw_ids,
            }
        )
    return groups


def consolidate_duplicate_experiences(
    *,
    project_id: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    duplicate_policy = dict(policy.get("duplicate_consolidation") or {})
    review_note = str(
        duplicate_policy.get(
            "review_note",
            "Merged by MCUM maintenance: exact duplicate experience superseded by canonical record.",
        )
        or ""
    ).strip()
    sample_limit = max(1, int(duplicate_policy.get("sample_limit", 5) or 5))

    groups = find_duplicate_experience_groups(project_id=project_id, policy=policy)
    if not groups:
        return {
            "project_id": project_id,
            "groups_considered": 0,
            "groups_merged": 0,
            "experiences_superseded": 0,
            "samples": [],
            "mode": "exact_match_only",
        }

    merged_groups = 0
    experiences_superseded = 0
    samples: list[dict[str, Any]] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            for group in groups:
                duplicate_ids = [str(value) for value in group.get("duplicate_ids") or [] if value]
                canonical_id = str(group.get("canonical_id") or "").strip()
                if not canonical_id or not duplicate_ids:
                    continue

                cur.execute(
                    """
                    UPDATE core_brain.experiences
                    SET superseded_by = %s,
                        review_notes = CASE
                            WHEN COALESCE(review_notes, '') = '' THEN %s
                            ELSE review_notes || E'\n' || %s
                        END,
                        last_validated_at = NOW()
                    WHERE id = ANY(%s)
                      AND superseded_by IS NULL
                    """,
                    (canonical_id, review_note, review_note, duplicate_ids),
                )
                updated = int(getattr(cur, "rowcount", 0) or 0)
                if updated <= 0:
                    continue
                merged_groups += 1
                experiences_superseded += updated
                if len(samples) < sample_limit:
                    samples.append(
                        {
                            "canonical_id": canonical_id,
                            "duplicate_ids": duplicate_ids[:sample_limit],
                            "group_size": int(group.get("group_size") or (len(duplicate_ids) + 1)),
                            "category": group.get("category"),
                            "skill_name": group.get("skill_name"),
                            "normalized_title": group.get("normalized_title") or "",
                        }
                    )

    return {
        "project_id": project_id,
        "groups_considered": len(groups),
        "groups_merged": merged_groups,
        "experiences_superseded": experiences_superseded,
        "samples": samples,
        "mode": "exact_match_only",
    }


def update_confidence(
    experience_id: str,
    new_confidence: float,
    revalidated: bool = True,
    new_context: bool = False,
) -> bool:
    if not 0.0 <= new_confidence <= 1.0:
        raise ValueError("new_confidence must be in [0.0, 1.0]")

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.experiences
                SET current_confidence = %s,
                    last_validated_at = NOW(),
                    revalidation_count = revalidation_count + %s,
                    unique_context_count = unique_context_count + %s
                WHERE id = %s
                """,
                (
                    new_confidence,
                    1 if revalidated else 0,
                    1 if new_context else 0,
                    experience_id,
                ),
                )
            return getattr(cur, "rowcount", 1) > 0


def adjust_confidence(
    experience_id: str,
    delta: float,
    revalidated: bool = True,
    new_context: bool = False,
    floor: float = 0.05,
    ceiling: float = 1.0,
) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.experiences
                SET current_confidence = LEAST(%s, GREATEST(%s, current_confidence + %s)),
                    last_validated_at = NOW(),
                    revalidation_count = revalidation_count + %s,
                    unique_context_count = unique_context_count + %s
                WHERE id = %s
                """,
                (
                    ceiling,
                    floor,
                    delta,
                    1 if revalidated else 0,
                    1 if new_context else 0,
                    experience_id,
                ),
            )
            return cur.rowcount > 0


def add_conflict(experience_id: str, conflicting_id: str) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.experiences
                SET conflict_refs = array_append(conflict_refs, %s::uuid)
                WHERE id = %s
                  AND NOT (%s::uuid = ANY(COALESCE(conflict_refs, '{}')))
                """,
                (conflicting_id, experience_id, conflicting_id),
            )
            cur.execute(
                """
                UPDATE core_brain.experiences
                SET conflict_refs = array_append(conflict_refs, %s::uuid)
                WHERE id = %s
                  AND NOT (%s::uuid = ANY(COALESCE(conflict_refs, '{}')))
                """,
                (experience_id, conflicting_id, experience_id),
            )
    return True


def semantic_search(
    query_text: str,
    category: str | None = None,
    skill_name: str | None = None,
    project_id: str | None = None,
    min_confidence: float = 0.30,
    min_similarity: float = 0.25,
    limit: int = 10,
) -> list[dict]:
    conditions, params = _build_experience_filters(
        min_confidence=min_confidence,
        category=category,
        skill_name=skill_name,
        project_id=project_id,
        require_embedding=True,
    )
    query_embedding = _embed_query_cached(query_text)
    policy = _load_retrieval_policy()
    sem_w = policy["semantic_weight"]
    conf_w = policy["confidence_weight"]

    # --- pgvector path: DB-side ANN search via HNSW index ---
    if _is_pgvector_enabled():
        embedding_literal = _embedding_to_vector_literal(query_embedding)
        aliased_conditions, aliased_params = _build_experience_filters(
            min_confidence=min_confidence,
            category=category,
            skill_name=skill_name,
            project_id=project_id,
            alias="e",
            require_embedding=True,
        )
        where_clause = " AND ".join(aliased_conditions)
        # Inject weights as SQL literals (internal values, not user input)
        sem_w_lit = f"{float(sem_w):.6f}"
        conf_w_lit = f"{float(conf_w):.6f}"

        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    f"""
                    WITH query_vec AS (
                        SELECT %s::vector AS vec
                    )
                    SELECT
                        e.id, e.category, e.title, e.content,
                        e.applicability, e.not_applicable_cases, e.conditions,
                        e.current_confidence, e.revalidation_count, e.unique_context_count,
                        e.tested_by, e.skill_name, e.skill_version, e.project_id,
                        e.task_description, e.conflict_refs, e.source_artifacts,
                        1 - (e.embedding <=> q.vec) AS similarity,
                        e.created_at, e.last_validated_at
                    FROM core_brain.experiences e, query_vec q
                    WHERE {where_clause}
                      AND 1 - (e.embedding <=> q.vec) >= %s
                    ORDER BY (
                        {sem_w_lit} * (1 - (e.embedding <=> q.vec))
                        + {conf_w_lit} * e.current_confidence
                    ) DESC
                    LIMIT %s
                    """,
                    [embedding_literal] + aliased_params + [min_similarity, limit],
                )
                results = []
                for row in cur.fetchall():
                    enriched = _normalize_experience_row(dict(row))
                    sim = float(row["similarity"])
                    enriched["_similarity"] = round(sim, 4)
                    enriched["_combined_score"] = round(sem_w * sim + conf_w * enriched["current_confidence"], 4)
                    results.append(enriched)
                return apply_memory_freshness(
                    results,
                    kind="experience",
                    score_key="_combined_score",
                )

    # --- JSONB fallback: Python-side cosine similarity ---
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT
                    id, category, title, content,
                    applicability, not_applicable_cases, conditions,
                    current_confidence, revalidation_count, unique_context_count,
                    tested_by, skill_name, skill_version, project_id,
                    task_description, conflict_refs, source_artifacts, embedding,
                    created_at, last_validated_at
                FROM core_brain.experiences
                WHERE {' AND '.join(conditions)}
                ORDER BY current_confidence DESC
                LIMIT 200
                """,
                params,
            )
            candidates = [_normalize_experience_row(dict(row)) for row in cur.fetchall()]

    if not candidates:
        return []

    scored: list[dict] = []
    for candidate in candidates:
        candidate_embedding = _normalize_json_field(candidate.get("embedding"), [])
        if not candidate_embedding:
            continue
        try:
            candidate_embedding = _validate_embedding(candidate_embedding)
        except ValueError:
            continue

        similarity = cosine_similarity(query_embedding, candidate_embedding)
        if similarity < min_similarity:
            continue

        combined_score = sem_w * similarity + conf_w * candidate["current_confidence"]
        enriched = dict(candidate)
        enriched["_similarity"] = round(similarity, 4)
        enriched["_combined_score"] = round(combined_score, 4)
        scored.append(enriched)

    scored = apply_memory_freshness(
        scored,
        kind="experience",
        score_key="_combined_score",
    )
    return scored[:limit]


def search_by_keywords(
    keywords: list[str],
    category: str | None = None,
    skill_name: str | None = None,
    project_id: str | None = None,
    min_confidence: float = 0.30,
    limit: int = 10,
) -> list[dict]:
    conditions = ["current_confidence >= %s", "superseded_by IS NULL"]
    params: list[Any] = [min_confidence]

    if keywords:
        fragments = []
        for keyword in keywords:
            fragments.append("(LOWER(title) LIKE %s OR content::text ILIKE %s)")
            params.extend([f"%{keyword.lower()}%", f"%{keyword}%"])
        conditions.append(f"({' OR '.join(fragments)})")

    if category:
        conditions.append("category = %s")
        params.append(category)
    if skill_name:
        conditions.append("skill_name = %s")
        params.append(skill_name)
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)

    params.append(limit)
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT
                    id, category, title, content,
                    applicability, not_applicable_cases, conditions,
                    current_confidence, revalidation_count, unique_context_count,
                    tested_by, skill_name, skill_version, project_id,
                    task_description, conflict_refs, source_artifacts, created_at, last_validated_at
                FROM core_brain.experiences
                WHERE {' AND '.join(conditions)}
                ORDER BY current_confidence DESC
                LIMIT %s
                """,
                params,
            )
            results = []
            for row in cur.fetchall():
                enriched = _normalize_experience_row(dict(row))
                enriched["_combined_score"] = round(float(enriched.get("current_confidence") or 0.0), 4)
                results.append(enriched)
            return apply_memory_freshness(
                results,
                kind="experience",
                score_key="_combined_score",
            )


def get_failure_patterns(
    query_text: str | None = None,
    project_id: str | None = None,
    min_confidence: float = 0.30,
    limit: int = 3,
) -> list[dict]:
    if query_text:
        return semantic_search(
            query_text=query_text,
            category="failure_pattern",
            project_id=project_id,
            min_confidence=min_confidence,
            limit=limit,
        )
    return search_by_keywords(
        keywords=[],
        category="failure_pattern",
        project_id=project_id,
        min_confidence=min_confidence,
        limit=limit,
    )


def _fetch_active_pattern_candidates(
    project_id: str | None = None,
    skill_name: str | None = None,
    min_confidence: float = 0.30,
    limit: int = 10,
) -> list[dict]:
    conditions = [
        "p.status = 'active'",
        "COALESCE(p.health_state, 'observing') <> 'degraded'",
        "COALESCE(p.avg_score, 0.0) >= %s",
    ]
    params: list[Any] = [min_confidence]
    if project_id:
        conditions.append("e.project_id = %s")
        params.append(project_id)
    if skill_name:
        conditions.append(
            "(p.scope_skill_name = %s OR e.skill_name = %s OR p.name ILIKE %s OR p.description ILIKE %s)"
        )
        params.extend([skill_name, skill_name, f"%{skill_name}%", f"%{skill_name}%"])

    params.append(limit)
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT
                    p.id, p.name, p.description, p.category, p.status,
                    p.promotion_criteria_met, p.experience_count, p.avg_score,
                    p.support_count, p.context_diversity, p.health_state, p.utility_score,
                    p.scope_type, p.scope_project_id, p.scope_skill_name,
                    p.usage_count, p.deprecated_at, p.deprecated_reason,
                    p.replacement_pattern_id, p.created_at, p.updated_at,
                    ARRAY_REMOVE(ARRAY_AGG(DISTINCT pe.experience_id), NULL) AS evidence_ids,
                    ARRAY_REMOVE(ARRAY_AGG(DISTINCT e.project_id), NULL) AS evidence_projects,
                    ARRAY_REMOVE(ARRAY_AGG(DISTINCT e.skill_name), NULL) AS evidence_skills
                FROM core_brain.patterns p
                LEFT JOIN core_brain.pattern_evidence pe ON pe.pattern_id = p.id
                LEFT JOIN core_brain.experiences e ON e.id = pe.experience_id
                WHERE {' AND '.join(conditions)}
                GROUP BY
                    p.id, p.name, p.description, p.category, p.status,
                    p.promotion_criteria_met, p.experience_count, p.avg_score,
                    p.support_count, p.context_diversity, p.health_state, p.utility_score,
                    p.scope_type, p.scope_project_id, p.scope_skill_name,
                    p.usage_count, p.deprecated_at, p.deprecated_reason,
                    p.replacement_pattern_id, p.created_at, p.updated_at
                ORDER BY p.avg_score DESC, p.experience_count DESC, p.context_diversity DESC, p.name ASC
                LIMIT %s
                """,
                params,
            )
            return [_normalize_pattern_row(dict(row)) for row in cur.fetchall()]


def _score_pattern_candidate(query_text: str, pattern: dict) -> tuple[float, list[str]]:
    query_tokens = set(_extract_keywords(query_text))
    pattern_tokens = set(
        _extract_keywords(
            " ".join(
                str(part or "")
                for part in (
                    pattern.get("name"),
                    pattern.get("description"),
                    pattern.get("category"),
                )
            )
        )
    )
    evidence_terms: list[str] = []
    for field in ("evidence_projects", "evidence_skills"):
        for term in pattern.get(field) or []:
            evidence_terms.append(str(term or ""))
    evidence_tokens = set(_extract_keywords(" ".join(evidence_terms)))
    token_overlap = _keyword_overlap_score(query_tokens, pattern_tokens | evidence_tokens)
    avg_score = min(1.0, float(pattern.get("avg_score") or 0.0))
    evidence_count = int(pattern.get("experience_count") or pattern.get("support_count") or 0)
    experience_count = min(1.0, float(evidence_count) / 5.0)
    diversity_score = min(1.0, float(pattern.get("context_diversity") or 0.0) / 5.0)
    score = round(
        (token_overlap * 0.45)
        + (avg_score * 0.30)
        + (experience_count * 0.15)
        + (diversity_score * 0.10),
        4,
    )
    reasons = [
        f"token_overlap={token_overlap:.2f}",
        f"avg_score={avg_score:.2f}",
        f"evidence_count={evidence_count}",
    ]
    return score, reasons


def get_active_patterns(
    query_text: str | None = None,
    project_id: str | None = None,
    skill_name: str | None = None,
    min_confidence: float = 0.30,
    limit: int = 3,
) -> list[dict]:
    try:
        candidates = _fetch_active_pattern_candidates(
            project_id=project_id,
            skill_name=skill_name,
            min_confidence=min_confidence,
            limit=max(limit * 4, limit),
        )

        if not candidates and project_id:
            candidates = _fetch_active_pattern_candidates(
                project_id=None,
                skill_name=skill_name,
                min_confidence=min_confidence,
                limit=max(limit * 4, limit),
            )
    except Exception:
        return []

    scored: list[dict] = []
    for candidate in candidates:
        score, reasons = _score_pattern_candidate(query_text or "", candidate)
        if score <= 0.0 and query_text:
            continue
        enriched = dict(candidate)
        enriched["_combined_score"] = score
        enriched["_utility_reasons"] = reasons
        scored.append(enriched)

    scored.sort(
        key=lambda item: (
            float(item.get("_combined_score") or 0.0),
            float(item.get("avg_score") or 0.0),
            int(item.get("experience_count") or 0),
        ),
        reverse=True,
    )
    return scored[:limit]


def get_recent_feedback_signals(
    query_text: str | None = None,
    project_id: str | None = None,
    skill_name: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    conditions = ["user_feedback IS NOT NULL"]
    params: list[Any] = []
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if skill_name:
        conditions.append("skill_name = %s")
        params.append(skill_name)

    keywords = _extract_keywords(query_text or "")
    if keywords:
        keyword_fragments = []
        for keyword in keywords[:5]:
            keyword_fragments.append(
                "("
                "LOWER(input_context) LIKE %s OR "
                "LOWER(COALESCE(outcome_description, '')) LIKE %s OR "
                "LOWER(COALESCE(decision_taken, '')) LIKE %s"
                ")"
            )
            like_value = f"%{keyword.lower()}%"
            params.extend([like_value, like_value, like_value])
        conditions.append(f"({' OR '.join(keyword_fragments)})")

    params.append(limit)
    positive_ids: set[str] = set()
    negative_ids: set[str] = set()
    positive_pattern_ids: set[str] = set()
    negative_pattern_ids: set[str] = set()
    signals: list[dict[str, Any]] = []

    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    f"""
                    SELECT
                        id, session_id, skill_name, input_context,
                        experiences_retrieved, patterns_retrieved,
                        retrieval_scores, outcome_status, outcome_description,
                        user_feedback, failure_reason, project_id, created_at
                    FROM core_brain.retrieval_runs
                    WHERE {' AND '.join(conditions)}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                for row in cur.fetchall():
                    feedback = row.get("user_feedback")
                    if feedback is None:
                        continue
                    scores = _normalize_retrieval_score_rows(row.get("retrieval_scores"))
                    experience_ids = [str(item.get("id") or "").strip() for item in scores if str(item.get("role") or "") == "experience" and str(item.get("id") or "").strip()]
                    pattern_ids = [str(item.get("id") or "").strip() for item in scores if str(item.get("role") or "") == "active_pattern" and str(item.get("id") or "").strip()]
                    failure_pattern_ids = [
                        str(item.get("id") or "").strip()
                        for item in scores
                        if str(item.get("role") or "") == "failure_pattern" and str(item.get("id") or "").strip()
                    ]
                    conflict_ids = [
                        str(item.get("id") or "").strip()
                        for item in scores
                        if str(item.get("role") or "") == "conflict_case" and str(item.get("id") or "").strip()
                    ]
                    if feedback > 0:
                        positive_ids.update(experience_ids)
                        positive_pattern_ids.update(pattern_ids)
                        positive_pattern_ids.update(failure_pattern_ids)
                        positive_pattern_ids.update(conflict_ids)
                    elif feedback < 0:
                        negative_ids.update(experience_ids)
                        negative_pattern_ids.update(pattern_ids)
                        negative_pattern_ids.update(failure_pattern_ids)
                        negative_pattern_ids.update(conflict_ids)
                    signals.append(
                        {
                            "id": str(row.get("id")),
                            "user_feedback": int(feedback),
                            "outcome_status": row.get("outcome_status"),
                            "outcome_description": row.get("outcome_description"),
                            "decision_taken": row.get("decision_taken"),
                            "input_context": row.get("input_context"),
                            "failure_reason": row.get("failure_reason"),
                            "created_at": row.get("created_at"),
                            "experience_ids": experience_ids,
                            "pattern_ids": pattern_ids,
                            "failure_pattern_ids": failure_pattern_ids,
                            "conflict_ids": conflict_ids,
                        }
                    )
    except Exception:
        return {
            "signals": [],
            "positive_experience_ids": [],
            "negative_experience_ids": [],
            "positive_pattern_ids": [],
            "negative_pattern_ids": [],
            "summary": {
                "signals_n": 0,
                "positive_n": 0,
                "negative_n": 0,
                "positive_experience_ids": 0,
                "negative_experience_ids": 0,
                "positive_pattern_ids": 0,
                "negative_pattern_ids": 0,
            },
        }

    return {
        "signals": signals,
        "positive_experience_ids": sorted(positive_ids),
        "negative_experience_ids": sorted(negative_ids),
        "positive_pattern_ids": sorted(positive_pattern_ids),
        "negative_pattern_ids": sorted(negative_pattern_ids),
        "summary": {
            "signals_n": len(signals),
            "positive_n": sum(1 for item in signals if int(item.get("user_feedback") or 0) > 0),
            "negative_n": sum(1 for item in signals if int(item.get("user_feedback") or 0) < 0),
            "positive_experience_ids": len(positive_ids),
            "negative_experience_ids": len(negative_ids),
            "positive_pattern_ids": len(positive_pattern_ids),
            "negative_pattern_ids": len(negative_pattern_ids),
        },
    }


def _extract_keywords(text: str) -> list[str]:
    stopwords = {
        "de",
        "la",
        "el",
        "en",
        "un",
        "una",
        "que",
        "es",
        "para",
        "con",
        "por",
        "los",
        "las",
        "del",
        "al",
        "se",
        "mi",
        "tu",
        "a",
        "y",
        "o",
        "e",
        "u",
        "como",
        "si",
        "no",
        "mas",
        "muy",
        "the",
        "is",
        "for",
        "to",
        "of",
        "and",
        "or",
        "this",
        "that",
    }
    words = text.lower().split()
    return [
        word.strip(".,;:!?()[]{}\"'")
        for word in words
        if len(word.strip(".,;:!?()[]{}\"'")) > 3
        and word.strip(".,;:!?()[]{}\"'") not in stopwords
    ][:10]


def _estimate_retrieval_item_tokens(item: dict) -> int:
    payload = {
        key: value
        for key, value in item.items()
        if key not in {"embedding"} and not str(key).startswith("_")
    }
    return estimate_tokens(payload)


def _apply_token_budget(
    experiences: list[dict],
    failure_patterns: list[dict],
    conflict_cases: list[dict],
    active_patterns: list[dict],
    budget: int | None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], int, list[str]]:
    if budget is None or budget <= 0:
        total_tokens = sum(
            _estimate_retrieval_item_tokens(item)
            for item in (experiences + failure_patterns + conflict_cases + active_patterns)
        )
        return experiences, failure_patterns, conflict_cases, active_patterns, total_tokens, []

    selected = {
        "experiences": [],
        "failure_patterns": [],
        "conflict_cases": [],
        "active_patterns": [],
    }
    skipped = 0
    total_tokens = 0
    warnings: list[str] = []

    for group_name, items in (
        ("experiences", experiences),
        ("failure_patterns", failure_patterns),
        ("conflict_cases", conflict_cases),
        ("active_patterns", active_patterns),
    ):
        for item in items:
            item_tokens = _estimate_retrieval_item_tokens(item)
            if total_tokens > 0 and total_tokens + item_tokens > budget:
                skipped += 1
                continue
            if total_tokens == 0 and item_tokens > budget:
                warnings.append(
                    f"Top retrieval item exceeded token budget ({item_tokens}>{budget}) and was included anyway."
                )
            selected[group_name].append(item)
            total_tokens += item_tokens

    if skipped:
        warnings.append(
            f"Context budget truncated {skipped} retrieval item(s) to stay under {budget} tokens."
        )

    return (
        selected["experiences"],
        selected["failure_patterns"],
        selected["conflict_cases"],
        selected["active_patterns"],
        total_tokens,
        warnings,
    )


def _summarize_scope_learning_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    sample_count = int(profile.get("sample_count") or 0)
    if sample_count <= 0:
        return None
    return {
        "scope": profile.get("scope"),
        "sample_count": sample_count,
        "active": bool(profile.get("active")),
        "score_delta": float(profile.get("score_delta") or 0.0),
        "eager_cross_project": bool(profile.get("eager_cross_project")),
        "prefer_same_project_only": bool(profile.get("prefer_same_project_only")),
        "recommended_cross_project_memories": int(profile.get("recommended_cross_project_memories") or 1),
    }


def _adapt_retrieval_policy_from_scope_learning(
    policy: dict[str, Any],
    scope_learning_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    adapted = dict(policy)
    profile = scope_learning_profile or {}
    if not profile.get("active"):
        adapted["_scope_learning_profile"] = _summarize_scope_learning_profile(profile)
        return adapted

    recommended = max(1, int(profile.get("recommended_cross_project_memories") or adapted.get("max_cross_project_memories", 1)))
    adapted["max_cross_project_memories"] = recommended
    if profile.get("eager_cross_project"):
        adapted["cross_project_fallback_only_if_no_project_hits"] = False
    elif profile.get("prefer_same_project_only"):
        adapted["cross_project_fallback_only_if_no_project_hits"] = True
        adapted["max_cross_project_memories"] = min(adapted["max_cross_project_memories"], 1)
    adapted["_scope_learning_profile"] = _summarize_scope_learning_profile(profile)
    return adapted


def _extend_unique_items(
    base_items: list[dict],
    extra_items: list[dict],
    *,
    limit: int,
    exclude_project_id: str | None = None,
) -> list[dict]:
    seen_ids = {str(item.get("id")) for item in base_items if item.get("id") is not None}
    extended = list(base_items)
    for item in extra_items:
        item_id = str(item.get("id")) if item.get("id") is not None else ""
        if item_id and item_id in seen_ids:
            continue
        if exclude_project_id and str(item.get("project_id") or "") == str(exclude_project_id):
            continue
        extended.append(item)
        if item_id:
            seen_ids.add(item_id)
        if len(extended) >= limit:
            break
    return extended


def _apply_feedback_signal_filters(
    items: list[dict],
    *,
    positive_ids: set[str] | None = None,
    negative_ids: set[str] | None = None,
    role_label: str | None = None,
) -> tuple[list[dict], int]:
    positive_ids = positive_ids or set()
    negative_ids = negative_ids or set()
    kept: list[dict] = []
    removed = 0
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id in negative_ids:
            removed += 1
            continue
        enriched = dict(item)
        if item_id and item_id in positive_ids:
            enriched["_feedback_boost"] = 1
        if role_label:
            enriched["_retrieval_role"] = role_label
        kept.append(enriched)

    kept.sort(
        key=lambda item: (
            bool(item.get("_feedback_boost")),
            float(item.get("_combined_score") or item.get("avg_score") or item.get("current_confidence") or 0.0),
        ),
        reverse=True,
    )
    return kept, removed


def retrieve_for_task(
    task_description: str,
    skill_context: str | None = None,
    project_id: str | None = None,
    policy: dict | None = None,
    scope_learning_profile: dict | None = None,
) -> dict:
    resolved_policy = _adapt_retrieval_policy_from_scope_learning(
        _load_retrieval_policy(policy),
        scope_learning_profile,
    )
    top_relevant_slots = max(0, int(resolved_policy.get("top_relevant_slots", 0) or 0))
    conflict_slot = max(0, int(resolved_policy.get("conflict_slot", 0) or 0))
    failure_pattern_slot = max(0, int(resolved_policy.get("failure_pattern_slot", 0) or 0))
    pattern_slot = max(0, int(resolved_policy.get("pattern_slot", 0) or 0))
    feedback_signal_slot = max(0, int(resolved_policy.get("feedback_signal_slot", 0) or 0))
    total_slots = top_relevant_slots + conflict_slot
    allow_cross_project = bool(resolved_policy.get("allow_cross_project_fallback", True))
    max_cross_project = max(0, int(resolved_policy.get("max_cross_project_memories", 1) or 0))
    eager_cross_project = (
        project_id is not None
        and allow_cross_project
        and not bool(resolved_policy.get("cross_project_fallback_only_if_no_project_hits", True))
    )
    warnings: list[str] = []
    project_scope = "global"

    relevant: list[dict] = []
    retrieval_mode = "semantic"
    active_patterns: list[dict] = []
    feedback_signals: dict[str, Any] = {
        "signals": [],
        "positive_experience_ids": [],
        "negative_experience_ids": [],
        "positive_pattern_ids": [],
        "negative_pattern_ids": [],
        "summary": {"signals_n": 0},
    }
    keywords = _extract_keywords(task_description)
    same_project_relevant_found = False

    if project_id and resolved_policy.get("project_first", True):
        relevant = semantic_search(
            query_text=task_description,
            skill_name=skill_context,
            project_id=project_id,
            min_confidence=resolved_policy["min_confidence"],
            min_similarity=resolved_policy["min_semantic_score"],
            limit=total_slots,
        )
        retrieval_mode = "semantic_project"
        project_scope = "same_project"
        same_project_relevant_found = bool(relevant)

        if not relevant:
            relevant = search_by_keywords(
                keywords=keywords,
                skill_name=skill_context,
                project_id=project_id,
                min_confidence=resolved_policy["min_confidence"],
                limit=total_slots,
            )
            if relevant:
                retrieval_mode = "keywords_fallback_project"
                same_project_relevant_found = True

    if relevant and eager_cross_project and len(relevant) < min(total_slots, max_cross_project + len(relevant)):
        supplement_limit = max(0, min(total_slots, len(relevant) + max_cross_project) - len(relevant))
        if supplement_limit > 0:
            cross_candidates = semantic_search(
                query_text=task_description,
                skill_name=skill_context,
                min_confidence=resolved_policy["min_confidence"],
                min_similarity=resolved_policy["min_semantic_score"],
                limit=max_cross_project,
            )
            supplemented = _extend_unique_items(
                relevant,
                cross_candidates,
                limit=len(relevant) + supplement_limit,
                exclude_project_id=project_id,
            )
            if len(supplemented) > len(relevant):
                relevant = supplemented
                retrieval_mode = f"{retrieval_mode}_plus_cross_project"
                project_scope = "cross_project_fallback"
                warnings.append("Historical retrieval learning supplemented same-project memory with cross-project evidence.")

    if not relevant and (not project_id or allow_cross_project):
        cross_limit = total_slots if not project_id else min(total_slots, max_cross_project)
        relevant = semantic_search(
            query_text=task_description,
            skill_name=skill_context,
            min_confidence=resolved_policy["min_confidence"],
            min_similarity=resolved_policy["min_semantic_score"],
            limit=cross_limit,
        )
        if project_id:
            retrieval_mode = "semantic_cross_project_fallback" if relevant else retrieval_mode
            if relevant:
                project_scope = "cross_project_fallback"
                warnings.append("Project-first retrieval fell back to cross-project semantic memory.")
        else:
            retrieval_mode = "semantic"

    if not relevant:
        keyword_limit = total_slots if not project_id else min(total_slots, max_cross_project)
        relevant = search_by_keywords(
            keywords=keywords,
            skill_name=skill_context,
            min_confidence=resolved_policy["min_confidence"],
            limit=keyword_limit,
        )
        if relevant:
            retrieval_mode = "keywords_cross_project_fallback" if project_id else "keywords_fallback"
            if project_id:
                project_scope = "cross_project_fallback"
                warnings.append("Project-first retrieval fell back to cross-project keyword memory.")

    failure_project_id = project_id if same_project_relevant_found and project_id else None
    failure_patterns = []
    if failure_pattern_slot > 0:
        failure_patterns = get_failure_patterns(
            query_text=task_description,
            project_id=failure_project_id,
            min_confidence=resolved_policy["min_confidence"],
            limit=failure_pattern_slot,
        )
    if (
        failure_patterns
        and eager_cross_project
        and len(failure_patterns) < failure_pattern_slot
        and max_cross_project > 0
    ):
        cross_failures = get_failure_patterns(
            query_text=task_description,
            min_confidence=resolved_policy["min_confidence"],
            limit=max_cross_project,
        )
        supplemented_failures = _extend_unique_items(
            failure_patterns,
            cross_failures,
            limit=min(failure_pattern_slot, len(failure_patterns) + max_cross_project),
            exclude_project_id=project_id,
        )
        if len(supplemented_failures) > len(failure_patterns):
            failure_patterns = supplemented_failures
            project_scope = "cross_project_fallback"
            warnings.append("Historical retrieval learning supplemented same-project failure patterns with cross-project evidence.")
    if not failure_patterns and project_id and allow_cross_project and failure_pattern_slot > 0:
        failure_patterns = get_failure_patterns(
            query_text=task_description,
            min_confidence=resolved_policy["min_confidence"],
            limit=min(failure_pattern_slot, max_cross_project),
        )
        if failure_patterns and failure_project_id:
            warnings.append("Failure patterns required cross-project fallback.")

    conflict_cases = [entry for entry in relevant if entry.get("conflict_refs")][:conflict_slot]
    if pattern_slot > 0:
        active_patterns = get_active_patterns(
            query_text=task_description,
            project_id=project_id if same_project_relevant_found else project_id,
            skill_name=skill_context,
            min_confidence=resolved_policy["min_confidence"],
            limit=pattern_slot,
        )
    if not active_patterns and project_id and allow_cross_project and pattern_slot > 0:
        active_patterns = get_active_patterns(
            query_text=task_description,
            project_id=None,
            skill_name=skill_context,
            min_confidence=resolved_policy["min_confidence"],
            limit=pattern_slot,
        )
        if active_patterns:
            warnings.append("Project-first retrieval fell back to cross-project active patterns.")

    if feedback_signal_slot > 0:
        feedback_signals = get_recent_feedback_signals(
            query_text=task_description,
            project_id=project_id,
            skill_name=skill_context,
            limit=feedback_signal_slot,
        )
    positive_experience_ids = set(str(item) for item in feedback_signals.get("positive_experience_ids", []))
    negative_experience_ids = set(str(item) for item in feedback_signals.get("negative_experience_ids", []))
    positive_pattern_ids = set(str(item) for item in feedback_signals.get("positive_pattern_ids", []))
    negative_pattern_ids = set(str(item) for item in feedback_signals.get("negative_pattern_ids", []))

    relevant, removed_relevant = _apply_feedback_signal_filters(
        relevant,
        positive_ids=positive_experience_ids,
        negative_ids=negative_experience_ids,
        role_label="experience",
    )
    failure_patterns, removed_failures = _apply_feedback_signal_filters(
        failure_patterns,
        positive_ids=positive_experience_ids,
        negative_ids=negative_experience_ids,
        role_label="failure_pattern",
    )
    conflict_cases, removed_conflicts = _apply_feedback_signal_filters(
        conflict_cases,
        positive_ids=positive_experience_ids,
        negative_ids=negative_experience_ids,
        role_label="conflict_case",
    )
    active_patterns, removed_patterns = _apply_feedback_signal_filters(
        active_patterns,
        positive_ids=positive_pattern_ids,
        negative_ids=negative_pattern_ids,
        role_label="active_pattern",
    )
    feedback_filter_count = removed_relevant + removed_failures + removed_conflicts + removed_patterns
    if feedback_filter_count:
        warnings.append(
            f"Human feedback filtered {feedback_filter_count} retrieved item(s) from recent runs."
        )
    if feedback_signals.get("summary", {}).get("signals_n"):
        warnings.append(
            "Human feedback active: "
            f"{feedback_signals['summary']['signals_n']} recent signal(s) "
            f"({feedback_signals['summary'].get('positive_n', 0)} positive, "
            f"{feedback_signals['summary'].get('negative_n', 0)} negative)."
        )

    relevant, relevant_governance = apply_memory_governor(
        relevant,
        item_kind="experience",
        policy=resolved_policy,
        active_project_id=project_id,
    )
    failure_patterns, failure_governance = apply_memory_governor(
        failure_patterns,
        item_kind="failure_pattern",
        policy=resolved_policy,
        active_project_id=project_id,
        preserve_at_least=0 if int(resolved_policy.get("failure_pattern_slot", 0) or 0) <= 0 else None,
    )
    conflict_cases, conflict_governance = apply_memory_governor(
        conflict_cases,
        item_kind="conflict_case",
        policy=resolved_policy,
        active_project_id=project_id,
        preserve_at_least=0 if int(resolved_policy.get("conflict_slot", 0) or 0) <= 0 else None,
    )
    active_patterns, pattern_governance = apply_memory_governor(
        active_patterns,
        item_kind="active_pattern",
        policy=resolved_policy,
        active_project_id=project_id,
        preserve_at_least=0 if int(resolved_policy.get("pattern_slot", 0) or 0) <= 0 else None,
    )
    memory_governance = summarize_governor_sections(
        {
            "experiences": relevant_governance,
            "failure_patterns": failure_governance,
            "conflict_cases": conflict_governance,
            "active_patterns": pattern_governance,
        }
    )
    for section in memory_governance["sections"].values():
        warnings.extend(section.get("warnings", []))

    used_ids = {entry["id"] for entry in relevant}
    experiences = [
        entry
        for entry in relevant
        if entry["id"] not in {conflict["id"] for conflict in conflict_cases}
    ][:top_relevant_slots]
    failure_patterns = [entry for entry in failure_patterns if entry["id"] not in used_ids]
    experiences, failure_patterns, conflict_cases, active_patterns, token_count, budget_warnings = _apply_token_budget(
        experiences,
        failure_patterns,
        conflict_cases,
        active_patterns,
        budget=int(resolved_policy.get("max_token_budget") or 0),
    )
    warnings.extend(budget_warnings)
    warnings.extend(summarize_freshness_warnings(experiences, label="Retrieved experiences"))
    warnings.extend(summarize_freshness_warnings(failure_patterns, label="Failure patterns"))
    warnings.extend(summarize_freshness_warnings(conflict_cases, label="Conflict cases"))
    warnings.extend(summarize_freshness_warnings(active_patterns, label="Active patterns"))

    total_retrieved = len(experiences) + len(failure_patterns) + len(conflict_cases) + len(active_patterns)
    return {
        "experiences": experiences,
        "failure_patterns": failure_patterns,
        "conflict_cases": conflict_cases,
        "active_patterns": active_patterns,
        "feedback_signals": feedback_signals,
        "policy_applied": resolved_policy,
        "retrieval_mode": retrieval_mode,
        "task_query": task_description,
        "total_retrieved": total_retrieved,
        "tokens_used_estimate": token_count,
        "project_scope": project_scope,
        "warnings": warnings,
        "scope_learning_profile": resolved_policy.get("_scope_learning_profile"),
        "memory_governance": memory_governance,
    }


def record_retrieval_run(
    session_id: str,
    project_id: str | None,
    skill_name: str,
    input_context: str,
    retrieval_result: dict,
    decision_taken: str,
    final_confidence: float | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    experiences = retrieval_result.get("experiences", [])
    failures = retrieval_result.get("failure_patterns", [])
    conflicts = retrieval_result.get("conflict_cases", [])
    patterns = retrieval_result.get("active_patterns", [])
    all_items = experiences + failures + conflicts + patterns
    failure_ids = {str(item.get("id")) for item in failures if item.get("id")}
    conflict_ids = {str(item.get("id")) for item in conflicts if item.get("id")}
    pattern_ids = {str(item.get("id")) for item in patterns if item.get("id")}
    scores = [
        {
            "id": str(item.get("id")),
            "role": (
                "active_pattern"
                if str(item.get("id")) in pattern_ids
                else "failure_pattern"
                if str(item.get("id")) in failure_ids
                else "conflict_case"
                if str(item.get("id")) in conflict_ids
                else "experience"
            ),
            "similarity": item.get("_similarity"),
            "combined_score": item.get("_combined_score"),
            "freshness_score": item.get("_freshness_score"),
            "freshness_state": item.get("_freshness_state"),
            "memory_value_score": item.get("_memory_value_score"),
            "contamination_risk": item.get("_contamination_risk"),
            "memory_state": item.get("_memory_governor_state"),
            "memory_governor_score": item.get("_memory_governor_score"),
            "category": item.get("category"),
        }
        for item in all_items
    ]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.retrieval_runs (
                    id, session_id, skill_name, input_context,
                    experiences_retrieved, patterns_retrieved,
                    retrieval_scores, retrieval_policy_used,
                    final_confidence, decision_taken, project_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s
                )
                RETURNING id
                """,
                (
                    run_id,
                    session_id,
                    skill_name,
                    input_context,
                    _unique_id_list(experiences),
                    _unique_id_list(patterns),
                    json.dumps(scores),
                    json.dumps(retrieval_result.get("policy_applied", {})),
                    final_confidence,
                    decision_taken,
                    project_id,
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else run_id


def finalize_retrieval_run(
    retrieval_run_id: str,
    outcome_status: str,
    outcome_description: str | None = None,
    final_confidence: float | None = None,
    failure_reason: str | None = None,
    user_feedback: int | None = None,
) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.retrieval_runs
                SET outcome_status = %s,
                    outcome_description = %s,
                    final_confidence = COALESCE(%s, final_confidence),
                    failure_reason = %s,
                    user_feedback = %s
                WHERE id = %s
                """,
                (
                    outcome_status,
                    outcome_description,
                    final_confidence,
                    failure_reason,
                    user_feedback,
                    retrieval_run_id,
                ),
            )
            return getattr(cur, "rowcount", 1) > 0


def compute_and_store_missing_embeddings() -> int:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, title, content, task_description
                FROM core_brain.experiences
                WHERE embedding IS NULL
                LIMIT 500
                """
            )
            rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        return 0

    count = 0
    with get_db() as conn:
        with get_cursor(conn) as cur:
            for row in rows:
                try:
                    content = _normalize_json_field(row.get("content"), {})
                    text = f"{row['title']} | {row.get('task_description') or ''}"
                    if isinstance(content, dict) and content.get("conclusion"):
                        text += f" | {content['conclusion']}"
                    raw_embedding = embed(text)
                    embedding_value = _embedding_to_sql(raw_embedding)
                    cur.execute(
                        "UPDATE core_brain.experiences SET embedding = %s WHERE id = %s",
                        (embedding_value, row["id"]),
                    )
                    count += 1
                except Exception as exc:
                    warnings.warn(
                        f"Skipping embedding backfill for experience {row['id']}: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
    return count


def get_graph_connections(
    experience_id: str,
    depth: int = 1,
    _visited: set | None = None,
) -> list[dict]:
    """Expand the legal knowledge graph from an experience node.

    Returns related experiences connected via ``core_brain.legal_graph``
    up to *depth* hops.  Only ``smart_summary`` (not full content) is
    returned to minimise token usage.
    """
    if _visited is None:
        _visited = {experience_id}
    if depth <= 0:
        return []

    results: list[dict] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    e.id, e.title, e.smart_summary,
                    lg.relation_type, lg.context_note, 'outgoing' AS direction
                FROM core_brain.legal_graph lg
                JOIN core_brain.experiences e ON e.id = lg.target_id
                WHERE lg.source_id = %s
                  AND e.superseded_by IS NULL
                UNION ALL
                SELECT
                    e.id, e.title, e.smart_summary,
                    lg.relation_type, lg.context_note, 'incoming' AS direction
                FROM core_brain.legal_graph lg
                JOIN core_brain.experiences e ON e.id = lg.source_id
                WHERE lg.target_id = %s
                  AND e.superseded_by IS NULL
                """,
                (experience_id, experience_id),
            )
            for row in cur.fetchall():
                rid = str(row["id"])
                if rid in _visited:
                    continue
                _visited.add(rid)
                results.append(
                    {
                        "id": rid,
                        "title": row["title"],
                        "smart_summary": row["smart_summary"] or "",
                        "relation_type": row["relation_type"],
                        "context_note": row["context_note"] or "",
                        "direction": row["direction"],
                    }
                )
    # Recursive expansion
    if depth > 1:
        for item in list(results):
            children = get_graph_connections(item["id"], depth - 1, _visited)
            results.extend(children)
    return results


def consulta_legal_expandida(
    pregunta: str,
    max_semantic: int = 5,
    graph_depth: int = 2,
    min_confidence: float = 0.30,
    min_similarity: float = 0.25,
) -> dict:
    """High-level legal query that combines semantic search with graph expansion.

    1. Performs a semantic search over legal experiences.
    2. Expands each hit through ``legal_graph`` to pull in related laws,
       articles, and rulings.
    3. Returns only ``smart_summary`` for each node to keep token budgets low.

    Returns a dict with ``resultados_directos`` and ``grafo_expandido``.
    """
    hits = semantic_search(
        query_text=pregunta,
        category="regulatory_rule",
        skill_name="legal-chile-expert",
        min_confidence=min_confidence,
        min_similarity=min_similarity,
        limit=max_semantic,
    )

    directos: list[dict] = []
    expandidos: list[dict] = []
    visited: set[str] = set()

    for hit in hits:
        hid = str(hit["id"])
        visited.add(hid)
        directos.append(
            {
                "id": hid,
                "title": hit["title"],
                "smart_summary": hit.get("smart_summary") or str(hit.get("content", {}).get("conclusion", "")),
                "similarity": hit.get("_similarity", 0),
                "confidence": hit.get("current_confidence", 0),
            }
        )

    for directo in directos:
        connections = get_graph_connections(directo["id"], depth=graph_depth, _visited=visited)
        expandidos.extend(connections)

    return {
        "pregunta": pregunta,
        "resultados_directos": directos,
        "grafo_expandido": expandidos,
        "total_directos": len(directos),
        "total_expandidos": len(expandidos),
        "total_nodos": len(directos) + len(expandidos),
    }


if __name__ == "__main__":
    context = retrieve_for_task("connect python to postgres on windows")
    print(context["retrieval_mode"])
    print(context["total_retrieved"])
