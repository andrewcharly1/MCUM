"""Task-aware graph query planning for MCUM project context."""

from __future__ import annotations

from typing import Any


_INTENT_TERMS: dict[str, tuple[str, ...]] = {
    "design": ("ui", "ux", "frontend", "design", "diseño", "pantalla", "vista", "css", "flutter"),
    "validate": ("test", "prueba", "validar", "validation", "qa", "criterio"),
    "impact": ("impacto", "afecta", "dependencia", "migrar", "refactor", "cambiar"),
    "change": ("corregir", "implementar", "crear", "mejorar", "editar", "fix", "build"),
    "understand": ("analizar", "entender", "arquitectura", "explicar", "revisar", "flujo"),
    "locate": ("donde", "ubicar", "buscar", "archivo", "simbolo", "funcion"),
}


def build_context_query_plan(
    task_description: str,
    *,
    task_brief: dict[str, Any] | None = None,
    agent_role: str = "coordinator",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    brief = dict(task_brief or {})
    resolved_policy = dict(policy or {})
    source = " ".join(
        str(value or "")
        for value in (
            task_description,
            brief.get("objective"),
            brief.get("expected_deliverable"),
            brief.get("success_criteria"),
            " ".join(str(item) for item in brief.get("sources_to_review") or []),
        )
    ).lower()
    matched: list[str] = []
    for intent, terms in _INTENT_TERMS.items():
        if any(term in source for term in terms):
            matched.append(intent)
    primary = matched[0] if matched else "understand"
    if agent_role != "coordinator":
        primary = "delegate"

    profile = str(brief.get("execution_profile") or "lite")
    default_budgets = {"fast": 500, "lite": 900, "full": 1400, "deep": 2200}
    token_budget = int(
        brief.get("context_token_budget")
        or resolved_policy.get("token_budgets", {}).get(agent_role)
        or default_budgets.get(profile, 900)
    )
    depth = 1
    if primary in {"impact", "understand"}:
        depth = 2
    if profile == "deep":
        depth = 3

    matched_set = set(matched)
    entity_types = [
        "code_symbol",
        "code_file",
        "external_symbol",
        "experience",
        "playbook",
        "pattern",
        "skill",
        "spec_contract",
    ]
    if "design" in matched or primary == "design":
        entity_types.append("design_system")
    return {
        "primary_intent": primary,
        "matched_intents": matched,
        "query": source.strip(),
        "depth": depth,
        "entity_types": entity_types,
        "token_budget": max(300, token_budget),
        "project_first": True,
        "allow_cross_project": False,
        "include_design_system": "design" in matched or primary == "design",
        "include_specs": bool(matched_set & {"change", "validate", "impact"}) or primary == "delegate",
        "include_failures": bool(matched_set & {"change", "validate", "impact"}) or primary == "delegate",
    }
