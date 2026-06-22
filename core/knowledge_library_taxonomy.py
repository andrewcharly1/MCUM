"""Shared taxonomy for governed methodology routing and indexing."""

from __future__ import annotations

import re
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+./:-]*", re.IGNORECASE)


TAXONOMY_REGISTRY: dict[str, dict[str, Any]] = {
    "pmbok": {
        "name": "PMBOK / Project Management",
        "description": "Project management guidance focused on value delivery, governance, stakeholders, and tailoring.",
        "authority_tier": "canonical",
        "repositories": ["LOCAL_PDFS"],
        "routing_terms": [
            "pmbok",
            "pmo",
            "project management",
            "project",
            "stakeholder",
            "stakeholders",
            "sponsor",
            "charter",
            "governance",
            "risk",
            "schedule",
            "scope",
            "cost",
            "quality",
            "tailoring",
            "communications",
            "value",
            "delivery",
            "procurement",
        ],
        "expansion_terms": ["project management", "stakeholder engagement", "value delivery", "governance"],
        "lenses": [
            "Optimize for value delivery and stakeholder outcomes.",
            "Prefer tailoring over rigid process application.",
            "Call out governance, risk, and dependency management explicitly.",
        ],
        "concepts": [
            {
                "slug": "stakeholder-engagement",
                "name": "Stakeholder Engagement",
                "concept_type": "principle",
                "aliases": ["stakeholder engagement", "stakeholders", "stakeholder", "engagement strategy"],
                "description": "Continuous engagement of stakeholders to protect alignment and delivery outcomes.",
            },
            {
                "slug": "value-delivery",
                "name": "Value Delivery",
                "concept_type": "principle",
                "aliases": ["value delivery", "deliver value", "outcomes", "benefits realization"],
                "description": "Project work should be optimized for sustained value delivery.",
            },
            {
                "slug": "project-governance",
                "name": "Project Governance",
                "concept_type": "artifact",
                "aliases": ["governance", "project governance", "decision rights", "oversight"],
                "description": "Structures and decision mechanisms that guide project execution.",
            },
            {
                "slug": "tailoring",
                "name": "Tailoring",
                "concept_type": "pattern",
                "aliases": ["tailoring", "tailor", "adapt methodology", "fit for context"],
                "description": "Adapt the management approach to the work, context, and constraints.",
            },
            {
                "slug": "risk-management",
                "name": "Risk Management",
                "concept_type": "pattern",
                "aliases": ["risk management", "risk", "uncertainty", "issue log"],
                "description": "Proactive management of uncertainty, exposure, and response planning.",
            },
        ],
    },
    "ddd": {
        "name": "Domain-Driven Design",
        "description": "Strategic and tactical domain modeling focused on boundaries, language, and business complexity.",
        "authority_tier": "primary",
        "repositories": ["sap-ddd-knowledgebase"],
        "routing_terms": [
            "domain",
            "bounded",
            "context",
            "contexts",
            "ubiquitous",
            "language",
            "aggregate",
            "aggregates",
            "entity",
            "entities",
            "value object",
            "value objects",
            "domain event",
            "domain events",
            "context mapping",
            "subdomain",
            "subdomains",
        ],
        "expansion_terms": ["bounded contexts", "domain model", "ubiquitous language", "context mapping"],
        "lenses": [
            "Protect domain boundaries before discussing implementation details.",
            "Use ubiquitous language and context mapping to reduce ambiguity.",
            "Model aggregates and events around business invariants, not tables.",
        ],
        "concepts": [
            {
                "slug": "bounded-context",
                "name": "Bounded Context",
                "concept_type": "domain",
                "aliases": ["bounded context", "bounded contexts", "context boundary", "service boundary"],
                "description": "A clear linguistic and model boundary where terms keep a single meaning.",
            },
            {
                "slug": "ubiquitous-language",
                "name": "Ubiquitous Language",
                "concept_type": "principle",
                "aliases": ["ubiquitous language", "shared language", "common vocabulary"],
                "description": "A shared domain language used by both domain experts and implementers.",
            },
            {
                "slug": "aggregate",
                "name": "Aggregate",
                "concept_type": "pattern",
                "aliases": ["aggregate", "aggregates", "aggregate root"],
                "description": "Consistency boundary used to protect invariants in the domain model.",
            },
            {
                "slug": "domain-event",
                "name": "Domain Event",
                "concept_type": "artifact",
                "aliases": ["domain event", "domain events", "business event"],
                "description": "Event that represents a meaningful occurrence in the domain.",
            },
            {
                "slug": "context-mapping",
                "name": "Context Mapping",
                "concept_type": "pattern",
                "aliases": ["context mapping", "context map", "upstream downstream", "relationship mapping"],
                "description": "Explicit modeling of relationships between bounded contexts.",
            },
        ],
    },
    "team_topologies": {
        "name": "Team Topologies",
        "description": "Socio-technical design of teams, interfaces, and interaction modes.",
        "authority_tier": "primary",
        "repositories": ["team-topologies-community-materials"],
        "routing_terms": [
            "team topology",
            "team topologies",
            "stream aligned",
            "platform team",
            "enabling team",
            "complicated subsystem",
            "cognitive load",
            "interaction mode",
            "team api",
            "ownership",
        ],
        "expansion_terms": ["team topologies", "cognitive load", "stream aligned", "interaction modes"],
        "lenses": [
            "Optimize ownership and flow before adding coordination layers.",
            "Keep cognitive load explicit when proposing team structures.",
            "Choose interaction modes deliberately instead of defaulting to collaboration everywhere.",
        ],
        "concepts": [
            {
                "slug": "stream-aligned-team",
                "name": "Stream-Aligned Team",
                "concept_type": "role",
                "aliases": ["stream aligned", "stream-aligned team", "stream aligned team"],
                "description": "Team aligned to a stream of change and customer value.",
            },
            {
                "slug": "platform-team",
                "name": "Platform Team",
                "concept_type": "role",
                "aliases": ["platform team", "internal platform"],
                "description": "Team that provides internal services to reduce friction for stream-aligned teams.",
            },
            {
                "slug": "enabling-team",
                "name": "Enabling Team",
                "concept_type": "role",
                "aliases": ["enabling team", "enablement team"],
                "description": "Team that helps others adopt capabilities and reduce bottlenecks.",
            },
            {
                "slug": "cognitive-load",
                "name": "Cognitive Load",
                "concept_type": "metric",
                "aliases": ["cognitive load", "team cognitive load"],
                "description": "The mental processing burden a team must carry to deliver effectively.",
            },
            {
                "slug": "interaction-mode",
                "name": "Interaction Mode",
                "concept_type": "pattern",
                "aliases": ["interaction mode", "collaboration", "x-as-a-service", "facilitating"],
                "description": "The mode by which teams work together over time.",
            },
        ],
    },
    "devops": {
        "name": "DevOps",
        "description": "Delivery, operations, automation, and reliability practices across the software lifecycle.",
        "authority_tier": "primary",
        "repositories": ["devops-roadmap"],
        "routing_terms": [
            "devops",
            "ci/cd",
            "pipeline",
            "deployment",
            "deploy",
            "observability",
            "monitoring",
            "iac",
            "infrastructure",
            "docker",
            "kubernetes",
            "incident",
            "sre",
            "devsecops",
            "release",
        ],
        "expansion_terms": ["ci cd", "deployment pipeline", "observability", "infrastructure as code"],
        "lenses": [
            "Favor flow efficiency, automation, and safe delivery paths.",
            "Keep observability and operational readiness close to deployment design.",
            "Treat security and reliability as part of the delivery system, not add-ons.",
        ],
        "concepts": [
            {
                "slug": "ci-cd",
                "name": "CI/CD",
                "concept_type": "tool",
                "aliases": ["ci cd", "ci/cd", "continuous integration", "continuous delivery"],
                "description": "Automation pipeline for integration, validation, and release flow.",
            },
            {
                "slug": "deployment-pipeline",
                "name": "Deployment Pipeline",
                "concept_type": "pattern",
                "aliases": ["deployment pipeline", "release pipeline", "pipeline"],
                "description": "Structured path from code change to deployment and validation.",
            },
            {
                "slug": "observability",
                "name": "Observability",
                "concept_type": "tool",
                "aliases": ["observability", "telemetry", "monitoring", "tracing"],
                "description": "Operational visibility through telemetry, logs, metrics, and traces.",
            },
            {
                "slug": "infrastructure-as-code",
                "name": "Infrastructure as Code",
                "concept_type": "tool",
                "aliases": ["infrastructure as code", "iac", "terraform", "configuration as code"],
                "description": "Manage infrastructure declaratively through versioned code.",
            },
            {
                "slug": "incident-response",
                "name": "Incident Response",
                "concept_type": "pattern",
                "aliases": ["incident", "incident response", "postmortem", "sre"],
                "description": "Operational response and learning cycle around failures and incidents.",
            },
        ],
    },
    "software_design": {
        "name": "Software Design",
        "description": "Design heuristics for managing complexity, modularity, and abstraction depth.",
        "authority_tier": "primary",
        "repositories": ["alysivji-notes"],
        "routing_terms": [
            "design",
            "modularity",
            "complexity",
            "abstraction",
            "interface",
            "coupling",
            "refactor",
            "maintainability",
            "deep module",
            "shallow module",
            "software design",
        ],
        "expansion_terms": ["software design", "modularity", "complexity management", "deep modules"],
        "lenses": [
            "Prefer deeper modules and simpler interfaces over surface-level decomposition.",
            "Treat complexity as a first-class design cost.",
            "Use abstractions to hide detail, not to rename detail.",
        ],
        "concepts": [
            {
                "slug": "modularity",
                "name": "Modularity",
                "concept_type": "principle",
                "aliases": ["modularity", "module boundary", "module design"],
                "description": "Separation of responsibilities that reduces change coupling.",
            },
            {
                "slug": "abstraction",
                "name": "Abstraction",
                "concept_type": "principle",
                "aliases": ["abstraction", "abstractions", "hide complexity"],
                "description": "Expose essential behavior while hiding unnecessary detail.",
            },
            {
                "slug": "coupling",
                "name": "Coupling",
                "concept_type": "metric",
                "aliases": ["coupling", "tight coupling", "dependency coupling"],
                "description": "The degree to which components depend on one another.",
            },
            {
                "slug": "deep-module",
                "name": "Deep Module",
                "concept_type": "pattern",
                "aliases": ["deep module", "deep modules"],
                "description": "Module with a simple interface and substantial hidden functionality.",
            },
            {
                "slug": "complexity-management",
                "name": "Complexity Management",
                "concept_type": "topic",
                "aliases": ["complexity", "complexity management", "manage complexity"],
                "description": "Approach to reducing accidental complexity in software systems.",
            },
        ],
    },
}


CONFLICT_MATRIX: dict[frozenset[str], dict[str, Any]] = {
    frozenset({"ddd", "pmbok"}): {
        "label": "domain-boundaries-vs-governance",
        "summary": "DDD optimizes for boundary clarity and model integrity, while PMBOK optimizes for governance and delivery alignment.",
        "comparison_axes": [
            {
                "axis": "Primary optimization",
                "ddd": "Boundary clarity, language consistency, and model integrity.",
                "pmbok": "Governance, stakeholder alignment, and value delivery control.",
            },
            {
                "axis": "Failure mode",
                "ddd": "Blurry domain boundaries and leaky models.",
                "pmbok": "Weak governance, poor stakeholder engagement, and unmanaged risk.",
            },
        ],
        "guidance": [
            "Preserve bounded contexts first, then wrap them with governance and delivery controls.",
            "Do not force governance artifacts to redefine the domain model.",
        ],
    },
    frozenset({"ddd", "team_topologies"}): {
        "label": "domain-boundaries-vs-team-boundaries",
        "summary": "DDD designs software boundaries; Team Topologies designs ownership and interaction boundaries.",
        "comparison_axes": [
            {
                "axis": "Boundary type",
                "ddd": "Domain and model boundaries.",
                "team_topologies": "Ownership, interaction, and cognitive-load boundaries.",
            },
            {
                "axis": "Failure mode",
                "ddd": "Context leakage and ambiguous language.",
                "team_topologies": "Unclear ownership and overloaded teams.",
            },
        ],
        "guidance": [
            "Align team ownership to bounded contexts when possible, but do not assume they are identical.",
            "Use cognitive load as a check on domain decomposition.",
        ],
    },
    frozenset({"pmbok", "devops"}): {
        "label": "governance-vs-flow-optimization",
        "summary": "PMBOK emphasizes governance and structured control, while DevOps emphasizes flow, automation, and operational feedback.",
        "comparison_axes": [
            {
                "axis": "Primary optimization",
                "pmbok": "Governance, oversight, and value control.",
                "devops": "Flow efficiency, automation, and fast feedback.",
            },
            {
                "axis": "Failure mode",
                "pmbok": "Heavy control without delivery acceleration.",
                "devops": "Fast delivery without adequate governance or risk management.",
            },
        ],
        "guidance": [
            "Keep governance guardrails lightweight enough to preserve delivery flow.",
            "Tie operational telemetry back to project risk and stakeholder commitments.",
        ],
    },
}


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def tokenize_text(value: Any) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(str(value or "")) if len(token) >= 2}


def get_taxonomy_registry() -> dict[str, dict[str, Any]]:
    return TAXONOMY_REGISTRY


def iter_methodology_definitions() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for slug, config in TAXONOMY_REGISTRY.items():
        items.append(
            {
                "slug": slug,
                "name": config["name"],
                "description": config["description"],
                "authority_tier": config["authority_tier"],
                "repositories": list(config.get("repositories") or []),
                "expansion_terms": list(config.get("expansion_terms") or []),
                "routing_terms": list(config.get("routing_terms") or []),
                "lenses": list(config.get("lenses") or []),
            }
        )
    return items


def iter_concept_definitions() -> list[dict[str, Any]]:
    concepts: list[dict[str, Any]] = []
    for methodology_slug, config in TAXONOMY_REGISTRY.items():
        for concept in config.get("concepts") or []:
            aliases: list[str] = []
            for alias in [concept.get("name"), *(concept.get("aliases") or [])]:
                cleaned = normalize_text(alias)
                if cleaned and cleaned not in aliases:
                    aliases.append(cleaned)
            concepts.append(
                {
                    "methodology_slug": methodology_slug,
                    "slug": concept["slug"],
                    "name": concept["name"],
                    "concept_type": concept["concept_type"],
                    "description": concept["description"],
                    "aliases": aliases,
                    "authority_tier": config["authority_tier"],
                }
            )
    return concepts


def _phrase_hits(text: str, candidates: list[str]) -> list[str]:
    lowered = normalize_text(text)
    hits: list[str] = []
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if len(normalized) < 4 and " " not in normalized and "/" not in normalized:
            continue
        if normalized and normalized in lowered and normalized not in hits:
            hits.append(normalized)
    return hits


def score_methodology_matches(source_text: str, *, source_repository: str | None = None) -> dict[str, dict[str, Any]]:
    lowered = normalize_text(source_text)
    token_set = tokenize_text(source_text)
    repository = str(source_repository or "").strip()
    matches: dict[str, dict[str, Any]] = {}

    for methodology in iter_methodology_definitions():
        slug = methodology["slug"]
        keywords = list(methodology.get("routing_terms") or [])
        phrase_matches = _phrase_hits(lowered, keywords)
        token_matches = [
            keyword.lower()
            for keyword in keywords
            if " " not in keyword and keyword.lower() in token_set and keyword.lower() not in phrase_matches
        ]
        hits = phrase_matches + token_matches
        repo_bonus = 0.22 if repository and repository in (methodology.get("repositories") or []) else 0.0
        if not hits and repo_bonus <= 0:
            continue
        score = min(1.0, 0.15 + (len(hits) * 0.11) + repo_bonus)
        matches[slug] = {
            "score": round(score, 4),
            "matched_terms": hits[:6],
            "repositories": list(methodology.get("repositories") or []),
            "expansion_terms": list(methodology.get("expansion_terms") or []),
            "lenses": list(methodology.get("lenses") or []),
            "authority_tier": methodology.get("authority_tier"),
        }
    return matches


def score_concept_matches(
    source_text: str,
    *,
    methodology_slugs: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    lowered = normalize_text(source_text)
    token_set = tokenize_text(source_text)
    allowed = {slug for slug in methodology_slugs or [] if slug}
    matches: dict[str, dict[str, Any]] = {}

    for concept in iter_concept_definitions():
        methodology_slug = concept["methodology_slug"]
        if allowed and methodology_slug not in allowed:
            continue
        aliases = list(concept.get("aliases") or [])
        phrase_matches = _phrase_hits(lowered, aliases)
        token_matches = [
            alias
            for alias in aliases
            if " " not in alias and alias in token_set and alias not in phrase_matches
        ]
        hits = phrase_matches + token_matches
        if not hits:
            continue
        score = min(1.0, 0.18 + (len(hits) * 0.16))
        matches[concept["slug"]] = {
            "score": round(score, 4),
            "matched_terms": hits[:6],
            "methodology_slug": methodology_slug,
            "concept_type": concept["concept_type"],
            "aliases": aliases,
            "name": concept["name"],
            "description": concept["description"],
            "authority_tier": concept["authority_tier"],
        }
    return matches


def build_methodology_lenses(methodology_slugs: list[str]) -> dict[str, list[str]]:
    lenses: dict[str, list[str]] = {}
    for slug in methodology_slugs:
        config = TAXONOMY_REGISTRY.get(slug)
        if config:
            lenses[slug] = list(config.get("lenses") or [])
    return lenses


def build_methodology_conflict_profile(
    methodology_scores: dict[str, float],
    *,
    top_methodologies: list[str] | None = None,
    concept_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    ordered = list(top_methodologies or [])
    if len(ordered) < 2:
        return {
            "active": False,
            "comparison_required": False,
            "methodologies": ordered[:1],
        }

    primary, secondary = ordered[:2]
    primary_score = float(methodology_scores.get(primary) or 0.0)
    secondary_score = float(methodology_scores.get(secondary) or 0.0)
    score_gap = round(abs(primary_score - secondary_score), 4)
    minimum_score = min(primary_score, secondary_score)
    if minimum_score < 0.24 or score_gap > 0.24:
        return {
            "active": False,
            "comparison_required": False,
            "methodologies": [primary, secondary],
            "score_gap": score_gap,
        }

    pair_key = frozenset({primary, secondary})
    pair_config = dict(CONFLICT_MATRIX.get(pair_key) or {})
    if not pair_config:
        pair_config = {
            "label": f"{primary}-vs-{secondary}",
            "summary": f"Compare {primary} and {secondary} explicitly instead of merging them into a single recipe.",
            "comparison_axes": [
                {
                    "axis": "Primary optimization",
                    primary: "What this methodology optimizes first.",
                    secondary: "What this methodology optimizes first.",
                }
            ],
            "guidance": [
                f"State where {primary} leads and where {secondary} constrains or complements it.",
            ],
        }

    return {
        "active": True,
        "comparison_required": True,
        "requires_diverse_hits": True,
        "methodologies": [primary, secondary],
        "dominant_methodology": primary if primary_score >= secondary_score else secondary,
        "score_gap": score_gap,
        "minimum_score": round(minimum_score, 4),
        "label": pair_config.get("label"),
        "summary": pair_config.get("summary"),
        "comparison_axes": list(pair_config.get("comparison_axes") or []),
        "guidance": list(pair_config.get("guidance") or []),
        "concept_scores": dict(concept_scores or {}),
    }
