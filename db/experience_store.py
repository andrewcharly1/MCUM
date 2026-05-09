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
    "min_semantic_score": 0.30,
    "semantic_weight": 0.60,
    "confidence_weight": 0.40,
    "diversity_enforced": True,
    "max_token_budget": 4000,
    "project_first": True,
    "allow_cross_project_fallback": True,
    "cross_project_fallback_only_if_no_project_hits": True,
    "max_cross_project_memories": 1,
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
    for field in (
        "content",
        "applicability",
        "not_applicable_cases",
        "conditions",
        "evidence_refs",
        "source_artifacts",
    ):
        if field in normalized:
            normalized[field] = _normalize_json_field(normalized[field], {})
    return normalized


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
                    evidence_refs, review_notes,
                    is_synthetic, tested_by,
                    project_id, skill_name, skill_version, task_description,
                    embedding
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
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
            return cur.rowcount > 0


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
                        e.task_description, e.conflict_refs,
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
                return results

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
                    task_description, conflict_refs, embedding,
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

    scored.sort(key=lambda item: item["_combined_score"], reverse=True)
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
                    task_description, conflict_refs, created_at, last_validated_at
                FROM core_brain.experiences
                WHERE {' AND '.join(conditions)}
                ORDER BY current_confidence DESC
                LIMIT %s
                """,
                params,
            )
            return [_normalize_experience_row(dict(row)) for row in cur.fetchall()]


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
    budget: int | None,
) -> tuple[list[dict], list[dict], list[dict], int, list[str]]:
    if budget is None or budget <= 0:
        total_tokens = sum(_estimate_retrieval_item_tokens(item) for item in (experiences + failure_patterns + conflict_cases))
        return experiences, failure_patterns, conflict_cases, total_tokens, []

    selected = {
        "experiences": [],
        "failure_patterns": [],
        "conflict_cases": [],
    }
    skipped = 0
    total_tokens = 0
    warnings: list[str] = []

    for group_name, items in (
        ("experiences", experiences),
        ("failure_patterns", failure_patterns),
        ("conflict_cases", conflict_cases),
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
        total_tokens,
        warnings,
    )


def retrieve_for_task(
    task_description: str,
    skill_context: str | None = None,
    project_id: str | None = None,
    policy: dict | None = None,
) -> dict:
    resolved_policy = _load_retrieval_policy(policy)
    total_slots = resolved_policy["top_relevant_slots"] + resolved_policy["conflict_slot"]
    allow_cross_project = bool(resolved_policy.get("allow_cross_project_fallback", True))
    max_cross_project = max(1, int(resolved_policy.get("max_cross_project_memories", 1)))
    warnings: list[str] = []
    project_scope = "global"

    relevant: list[dict] = []
    retrieval_mode = "semantic"
    keywords = _extract_keywords(task_description)

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

    failure_project_id = project_id if project_scope == "same_project" and project_id else None
    failure_patterns = get_failure_patterns(
        query_text=task_description,
        project_id=failure_project_id,
        min_confidence=resolved_policy["min_confidence"],
        limit=resolved_policy["failure_pattern_slot"],
    )
    if not failure_patterns and project_id and allow_cross_project:
        failure_patterns = get_failure_patterns(
            query_text=task_description,
            min_confidence=resolved_policy["min_confidence"],
            limit=min(resolved_policy["failure_pattern_slot"], max_cross_project),
        )
        if failure_patterns and failure_project_id:
            warnings.append("Failure patterns required cross-project fallback.")

    used_ids = {entry["id"] for entry in relevant}
    conflict_cases = [entry for entry in relevant if entry.get("conflict_refs")][: resolved_policy["conflict_slot"]]
    experiences = [
        entry
        for entry in relevant
        if entry["id"] not in {conflict["id"] for conflict in conflict_cases}
    ][: resolved_policy["top_relevant_slots"]]
    failure_patterns = [entry for entry in failure_patterns if entry["id"] not in used_ids]
    experiences, failure_patterns, conflict_cases, token_count, budget_warnings = _apply_token_budget(
        experiences,
        failure_patterns,
        conflict_cases,
        budget=int(resolved_policy.get("max_token_budget") or 0),
    )
    warnings.extend(budget_warnings)

    total_retrieved = len(experiences) + len(failure_patterns) + len(conflict_cases)
    return {
        "experiences": experiences,
        "failure_patterns": failure_patterns,
        "conflict_cases": conflict_cases,
        "policy_applied": resolved_policy,
        "retrieval_mode": retrieval_mode,
        "task_query": task_description,
        "total_retrieved": total_retrieved,
        "tokens_used_estimate": token_count,
        "project_scope": project_scope,
        "warnings": warnings,
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
    all_items = experiences + failures + conflicts
    scores = [
        {
            "id": str(item.get("id")),
            "similarity": item.get("_similarity"),
            "combined_score": item.get("_combined_score"),
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
                    [item["id"] for item in experiences],
                    [item["id"] for item in failures + conflicts],
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
            return cur.rowcount > 0


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


if __name__ == "__main__":
    context = retrieve_for_task("connect python to postgres on windows")
    print(context["retrieval_mode"])
    print(context["total_retrieved"])
