"""Shadow retrieval bridge for the governed knowledge_library schema."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .connection import get_cursor, get_db


ROOT = Path(__file__).resolve().parent.parent
FLAGS_FILE = ROOT / "directives" / "knowledge_library_flags.json"
POLICY_FILE = ROOT / "directives" / "knowledge_library_policy.json"


def _load_build_library_preflight():
    module_path = ROOT / "core" / "knowledge_library_preflight.py"
    spec = importlib.util.spec_from_file_location(
        "mcum_live_knowledge_library_preflight",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load knowledge library preflight from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.build_library_preflight


build_library_preflight = _load_build_library_preflight()


def _load_route_task_to_knowledge_library():
    module_path = ROOT / "core" / "knowledge_library_router.py"
    spec = importlib.util.spec_from_file_location(
        "mcum_live_knowledge_library_router",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load knowledge library router from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.route_task_to_knowledge_library


route_task_to_knowledge_library = _load_route_task_to_knowledge_library()


def _load_rank_concepts_semantically():
    try:
        from .knowledge_library_semantic import rank_concepts_semantically
    except Exception:
        return None
    return rank_concepts_semantically


rank_concepts_semantically = _load_rank_concepts_semantically()


def _candidate_limit(limit: int) -> int:
    return max(int(limit) * 5, 12)


def _authority_weight(authority_tier: Any) -> float:
    weights = {
        "canonical": 0.24,
        "primary": 0.16,
        "secondary": 0.1,
        "community": 0.05,
        "internal": 0.02,
    }
    return float(weights.get(str(authority_tier or "").strip().lower(), 0.0))


def _ensure_unique_ordered(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for item in items:
        cleaned = str(item or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(item)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _summary_query(limit: int, repositories: list[str] | None = None) -> str:
    repository_clause = ""
    if repositories:
        repository_clause = " AND COALESCE(d.source_repository, 'LOCAL_PDFS') = ANY(%s)"
    return f"""
        SELECT
            s.id::text AS summary_id,
            s.summary_level::text AS summary_level,
            s.summary_title,
            s.summary_text,
            s.token_count,
            d.id::text AS document_id,
            d.title AS document_title,
            d.source_path,
            d.source_repository,
            sec.id::text AS section_id,
            sec.heading AS section_heading,
            sec.section_path,
            sec.page_start AS section_page_start,
            sec.page_end AS section_page_end,
            c.id::text AS chunk_id,
            c.chunk_order,
            c.page_start AS chunk_page_start,
            c.page_end AS chunk_page_end,
            d.authority_tier::text AS authority_tier,
            dm_match.methodology_slug AS matched_methodology_slug,
            ts_rank_cd(
                to_tsvector('simple', COALESCE(s.summary_title, '') || ' ' || COALESCE(s.summary_text, '')),
                plainto_tsquery('simple', %s)
            ) AS lexical_rank,
            COALESCE(dm_match.methodology_score, 0) AS methodology_score,
            COALESCE(sec_match.concept_score, 0) AS section_concept_score,
            COALESCE(chunk_match.concept_score, 0) AS chunk_concept_score,
            CASE
                WHEN COALESCE(d.source_repository, 'LOCAL_PDFS') = ANY(%s) THEN 0.12
                ELSE 0
            END AS repository_bonus
        FROM knowledge_library.summaries s
        JOIN knowledge_library.document_versions dv
          ON dv.id = s.document_version_id
        JOIN knowledge_library.documents d
          ON d.id = dv.document_id
        LEFT JOIN knowledge_library.sections sec
          ON sec.id = s.section_id
        LEFT JOIN knowledge_library.chunks c
          ON c.id = s.chunk_id
        LEFT JOIN LATERAL (
            SELECT
                m.methodology_slug,
                dm.relevance_score AS methodology_score
            FROM knowledge_library.document_methodologies dm
            JOIN knowledge_library.methodologies m
              ON m.id = dm.methodology_id
            WHERE dm.document_id = d.id
              AND m.methodology_slug = ANY(%s)
            ORDER BY dm.relevance_score DESC, m.methodology_slug ASC
            LIMIT 1
        ) dm_match ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(sc.relevance_score) AS concept_score
            FROM knowledge_library.section_concepts sc
            JOIN knowledge_library.concepts concept
              ON concept.id = sc.concept_id
            WHERE sec.id IS NOT NULL
              AND sc.section_id = sec.id
              AND concept.concept_slug = ANY(%s)
        ) sec_match ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(cc.relevance_score) AS concept_score
            FROM knowledge_library.chunk_concepts cc
            JOIN knowledge_library.concepts concept
              ON concept.id = cc.concept_id
            WHERE c.id IS NOT NULL
              AND cc.chunk_id = c.id
              AND concept.concept_slug = ANY(%s)
        ) chunk_match ON TRUE
        WHERE
            dv.ingestion_status = 'completed'
            AND to_tsvector('simple', COALESCE(s.summary_title, '') || ' ' || COALESCE(s.summary_text, ''))
                @@ plainto_tsquery('simple', %s)
            {repository_clause}
        ORDER BY lexical_rank DESC, d.title ASC
        LIMIT {_candidate_limit(limit)}
    """


def _chunk_query(limit: int, repositories: list[str] | None = None) -> str:
    repository_clause = ""
    if repositories:
        repository_clause = " AND COALESCE(d.source_repository, 'LOCAL_PDFS') = ANY(%s)"
    return f"""
        SELECT
            c.id::text AS chunk_id,
            c.chunk_order,
            c.content,
            c.summary_excerpt,
            d.id::text AS document_id,
            d.title AS document_title,
            d.source_path,
            d.source_repository,
            d.authority_tier::text AS authority_tier,
            dm_match.methodology_slug AS matched_methodology_slug,
            sec.id::text AS section_id,
            sec.heading AS section_heading,
            sec.section_path,
            c.page_start,
            c.page_end,
            ts_rank_cd(
                to_tsvector('simple', COALESCE(c.content, '') || ' ' || COALESCE(c.summary_excerpt, '')),
                plainto_tsquery('simple', %s)
            ) AS lexical_rank,
            COALESCE(dm_match.methodology_score, 0) AS methodology_score,
            COALESCE(sec_match.concept_score, 0) AS section_concept_score,
            COALESCE(chunk_match.concept_score, 0) AS chunk_concept_score,
            CASE
                WHEN COALESCE(d.source_repository, 'LOCAL_PDFS') = ANY(%s) THEN 0.12
                ELSE 0
            END AS repository_bonus
        FROM knowledge_library.chunks c
        JOIN knowledge_library.document_versions dv
          ON dv.id = c.document_version_id
        JOIN knowledge_library.documents d
          ON d.id = dv.document_id
        LEFT JOIN knowledge_library.sections sec
          ON sec.id = c.section_id
        LEFT JOIN LATERAL (
            SELECT
                m.methodology_slug,
                dm.relevance_score AS methodology_score
            FROM knowledge_library.document_methodologies dm
            JOIN knowledge_library.methodologies m
              ON m.id = dm.methodology_id
            WHERE dm.document_id = d.id
              AND m.methodology_slug = ANY(%s)
            ORDER BY dm.relevance_score DESC, m.methodology_slug ASC
            LIMIT 1
        ) dm_match ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(sc.relevance_score) AS concept_score
            FROM knowledge_library.section_concepts sc
            JOIN knowledge_library.concepts concept
              ON concept.id = sc.concept_id
            WHERE sec.id IS NOT NULL
              AND sc.section_id = sec.id
              AND concept.concept_slug = ANY(%s)
        ) sec_match ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(cc.relevance_score) AS concept_score
            FROM knowledge_library.chunk_concepts cc
            JOIN knowledge_library.concepts concept
              ON concept.id = cc.concept_id
            WHERE cc.chunk_id = c.id
              AND concept.concept_slug = ANY(%s)
        ) chunk_match ON TRUE
        WHERE
            dv.ingestion_status = 'completed'
            AND to_tsvector('simple', COALESCE(c.content, '') || ' ' || COALESCE(c.summary_excerpt, ''))
                @@ plainto_tsquery('simple', %s)
            {repository_clause}
        ORDER BY lexical_rank DESC, d.title ASC
        LIMIT {_candidate_limit(limit)}
    """


def _composite_rank(row: dict[str, Any]) -> float:
    lexical_rank = float(row.get("lexical_rank") or row.get("rank") or 0.0)
    methodology_score = float(row.get("methodology_score") or 0.0)
    section_concept_score = float(row.get("section_concept_score") or 0.0)
    chunk_concept_score = float(row.get("chunk_concept_score") or 0.0)
    repository_bonus = float(row.get("repository_bonus") or 0.0)
    authority_bonus = _authority_weight(row.get("authority_tier"))
    return round(
        lexical_rank
        + (methodology_score * 0.38)
        + (max(section_concept_score, chunk_concept_score) * 0.28)
        + repository_bonus
        + authority_bonus,
        6,
    )


def _merge_semantic_route_plan(route_plan: dict[str, Any], query_text: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    semantic_matches: list[dict[str, Any]] = []
    if rank_concepts_semantically is None:
        route_plan["semantic_concepts"] = semantic_matches
        return route_plan, warnings

    try:
        semantic_matches = list(
            rank_concepts_semantically(
                query_text,
                methodology_slugs=list(route_plan.get("top_methodologies") or []),
                top_k=4,
            )
            or []
        )
    except Exception as exc:
        warnings.append(f"knowledge_library:semantic_concepts_unavailable:{type(exc).__name__}")
        route_plan["semantic_concepts"] = semantic_matches
        return route_plan, warnings

    preferred_concepts = list(route_plan.get("preferred_concepts") or [])
    preferred_methodologies = list(route_plan.get("top_methodologies") or [])
    semantic_methodology_scores = dict(route_plan.get("semantic_methodology_scores") or {})

    for match in semantic_matches:
        concept_slug = str(match.get("concept_slug") or "").strip()
        methodology_slug = str(match.get("methodology_slug") or "").strip()
        if concept_slug and concept_slug not in preferred_concepts:
            preferred_concepts.append(concept_slug)
        if methodology_slug:
            if methodology_slug not in preferred_methodologies:
                preferred_methodologies.append(methodology_slug)
            semantic_methodology_scores[methodology_slug] = max(
                float(semantic_methodology_scores.get(methodology_slug) or 0.0),
                float(match.get("semantic_score") or 0.0),
            )

    route_plan["preferred_concepts"] = preferred_concepts[:8]
    route_plan["semantic_concepts"] = semantic_matches
    route_plan["semantic_methodology_scores"] = semantic_methodology_scores
    return route_plan, warnings


def _select_diverse_rows(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    limit: int,
    required_methodologies: list[str] | None = None,
) -> list[dict[str, Any]]:
    ranked_rows = sorted(rows, key=_composite_rank, reverse=True)
    if not required_methodologies:
        return ranked_rows[:limit]

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    required = _ensure_unique_ordered(required_methodologies)
    for methodology_slug in required:
        for row in ranked_rows:
            row_id = str(row.get(id_field) or "").strip()
            matched_slug = str(row.get("matched_methodology_slug") or "").strip()
            if not row_id or row_id in selected_ids or matched_slug != methodology_slug:
                continue
            selected.append(row)
            selected_ids.add(row_id)
            break

    for row in ranked_rows:
        row_id = str(row.get(id_field) or "").strip()
        if not row_id or row_id in selected_ids:
            continue
        selected.append(row)
        selected_ids.add(row_id)
        if len(selected) >= limit:
            break

    return selected[:limit]


def _make_context_item_from_summary(row: dict[str, Any], *, rank: float, mode: str) -> dict[str, Any]:
    source_path = row.get("source_path")
    source_repository = row.get("source_repository")
    source_ref = source_path or source_repository or "knowledge_library"
    page_start = row.get("chunk_page_start") or row.get("section_page_start")
    page_end = row.get("chunk_page_end") or row.get("section_page_end")
    title_bits = [row.get("document_title"), row.get("summary_title") or row.get("section_heading")]
    title = " :: ".join(str(bit).strip() for bit in title_bits if str(bit or "").strip())
    locator = f"p{page_start}-{page_end}" if page_start and page_end else "n/a"
    summary_text = str(row.get("summary_text") or "").strip()
    context = (
        "source="
        f"{source_ref}; section={row.get('section_heading') or row.get('section_path') or 'document'}; "
        f"locator={locator}; mode={mode}"
    )
    return {
        "id": f"kl-summary:{row['summary_id']}",
        "title": title or str(row.get("document_title") or "knowledge document"),
        "category": "knowledge_library",
        "content": {
            "conclusion": summary_text,
            "context": context,
        },
        "applicability": {
            "when": f"Use when methodology guidance from {row.get('document_title')} can inform the task.",
        },
        "source_artifacts": [{"path": source_ref}],
        "evidence_refs": [{"path": source_ref, "section": row.get("section_path")}],
        "knowledge_library": {
            "document_id": row.get("document_id"),
            "document_title": row.get("document_title"),
            "summary_level": row.get("summary_level"),
            "section_id": row.get("section_id"),
            "section_heading": row.get("section_heading"),
            "chunk_id": row.get("chunk_id"),
            "matched_methodology_slug": row.get("matched_methodology_slug"),
            "page_start": page_start,
            "page_end": page_end,
            "mode": mode,
            "rank": rank,
            "lexical_rank": float(row.get("lexical_rank") or row.get("rank") or 0.0),
            "methodology_score": float(row.get("methodology_score") or 0.0),
            "section_concept_score": float(row.get("section_concept_score") or 0.0),
            "chunk_concept_score": float(row.get("chunk_concept_score") or 0.0),
            "authority_tier": row.get("authority_tier"),
        },
        "_similarity": rank,
    }


def _make_context_item_from_chunk(row: dict[str, Any], *, rank: float, mode: str) -> dict[str, Any]:
    source_path = row.get("source_path")
    source_repository = row.get("source_repository")
    source_ref = source_path or source_repository or "knowledge_library"
    page_start = row.get("page_start")
    page_end = row.get("page_end")
    locator = f"p{page_start}-{page_end}" if page_start and page_end else "n/a"
    excerpt = str(row.get("content") or row.get("summary_excerpt") or "").strip()
    title_bits = [row.get("document_title"), row.get("section_heading"), f"chunk {row.get('chunk_order')}"]
    title = " :: ".join(str(bit).strip() for bit in title_bits if str(bit or "").strip())
    return {
        "id": f"kl-chunk:{row['chunk_id']}",
        "title": title,
        "category": "knowledge_library",
        "content": {
            "conclusion": excerpt[:900],
            "context": (
                "source="
                f"{source_ref}; section={row.get('section_heading') or row.get('section_path') or 'document'}; "
                f"locator={locator}; mode={mode}"
            ),
        },
        "applicability": {
            "when": f"Use when deeper evidence from {row.get('document_title')} is needed.",
        },
        "source_artifacts": [{"path": source_ref}],
        "evidence_refs": [{"path": source_ref, "section": row.get("section_path")}],
        "knowledge_library": {
            "document_id": row.get("document_id"),
            "document_title": row.get("document_title"),
            "section_id": row.get("section_id"),
            "section_heading": row.get("section_heading"),
            "chunk_id": row.get("chunk_id"),
            "matched_methodology_slug": row.get("matched_methodology_slug"),
            "page_start": page_start,
            "page_end": page_end,
            "mode": mode,
            "rank": rank,
            "lexical_rank": float(row.get("lexical_rank") or row.get("rank") or 0.0),
            "methodology_score": float(row.get("methodology_score") or 0.0),
            "section_concept_score": float(row.get("section_concept_score") or 0.0),
            "chunk_concept_score": float(row.get("chunk_concept_score") or 0.0),
            "authority_tier": row.get("authority_tier"),
        },
        "_similarity": rank,
    }


def retrieve_knowledge_library_shadow(
    task_description: str,
    *,
    task_brief: dict[str, Any] | None = None,
    flags: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve governed references in shadow mode without writeback."""

    resolved_flags = flags or _load_json(FLAGS_FILE)
    resolved_policy = policy or _load_json(POLICY_FILE)
    plan = build_library_preflight(
        task_description=task_description,
        task_brief=task_brief or {},
        flags=resolved_flags,
        policy=resolved_policy,
    )
    if not plan.enabled or not plan.allow_read_path:
        return {
            "enabled": False,
            "shadow_mode": False,
            "applied_mode": "disabled",
            "hits": [],
            "warnings": [plan.reason],
            "tokens_used_estimate": 0,
            "metadata": {
                "plan_reason": plan.reason,
                "integration_mode": plan.integration_mode,
                "max_summary_hits": plan.max_summary_hits,
                "max_chunk_hits": plan.max_chunk_hits,
            },
        }

    retrieval_policy = dict((resolved_policy or {}).get("retrieval") or {})
    route_plan = route_task_to_knowledge_library(
        task_description,
        task_brief=task_brief or {},
    )
    route_plan, semantic_warnings = _merge_semantic_route_plan(
        dict(route_plan or {}),
        str(route_plan.get("task_text") or task_description),
    )
    requested_mode = str(retrieval_policy.get("default_mode", "summary_first"))
    applied_mode = "summary_first"
    if requested_mode in {"section_then_chunk", "chunk_only"}:
        applied_mode = requested_mode
    elif requested_mode in {"deep_read", "full_document"} and plan.full_read_allowed:
        applied_mode = requested_mode

    summary_limit = max(1, int(plan.max_summary_hits))
    chunk_limit = max(0, int(plan.max_chunk_hits))

    hits: list[dict[str, Any]] = []
    warnings: list[str] = list(semantic_warnings)
    seen_ids: set[str] = set()
    preferred_repositories = list(route_plan.get("preferred_repositories") or [])
    preferred_concepts = list(route_plan.get("preferred_concepts") or [])
    preferred_methodologies = list(route_plan.get("top_methodologies") or [])
    conflict_profile = dict(route_plan.get("conflict_profile") or {})
    conflict_methodologies = (
        list(conflict_profile.get("methodologies") or [])
        if bool(conflict_profile.get("active"))
        else []
    )
    methodology_queries = dict(route_plan.get("methodology_queries") or {})
    expanded_queries = list(route_plan.get("expanded_queries") or [task_description])
    repository_scope = preferred_repositories or ["__none__"]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            query_plans = [("targeted", preferred_repositories)] if preferred_repositories else []
            query_plans.append(("fallback", []))

            summary_candidates: list[dict[str, Any]] = []
            for query_text in expanded_queries:
                for _scope_name, repositories in query_plans:
                    params: list[Any] = [
                        query_text,
                        repository_scope,
                        preferred_methodologies,
                        preferred_concepts,
                        preferred_concepts,
                        query_text,
                    ]
                    if repositories:
                        params.append(repositories)
                    cur.execute(_summary_query(summary_limit, repositories), tuple(params))
                    summary_candidates.extend(list(cur.fetchall()))

            selected_summary_rows = _select_diverse_rows(
                summary_candidates,
                id_field="summary_id",
                limit=summary_limit,
                required_methodologies=conflict_methodologies,
            )
            if conflict_methodologies:
                summary_coverage = {
                    str(row.get("matched_methodology_slug") or "").strip()
                    for row in selected_summary_rows
                    if str(row.get("matched_methodology_slug") or "").strip()
                }
                missing_methodologies = [slug for slug in conflict_methodologies if slug not in summary_coverage]
                if missing_methodologies:
                    fallback_summary_rows: list[dict[str, Any]] = []
                    for missing_methodology in missing_methodologies:
                        method_plan = dict(methodology_queries.get(missing_methodology) or {})
                        method_queries = list(method_plan.get("queries") or [])
                        method_repositories = list(method_plan.get("repositories") or [])
                        for focused_query in method_queries[:3]:
                            params = [
                                focused_query,
                                method_repositories or repository_scope,
                                [missing_methodology],
                                preferred_concepts,
                                preferred_concepts,
                                focused_query,
                            ]
                            if method_repositories:
                                params.append(method_repositories)
                            cur.execute(_summary_query(summary_limit, method_repositories), tuple(params))
                            method_rows = list(cur.fetchall())
                            fallback_summary_rows.extend(
                                row for row in method_rows
                                if str(row.get("matched_methodology_slug") or "").strip() == missing_methodology
                            )
                            if fallback_summary_rows:
                                break
                    if fallback_summary_rows:
                        summary_candidates.extend(fallback_summary_rows)
                        selected_summary_rows = _select_diverse_rows(
                            summary_candidates,
                            id_field="summary_id",
                            limit=summary_limit,
                            required_methodologies=conflict_methodologies,
                        )
                        summary_coverage = {
                            str(row.get("matched_methodology_slug") or "").strip()
                            for row in selected_summary_rows
                            if str(row.get("matched_methodology_slug") or "").strip()
                        }
                        missing_methodologies = [
                            slug for slug in conflict_methodologies if slug not in summary_coverage
                        ]
                if missing_methodologies:
                    warnings.append(
                        "knowledge_library:conflict_comparison_incomplete:"
                        + ",".join(missing_methodologies)
                    )
            for row in selected_summary_rows:
                rank = _composite_rank(row)
                item = _make_context_item_from_summary(row, rank=rank, mode=applied_mode)
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                hits.append(item)

            if applied_mode in {"section_then_chunk", "chunk_only", "deep_read", "full_document"} and chunk_limit > 0:
                chunk_candidates: list[dict[str, Any]] = []
                for query_text in expanded_queries:
                    for _scope_name, repositories in query_plans:
                        params = [
                            query_text,
                            repository_scope,
                            preferred_methodologies,
                            preferred_concepts,
                            preferred_concepts,
                            query_text,
                        ]
                        if repositories:
                            params.append(repositories)
                        cur.execute(_chunk_query(chunk_limit, repositories), tuple(params))
                        chunk_candidates.extend(list(cur.fetchall()))

                selected_chunk_rows = _select_diverse_rows(
                    chunk_candidates,
                    id_field="chunk_id",
                    limit=chunk_limit,
                    required_methodologies=conflict_methodologies,
                )
                for row in selected_chunk_rows:
                    rank = _composite_rank(row)
                    item = _make_context_item_from_chunk(row, rank=rank, mode=applied_mode)
                    if item["id"] in seen_ids:
                        continue
                    seen_ids.add(item["id"])
                    hits.append(item)

    if not hits:
        warnings.append("knowledge_library:no_hits")

    token_budget_key = {
        "summary_first": "summary_only",
        "section_then_chunk": "section_then_chunk",
        "chunk_only": "section_then_chunk",
        "deep_read": "deep_read",
        "full_document": "full_document",
    }[applied_mode]
    token_budget = int(((retrieval_policy.get("token_budget") or {}).get(token_budget_key, 600)))
    tokens_used_estimate = sum(max(1, len(str(item.get("content", {}).get("conclusion") or "")) // 4) for item in hits)

    return {
        "enabled": True,
        "shadow_mode": plan.integration_mode == "shadow",
        "applied_mode": applied_mode,
        "hits": hits,
        "warnings": warnings,
        "tokens_used_estimate": tokens_used_estimate,
        "metadata": {
            "plan_reason": plan.reason,
            "integration_mode": plan.integration_mode,
            "requested_mode": requested_mode,
            "token_budget": token_budget,
            "route_plan": route_plan,
            "taxonomy_signal_counts": {
                "preferred_methodologies": len(preferred_methodologies),
                "preferred_concepts": len(preferred_concepts),
                "semantic_concepts": len(route_plan.get("semantic_concepts") or []),
                "conflict_active": int(bool(conflict_profile.get("active"))),
            },
            "shadow_enabled": bool((resolved_flags.get("flags") or {}).get("shadow_mode_enabled", False)),
            "mcum_orchestrator_cable_enabled": bool(
                (resolved_flags.get("flags") or {}).get("mcum_orchestrator_cable_enabled", False)
            ),
        },
    }
