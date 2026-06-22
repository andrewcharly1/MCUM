"""
Cost-aware model routing for supervised MCUM work.

The router does not call external model APIs. It produces an auditable
recommendation that the coordinator/agent runner can follow when assigning
work to humans, Codex subagents, Claude Code, OpenCode or other clients.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_MODEL_ROUTER_POLICY: dict[str, Any] = {
    "enabled": True,
    "strategy": "cost_aware",
    "models": {
        "architect": "gpt-5.5",
        "coder": "gpt-5.3-codex",
        "fast": "gpt-5.4-mini",
    },
    "relative_cost_weights": {
        "gpt-5.5": 3.0,
        "gpt-5.3-codex": 1.0,
        "gpt-5.4-mini": 0.35,
    },
    "deep_model_complexity_threshold": 0.78,
    "deep_model_risk_levels": ["alto", "high", "critico", "critical"],
    "mini_model_max_complexity": 0.46,
    "role_profiles": {
        "coordinator": {
            "agent_profile": "orchestrator-master",
            "default_model_key": "coder",
            "deep_model_key": "architect",
            "fast_model_key": "fast",
            "reasoning_effort": "high",
            "token_budget": {"context_in": 1800, "output": 650},
            "responsibilities": [
                "define_plan",
                "select_skill",
                "merge_results",
                "approve_memory_writeback",
            ],
        },
        "context_analyst": {
            "agent_profile": "context-retriever",
            "default_model_key": "fast",
            "deep_model_key": "coder",
            "reasoning_effort": "low",
            "token_budget": {"context_in": 900, "output": 260},
            "responsibilities": ["bootstrap", "retrieve_context", "summarize_evidence"],
        },
        "planner": {
            "agent_profile": "planner",
            "default_model_key": "coder",
            "fast_model_key": "fast",
            "deep_model_key": "architect",
            "reasoning_effort": "medium",
            "token_budget": {"context_in": 1200, "output": 420},
            "responsibilities": ["plan_steps", "define_scope", "surface_risks"],
        },
        "strategist": {
            "agent_profile": "planner",
            "default_model_key": "coder",
            "deep_model_key": "architect",
            "reasoning_effort": "medium",
            "token_budget": {"context_in": 1300, "output": 460},
            "responsibilities": ["strategy", "tradeoffs", "sequencing"],
        },
        "implementer": {
            "agent_profile": "builder-codex",
            "default_model_key": "coder",
            "deep_model_key": "architect",
            "reasoning_effort": "medium",
            "token_budget": {"context_in": 1500, "output": 700},
            "responsibilities": ["edit_files", "run_commands", "produce_patch"],
        },
        "validator": {
            "agent_profile": "validator-qa",
            "default_model_key": "coder",
            "fast_model_key": "fast",
            "reasoning_effort": "medium",
            "token_budget": {"context_in": 1100, "output": 360},
            "responsibilities": ["run_tests", "check_regressions", "verify_result"],
        },
        "auditor": {
            "agent_profile": "validator-qa",
            "default_model_key": "fast",
            "deep_model_key": "coder",
            "reasoning_effort": "low",
            "token_budget": {"context_in": 900, "output": 320},
            "responsibilities": ["review_coverage", "find_gaps", "report_risks"],
        },
        "memory_governor": {
            "agent_profile": "memory-governor",
            "default_model_key": "fast",
            "deep_model_key": "coder",
            "reasoning_effort": "low",
            "token_budget": {"context_in": 700, "output": 260},
            "responsibilities": ["decide_save_discard", "avoid_memory_pollution"],
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_model_router_policy(execution_policy: dict[str, Any] | None) -> dict[str, Any]:
    return _deep_merge(
        DEFAULT_MODEL_ROUTER_POLICY,
        (execution_policy or {}).get("model_router") or {},
    )


def _risk_level(task_brief: dict[str, Any]) -> str:
    return str(task_brief.get("risk_level") or "medio").strip().lower()


def _is_deep_task(task_brief: dict[str, Any], complexity_score: float, policy: dict[str, Any]) -> bool:
    if str(task_brief.get("force_model") or "").strip():
        return False
    threshold = float(policy.get("deep_model_complexity_threshold", 0.78) or 0.78)
    risk_levels = {str(item).lower() for item in policy.get("deep_model_risk_levels", [])}
    text = " ".join(
        str(task_brief.get(key) or "")
        for key in ("objective", "expected_deliverable", "success_criteria", "validation_required")
    ).lower()
    critical_terms = (
        "arquitectura",
        "multitenant",
        "seguridad",
        "security",
        "saas",
        "schema",
        "migracion",
        "riesgo",
        "produccion",
    )
    return (
        complexity_score >= threshold
        or _risk_level(task_brief) in risk_levels
        or any(term in text for term in critical_terms)
    )


def _is_fast_enough(task_brief: dict[str, Any], role: str, complexity_score: float, policy: dict[str, Any]) -> bool:
    if str(task_brief.get("force_model") or "").strip():
        return False
    if _risk_level(task_brief) in {"alto", "high", "critico", "critical"}:
        return False
    max_complexity = float(policy.get("mini_model_max_complexity", 0.46) or 0.46)
    return complexity_score <= max_complexity and role in {"context_analyst", "auditor", "validator", "coordinator", "planner"}


def _model_from_key(policy: dict[str, Any], key: str) -> str:
    models = dict(policy.get("models") or {})
    return str(models.get(key) or key)


def _role_profile(policy: dict[str, Any], role: str, mode: str) -> dict[str, Any]:
    profiles = dict(policy.get("role_profiles") or {})
    if role in profiles:
        return deepcopy(profiles[role])
    if mode == "write":
        return deepcopy(profiles.get("implementer") or {})
    return deepcopy(profiles.get("context_analyst") or {})


def _relative_cost(model: str, total_tokens: int, policy: dict[str, Any]) -> float:
    weights = dict(policy.get("relative_cost_weights") or {})
    weight = float(weights.get(model, 1.0) or 1.0)
    return round((max(total_tokens, 1) / 1000.0) * weight, 3)


def route_model_for_worker(
    worker: dict[str, Any],
    task_brief: dict[str, Any],
    *,
    complexity_score: float,
    execution_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    policy = normalize_model_router_policy(execution_policy)
    role = str(worker.get("role") or task_brief.get("worker_role") or "worker")
    mode = str(worker.get("mode") or "read_only")
    profile = _role_profile(policy, role, mode)
    forced_model = str(task_brief.get("force_model") or "").strip()

    if forced_model:
        model = forced_model
        decision = "forced"
    elif _is_deep_task(task_brief, complexity_score, policy) and profile.get("deep_model_key"):
        model = _model_from_key(policy, str(profile.get("deep_model_key")))
        decision = "deep_reasoning"
    elif _is_fast_enough(task_brief, role, complexity_score, policy) and profile.get("fast_model_key"):
        model = _model_from_key(policy, str(profile.get("fast_model_key")))
        decision = "fast_low_cost"
    else:
        model = _model_from_key(policy, str(profile.get("default_model_key") or "coder"))
        decision = "balanced_default"

    budget = dict(profile.get("token_budget") or {})
    context_in = int(budget.get("context_in") or 1000)
    output = int(budget.get("output") or 350)
    total = context_in + output
    baseline_model = _model_from_key(policy, "architect")
    return {
        "agent_profile": profile.get("agent_profile") or role,
        "recommended_model": model,
        "decision": decision,
        "reasoning_effort": profile.get("reasoning_effort") or "medium",
        "token_budget": {
            "context_in": context_in,
            "output": output,
            "total": total,
        },
        "relative_cost_units": _relative_cost(model, total, policy),
        "baseline_deep_cost_units": _relative_cost(baseline_model, total, policy),
        "responsibilities": list(profile.get("responsibilities") or []),
        "rationale": [
            f"role={role}",
            f"mode={mode}",
            f"complexity={complexity_score:.2f}",
            f"risk={_risk_level(task_brief)}",
        ],
    }


def route_model_for_coordinator(
    task_brief: dict[str, Any],
    *,
    complexity_score: float,
    execution_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    return route_model_for_worker(
        {"role": "coordinator", "mode": "coordinate"},
        task_brief,
        complexity_score=complexity_score,
        execution_policy=execution_policy,
    )


def summarize_model_routes(routes: list[dict[str, Any]]) -> dict[str, Any]:
    actual = round(sum(float(route.get("relative_cost_units") or 0.0) for route in routes), 3)
    baseline = round(sum(float(route.get("baseline_deep_cost_units") or 0.0) for route in routes), 3)
    savings_ratio = round(max(0.0, 1.0 - (actual / baseline)), 3) if baseline else 0.0
    return {
        "estimated_relative_cost_units": actual,
        "estimated_all_deep_cost_units": baseline,
        "estimated_savings_ratio": savings_ratio,
        "models": sorted({str(route.get("recommended_model")) for route in routes if route.get("recommended_model")}),
    }
