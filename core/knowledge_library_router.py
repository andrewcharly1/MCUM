"""Task-aware routing for the governed knowledge library."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

try:
    from .knowledge_library_taxonomy import (
        build_methodology_conflict_profile,
        build_methodology_lenses,
        score_concept_matches,
        score_methodology_matches,
    )
except ImportError:
    module_path = Path(__file__).resolve().with_name("knowledge_library_taxonomy.py")
    spec = importlib.util.spec_from_file_location(
        "mcum_live_knowledge_library_taxonomy",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    build_methodology_conflict_profile = module.build_methodology_conflict_profile
    build_methodology_lenses = module.build_methodology_lenses
    score_concept_matches = module.score_concept_matches
    score_methodology_matches = module.score_methodology_matches


def route_task_to_knowledge_library(
    task_description: str,
    *,
    task_brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a lightweight route plan from task text to methodology sources."""

    task_brief = dict(task_brief or {})
    source_text = " ".join(
        str(part or "")
        for part in [
            task_description,
            task_brief.get("objective"),
            task_brief.get("expected_deliverable"),
            task_brief.get("success_criteria"),
            " ".join(str(item) for item in (task_brief.get("sources_to_review") or [])),
        ]
        if str(part or "").strip()
    )
    methodology_matches = score_methodology_matches(source_text)
    methodology_scores = {
        slug: float(match.get("score") or 0.0)
        for slug, match in methodology_matches.items()
    }
    matched_terms = {
        slug: list(match.get("matched_terms") or [])
        for slug, match in methodology_matches.items()
    }
    preferred_repositories: list[str] = []
    expansions: list[str] = []
    per_methodology_expansions: dict[str, list[str]] = {}
    methodology_queries: dict[str, dict[str, Any]] = {}

    base_task = task_description.strip()
    if base_task:
        expansions.append(base_task)
    objective = str(task_brief.get("objective") or "").strip()
    if objective and objective not in expansions:
        expansions.append(objective)

    ranked_methodologies = sorted(
        methodology_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    top_methodologies = [name for name, _score in ranked_methodologies[:2]]
    concept_matches = score_concept_matches(source_text, methodology_slugs=top_methodologies)
    ranked_concepts = sorted(
        concept_matches.items(),
        key=lambda item: item[1].get("score") or 0.0,
        reverse=True,
    )
    preferred_concepts = [slug for slug, _match in ranked_concepts[:6]]
    methodology_lenses = build_methodology_lenses(top_methodologies)
    conflict_profile = build_methodology_conflict_profile(
        methodology_scores,
        top_methodologies=top_methodologies,
        concept_scores={
            slug: float(match.get("score") or 0.0)
            for slug, match in ranked_concepts[:10]
        },
    )

    for methodology in top_methodologies:
        config = methodology_matches.get(methodology) or {}
        for repo in config.get("repositories") or []:
            if repo not in preferred_repositories:
                preferred_repositories.append(repo)
        focused_terms: list[str] = []
        for term in list(matched_terms.get(methodology) or []) + list(config.get("expansion_terms") or []):
            cleaned = str(term or "").strip()
            if cleaned and cleaned not in focused_terms:
                focused_terms.append(cleaned)
        focused_query = " ".join(focused_terms[:6]).strip()
        methodology_expansions: list[str] = []
        if focused_query:
            methodology_expansions.append(focused_query)
        for term in config.get("expansion_terms") or []:
            cleaned = str(term or "").strip()
            if cleaned and cleaned not in methodology_expansions:
                methodology_expansions.append(cleaned)
        per_methodology_expansions[methodology] = methodology_expansions
        methodology_queries[methodology] = {
            "queries": methodology_expansions[:4],
            "repositories": list(config.get("repositories") or []),
        }

    for methodology in top_methodologies:
        for query in per_methodology_expansions.get(methodology) or []:
            if query and query not in expansions:
                expansions.append(query)
            if len(expansions) >= 4:
                break
        if len(expansions) >= 4:
            break

    if len(expansions) < 5:
        max_rounds = max((len(items) for items in per_methodology_expansions.values()), default=0)
        for round_index in range(max_rounds):
            for methodology in top_methodologies:
                options = per_methodology_expansions.get(methodology) or []
                if round_index >= len(options):
                    continue
                query = options[round_index]
                if query and query not in expansions:
                    expansions.append(query)
                if len(expansions) >= 5:
                    break
            if len(expansions) >= 5:
                break

    return {
        "task_text": source_text,
        "methodology_scores": methodology_scores,
        "matched_terms": matched_terms,
        "top_methodologies": top_methodologies,
        "preferred_repositories": preferred_repositories,
        "preferred_concepts": preferred_concepts,
        "concept_scores": {
            slug: float(match.get("score") or 0.0)
            for slug, match in ranked_concepts[:10]
        },
        "concept_matches": {
            slug: {
                "matched_terms": list(match.get("matched_terms") or []),
                "methodology_slug": match.get("methodology_slug"),
                "concept_type": match.get("concept_type"),
            }
            for slug, match in ranked_concepts[:10]
        },
        "methodology_lenses": methodology_lenses,
        "methodology_queries": methodology_queries,
        "conflict_profile": conflict_profile,
        "expanded_queries": [query for query in expansions if query][:5],
    }
