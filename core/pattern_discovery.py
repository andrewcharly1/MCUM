"""Governed semantic discovery for MCUM operational patterns."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
import re
from typing import Any, Callable

from ..db import pattern_store
from ..policy import load_pattern_policy


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "para", "como", "cuando", "desde", "sobre", "entre", "esta", "este", "esto",
    "that", "with", "from", "when", "this", "into", "using", "task", "completed",
    "mcum", "agent", "skill", "project", "proyecto", "tarea", "sistema",
}


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _experience_text(experience: dict[str, Any]) -> str:
    content = _json_value(experience.get("content"), {})
    applicability = _json_value(experience.get("applicability"), {})
    parts = [
        str(experience.get("title") or ""),
        str(content.get("conclusion") or ""),
        str(content.get("reasoning") or "")[:500],
        str(experience.get("task_description") or ""),
        json.dumps(applicability, ensure_ascii=False, default=str)[:300],
    ]
    return " | ".join(part.strip() for part in parts if part and part.strip())


def _source_hash(text: str) -> str:
    return sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _normalize_vector(vector: list[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm < 1e-10:
        return [0.0 for _ in values]
    return [value / norm for value in values]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError(f"Embedding dimension mismatch: {len(left)} != {len(right)}")
    return sum(a * b for a, b in zip(left, right))


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dimension = len(vectors[0])
    totals = [0.0] * dimension
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError("All embeddings in a cluster must have the same dimension.")
        for index, value in enumerate(vector):
            totals[index] += value
    return _normalize_vector([value / len(vectors) for value in totals])


def _connected_components(vectors: list[list[float]], threshold: float) -> list[list[int]]:
    adjacency: dict[int, set[int]] = defaultdict(set)
    for left_index in range(len(vectors)):
        for right_index in range(left_index + 1, len(vectors)):
            if _cosine(vectors[left_index], vectors[right_index]) >= threshold:
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)

    seen: set[int] = set()
    components: list[list[int]] = []
    for start in range(len(vectors)):
        if start in seen:
            continue
        stack = [start]
        component: list[int] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(adjacency[current] - seen)
        components.append(component)
    return components


def _candidate_label(category: str, skill_name: str, experiences: list[dict[str, Any]]) -> str:
    counter: Counter[str] = Counter()
    for experience in experiences:
        for token in _TOKEN_RE.findall(_experience_text(experience).lower()):
            if token not in _STOPWORDS and not token.isdigit():
                counter[token] += 1
    common = [token for token, count in counter.most_common(4) if count >= 2]
    topic = " ".join(common[:3]) or category.replace("_", " ")
    return f"{skill_name}: {topic}"[:180]


def _context_key(experience: dict[str, Any]) -> str:
    return f"{experience.get('project_id') or 'none'}:{str(experience.get('task_description') or '').strip().lower()}"


def build_pattern_candidates(
    *,
    experiences: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
    policy: dict[str, Any],
    project_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    clustering = dict(policy.get("clustering") or {})
    gates = dict(policy.get("quality_gates") or {})
    pair_threshold = float(clustering.get("pair_similarity_threshold", 0.82) or 0.82)
    centroid_threshold = float(clustering.get("centroid_similarity_threshold", 0.78) or 0.78)
    max_group_size = max(3, int(clustering.get("max_group_size", 250) or 250))
    max_cluster_size = max(3, int(clustering.get("max_cluster_size", 30) or 30))
    min_support = max(3, int(gates.get("min_support", 3) or 3))
    min_context_diversity = max(2, int(gates.get("min_context_diversity", 2) or 2))
    min_projects_global = max(1, int(gates.get("min_distinct_projects_global", 2) or 2))
    min_skills_global = max(1, int(gates.get("min_distinct_skills_global", 2) or 2))
    min_contexts_global = max(
        min_context_diversity,
        int(gates.get("min_distinct_contexts_global", 3) or 3),
    )
    project_mode = str(gates.get("project_diversity_mode", "projects_only") or "projects_only")
    min_cohesion = float(gates.get("min_cohesion", 0.80) or 0.80)
    min_avg_confidence = float(gates.get("min_avg_confidence", 0.75) or 0.75)
    max_conflicts = max(0, int(gates.get("max_open_conflicts", 0) or 0))
    review_score = float(gates.get("review_score", 0.78) or 0.78)
    scope_type = "project" if project_id else "skill"

    grouped: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for experience in experiences:
        experience_id = str(experience.get("id") or "")
        if experience_id not in embeddings:
            continue
        key = (
            str(experience.get("category") or ""),
            str(experience.get("skill_name") or ""),
            str(experience.get("project_id")) if project_id else None,
        )
        grouped[key].append(experience)

    candidates: list[dict[str, Any]] = []
    skipped_large_groups: list[dict[str, Any]] = []
    for (category, skill_name, scope_project_id), group in grouped.items():
        if len(group) < min_support:
            continue
        if len(group) > max_group_size:
            skipped_large_groups.append(
                {"category": category, "skill_name": skill_name, "size": len(group)}
            )
            continue

        vectors = [_normalize_vector(embeddings[str(item["id"])]) for item in group]
        for component in _connected_components(vectors, pair_threshold):
            if len(component) < min_support:
                continue
            component_vectors = [vectors[index] for index in component]
            provisional_centroid = _centroid(component_vectors)
            ranked = sorted(
                (
                    (_cosine(vectors[index], provisional_centroid), index)
                    for index in component
                ),
                reverse=True,
            )[:max_cluster_size]
            refined_indices = [
                index for similarity, index in ranked if similarity >= centroid_threshold
            ]
            if len(refined_indices) < min_support:
                continue

            member_vectors = [vectors[index] for index in refined_indices]
            centroid = _centroid(member_vectors)
            similarities = [_cosine(vector, centroid) for vector in member_vectors]
            members = [group[index] for index in refined_indices]
            member_ids = [str(item["id"]) for item in members]
            seed_member = min(
                members,
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    str(item.get("id") or ""),
                ),
            )
            seed_id = str(seed_member["id"])
            distinct_projects = {
                str(item.get("project_id")) for item in members if item.get("project_id")
            }
            context_diversity = len({_context_key(item) for item in members})
            contradiction_count = sum(
                1
                for item in members
                if float(item.get("contradiction_penalty") or 0.0) > 0.0
                or bool(item.get("conflict_refs"))
            )
            cohesion = round(sum(similarities) / len(similarities), 4)
            avg_confidence = round(
                sum(float(item.get("current_confidence") or 0.0) for item in members)
                / len(members),
                4,
            )
            distinct_skills = {
                str(item.get("skill_name"))
                for item in members
                if item.get("skill_name")
            }
            projects_pass = len(distinct_projects) >= min_projects_global
            skills_pass = len(distinct_skills) >= min_skills_global
            contexts_pass = context_diversity >= min_contexts_global
            if project_id:
                project_gate = True
            elif project_mode == "projects_or_skills":
                project_gate = projects_pass or skills_pass
            elif project_mode == "projects_or_contexts":
                project_gate = projects_pass or contexts_pass
            elif project_mode in {"either", "projects_or_skills_or_contexts"}:
                project_gate = projects_pass or skills_pass or contexts_pass
            else:
                project_gate = projects_pass
            diversity_source = (
                "explicit_scope"
                if project_id
                else (
                    "projects"
                    if projects_pass
                    else (
                        "skills"
                        if project_mode in {"projects_or_skills", "either", "projects_or_skills_or_contexts"}
                        and skills_pass
                        else (
                            "contexts"
                            if project_mode in {"projects_or_contexts", "either", "projects_or_skills_or_contexts"}
                            and contexts_pass
                            else "none"
                        )
                    )
                )
            )
            quality_ready = (
                len(members) >= min_support
                and context_diversity >= min_context_diversity
                and project_gate
                and cohesion >= min_cohesion
                and avg_confidence >= min_avg_confidence
                and contradiction_count <= max_conflicts
            )
            support_score = min(1.0, len(members) / max(min_support * 2, 1))
            diversity_score = min(1.0, context_diversity / max(min_context_diversity * 2, 1))
            quality_score = round(
                (cohesion * 0.45)
                + (avg_confidence * 0.25)
                + (support_score * 0.15)
                + (diversity_score * 0.15),
                4,
            )
            candidate_key = sha256(
                f"{category}|{skill_name}|{scope_type}|{scope_project_id or ''}|{seed_id}|"
                f"{clustering.get('algorithm_version', 'semantic-components-v2')}".encode("utf-8")
            ).hexdigest()
            evidence = []
            for member, similarity in zip(members, similarities):
                has_conflict = (
                    float(member.get("contradiction_penalty") or 0.0) > 0.0
                    or bool(member.get("conflict_refs"))
                )
                evidence.append(
                    {
                        "experience_id": str(member["id"]),
                        "evidence_role": "contradict" if has_conflict else "support",
                        "similarity": round(similarity, 4),
                        "weight": round(float(member.get("current_confidence") or 0.0), 4),
                        "metadata": {"context_key": _context_key(member)},
                    }
                )

            label = _candidate_label(category, skill_name, members)
            candidates.append(
                {
                    "candidate_key": candidate_key,
                    "category": category,
                    "skill_name": skill_name,
                    "scope_type": scope_type,
                    "scope_project_id": scope_project_id,
                    "label": label,
                    "summary": (
                        f"Observed semantic cluster for {skill_name}/{category}: "
                        f"{len(members)} experiences, cohesion {cohesion:.2f}, "
                        f"context diversity {context_diversity}."
                    ),
                    "status": "review" if quality_ready and quality_score >= review_score else "shadow",
                    "support_count": len(members),
                    "distinct_project_count": len(distinct_projects),
                    "context_diversity": context_diversity,
                    "cohesion_score": cohesion,
                    "contradiction_count": contradiction_count,
                    "avg_confidence": avg_confidence,
                    "quality_score": quality_score,
                    "quality_ready": quality_ready,
                    "seed_experience_id": seed_id,
                    "embedding_model": str((policy.get("embedding") or {}).get("model_name") or ""),
                    "algorithm_version": str(
                        clustering.get("algorithm_version") or "semantic-components-v2"
                    ),
                    "metadata": {
                        "member_ids": member_ids,
                        "quality_gates": {
                            "support": len(members) >= min_support,
                            "context_diversity": context_diversity >= min_context_diversity,
                            "project_diversity": project_gate,
                            "project_diversity_source": diversity_source,
                            "project_diversity_mode": project_mode,
                            "cohesion": cohesion >= min_cohesion,
                            "avg_confidence": avg_confidence >= min_avg_confidence,
                            "open_conflicts": contradiction_count <= max_conflicts,
                        },
                    },
                    "evidence": evidence,
                    "centroid_embedding": centroid,
                }
            )

    candidates.sort(
        key=lambda item: (
            bool(item["quality_ready"]),
            float(item["quality_score"]),
            int(item["support_count"]),
        ),
        reverse=True,
    )
    return candidates, {
        "groups_analyzed": len(grouped),
        "groups_skipped_too_large": skipped_large_groups,
    }


def _encode_semantic_texts(
    texts: list[str],
    *,
    model_name: str,
    batch_size: int,
) -> list[list[float]]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    vectors = model.encode(texts, normalize_embeddings=True, batch_size=batch_size)
    return [vector.tolist() for vector in vectors]


def run_pattern_discovery(
    *,
    project_id: str | None = None,
    policy: dict[str, Any] | None = None,
    write_candidates: bool = True,
    encode_texts: Callable[..., list[list[float]]] | None = None,
) -> dict[str, Any]:
    resolved = dict(policy or load_pattern_policy())
    embedding_policy = dict(resolved.get("embedding") or {})
    eligibility = dict(resolved.get("eligibility") or {})
    lifecycle = dict(resolved.get("lifecycle") or {})
    clustering = dict(resolved.get("clustering") or {})
    model_name = str(embedding_policy.get("model_name") or "all-MiniLM-L6-v2")
    backend = str(embedding_policy.get("backend") or "sentence-transformers").lower()
    run_id: str | None = None
    scope_type = "project" if project_id else "global"
    metrics = {
        "experiences_scanned": 0,
        "embeddings_generated": 0,
        "embeddings_reused": 0,
        "groups_analyzed": 0,
        "candidates_observed": 0,
        "candidates_review_ready": 0,
    }

    if write_candidates:
        run_id = pattern_store.start_discovery_run(
            scope_type=scope_type,
            project_id=project_id,
            mode=str(resolved.get("mode") or "shadow"),
            policy_version=str(resolved.get("version") or "2.0.0"),
            algorithm_version=str(clustering.get("algorithm_version") or "semantic-components-v2"),
            embedding_model=model_name,
        )

    try:
        if bool(embedding_policy.get("require_semantic_model", True)) and backend not in {
            "sentence-transformers",
            "sentence",
            "st",
        }:
            findings = {
                "reason": "semantic_model_required",
                "configured_backend": backend,
                "auto_promote": False,
            }
            if run_id:
                pattern_store.finish_discovery_run(
                    run_id, status="blocked", metrics=metrics, findings=findings
                )
            return {"status": "blocked", "discovery_run_id": run_id, **metrics, **findings}

        experiences = pattern_store.fetch_eligible_experiences(
            included_categories=list(eligibility.get("included_categories") or []),
            excluded_categories=list(eligibility.get("excluded_categories") or ["regulatory_rule"]),
            min_confidence=float(eligibility.get("min_confidence", 0.55) or 0.55),
            exclude_synthetic=bool(eligibility.get("exclude_synthetic", True)),
            project_id=project_id,
            limit=int(eligibility.get("max_experiences_per_run", 1000) or 1000),
        )
        metrics["experiences_scanned"] = len(experiences)
        source_rows = {
            str(experience["id"]): {
                "text": _experience_text(experience),
            }
            for experience in experiences
        }
        for row in source_rows.values():
            row["source_hash"] = _source_hash(str(row["text"]))

        cached = pattern_store.get_cached_experience_embeddings(
            experience_ids=list(source_rows),
            model_name=model_name,
        )
        embeddings: dict[str, list[float]] = {}
        missing_ids: list[str] = []
        for experience_id, row in source_rows.items():
            cache_row = cached.get(experience_id)
            if cache_row and str(cache_row.get("source_hash") or "") == row["source_hash"]:
                embeddings[experience_id] = _normalize_vector(
                    _json_value(cache_row.get("embedding"), [])
                )
                metrics["embeddings_reused"] += 1
            else:
                missing_ids.append(experience_id)

        if missing_ids:
            encoder = encode_texts or _encode_semantic_texts
            generated = encoder(
                [str(source_rows[experience_id]["text"]) for experience_id in missing_ids],
                model_name=model_name,
                batch_size=max(1, int(embedding_policy.get("batch_size", 32) or 32)),
            )
            if len(generated) != len(missing_ids):
                raise RuntimeError("Semantic encoder returned an unexpected number of embeddings.")
            cache_updates = []
            for experience_id, vector in zip(missing_ids, generated):
                normalized = _normalize_vector(vector)
                expected_dimension = int(embedding_policy.get("dimension", 384) or 384)
                if len(normalized) != expected_dimension:
                    raise ValueError(
                        f"Pattern embedding dimension must be {expected_dimension}, got {len(normalized)}."
                    )
                embeddings[experience_id] = normalized
                cache_updates.append(
                    {
                        "experience_id": experience_id,
                        "source_hash": source_rows[experience_id]["source_hash"],
                        "embedding": normalized,
                    }
                )
            if write_candidates:
                pattern_store.upsert_experience_embeddings(rows=cache_updates, model_name=model_name)
            metrics["embeddings_generated"] = len(cache_updates)

        candidates, discovery_findings = build_pattern_candidates(
            experiences=experiences,
            embeddings=embeddings,
            policy=resolved,
            project_id=project_id,
        )
        metrics["groups_analyzed"] = int(discovery_findings.get("groups_analyzed") or 0)
        metrics["candidates_observed"] = len(candidates)
        metrics["candidates_review_ready"] = sum(
            1 for candidate in candidates if candidate.get("quality_ready")
        )

        persisted: list[str] = []
        if write_candidates and run_id:
            for candidate in candidates:
                persisted.append(
                    pattern_store.upsert_pattern_candidate(
                        candidate=candidate,
                        evidence=list(candidate.get("evidence") or []),
                        centroid_embedding=list(candidate.get("centroid_embedding") or []),
                        discovery_run_id=run_id,
                    )
                )
            expired = pattern_store.expire_unseen_candidates(
                ttl_days=int(lifecycle.get("candidate_ttl_days", 90) or 90)
            )
        else:
            expired = 0

        findings = {
            **discovery_findings,
            "persisted_candidate_ids": persisted,
            "expired_candidates": expired,
            "auto_promote": False,
            "regulatory_rules_excluded": "regulatory_rule"
            in set(eligibility.get("excluded_categories") or []),
        }
        if run_id:
            pattern_store.finish_discovery_run(
                run_id, status="success", metrics=metrics, findings=findings
            )
        public_candidates = [
            {
                key: value
                for key, value in candidate.items()
                if key not in {"evidence", "centroid_embedding"}
            }
            for candidate in candidates[:20]
        ]
        return {
            "status": "success",
            "mode": str(resolved.get("mode") or "shadow"),
            "discovery_run_id": run_id,
            **metrics,
            "candidates": public_candidates,
            "findings": findings,
        }
    except Exception as exc:
        if run_id:
            pattern_store.finish_discovery_run(
                run_id,
                status="failure",
                metrics=metrics,
                findings={"auto_promote": False},
                error_message=str(exc),
            )
        return {
            "status": "failure",
            "discovery_run_id": run_id,
            "error": str(exc),
            **metrics,
            "auto_promote": False,
        }


def auto_promote_ready_candidates(
    *,
    project_id: str | None = None,
    policy: dict[str, Any] | None = None,
    reviewed_by: str = "mcum-auto-promote",
    max_promotions: int = 5,
) -> dict[str, Any]:
    """Governed automatic promotion of review-ready pattern candidates.

    Only runs when ``pattern_policy.auto_promote`` is true. For each
    review-ready candidate it materializes a draft and then re-checks every
    quality gate through :func:`activate_pattern`. Candidates that fail any
    gate stay as drafts and are reported in ``rejected`` -- they are never
    force-activated. This preserves the strict quality contract while removing
    the manual confirmation click.
    """
    resolved = dict(policy or load_pattern_policy())
    if not bool(resolved.get("auto_promote", False)):
        return {"status": "disabled", "auto_promote": False, "promoted": [], "rejected": []}

    quality_gates = dict(resolved.get("quality_gates") or {})
    lifecycle = dict(resolved.get("lifecycle") or {})
    max_age_days = int(lifecycle.get("candidate_ttl_days", 90) or 90)
    limit = max(1, int(max_promotions or 1))

    candidates = pattern_store.list_review_ready_candidates(
        project_id=project_id, limit=limit, max_age_days=max_age_days
    )
    promoted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("id"))
        label = candidate.get("label")
        try:
            draft = pattern_store.materialize_candidate_to_draft(
                candidate_id=candidate_id,
                reviewed_by=reviewed_by,
                review_notes="Auto-promoted: candidate passed all quality gates.",
            )
            pattern_id = str(draft.get("pattern_id") or "")
            pattern_store.activate_pattern(
                pattern_id=pattern_id,
                reviewed_by=reviewed_by,
                quality_gates=quality_gates,
                review_notes="Auto-activated: every quality gate re-verified.",
            )
            promoted.append({"candidate_id": candidate_id, "pattern_id": pattern_id, "label": label})
        except ValueError as exc:
            # A gate failed at materialize or activate time -> leave as draft,
            # never force-activate. Report it for visibility.
            rejected.append({"candidate_id": candidate_id, "reason": str(exc), "label": label})

    return {
        "status": "success",
        "auto_promote": True,
        "reviewed": len(candidates),
        "promoted": promoted,
        "rejected": rejected,
    }
