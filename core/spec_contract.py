"""
Spec Contract generation for MCUM.

This module turns a task brief into an auditable behavioral contract before
execution. It is deterministic on purpose: the LLM may enrich the contract
later, but MCUM always has a safe baseline that can be persisted and traced.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_SPEC_POLICY: dict[str, Any] = {
    "enabled": True,
    "persist": True,
    "always_attach": True,
    "default_mode": "lite",
    "full_task_types": ["crear", "corregir", "mejorar", "automatizar"],
    "lite_task_types": ["analizar", "validar", "planificar"],
    "full_risk_levels": ["alto", "high", "critico", "critical"],
    "block_on_low_score": False,
    "min_score": 0.55,
    "block_on_persist_failure": True,
}


def normalize_spec_policy(execution_policy: dict[str, Any] | None) -> dict[str, Any]:
    policy = deepcopy(DEFAULT_SPEC_POLICY)
    policy.update((execution_policy or {}).get("spec_contract") or {})
    policy["enabled"] = bool(policy.get("enabled", True))
    policy["persist"] = bool(policy.get("persist", True))
    policy["always_attach"] = bool(policy.get("always_attach", True))
    return policy


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    elif isinstance(value, tuple):
        raw = list(value)
    else:
        raw = [value]
    result: list[str] = []
    for item in raw:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _clip(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def resolve_spec_mode(task_brief: dict[str, Any], policy: dict[str, Any]) -> str:
    task_type = str(task_brief.get("task_type") or "").lower()
    risk_level = str(task_brief.get("risk_level") or "").lower()
    full_task_types = {str(item).lower() for item in policy.get("full_task_types") or []}
    full_risk_levels = {str(item).lower() for item in policy.get("full_risk_levels") or []}
    if task_type in full_task_types or risk_level in full_risk_levels:
        return "full"
    lite_task_types = {str(item).lower() for item in policy.get("lite_task_types") or []}
    if task_type in lite_task_types:
        return "lite"
    return str(policy.get("default_mode") or "lite")


def _assumptions(task_brief: dict[str, Any]) -> list[dict[str, Any]]:
    sources = _as_list(task_brief.get("sources_to_review"))
    constraints = _as_list(task_brief.get("constraints"))
    assumptions = [
        {
            "code": "A-001",
            "text": "MCUM must preserve existing behavior outside the declared scope.",
            "status": "system_validated",
            "risk": "medium",
        },
        {
            "code": "A-002",
            "text": "The final result must include validation evidence before success is recorded.",
            "status": "system_validated",
            "risk": "low",
        },
    ]
    if not sources:
        assumptions.append(
            {
                "code": "A-003",
                "text": "No explicit source files were provided; MCUM infers scope from project path and task text.",
                "status": "inferred",
                "risk": "medium",
            }
        )
    if not constraints:
        assumptions.append(
            {
                "code": "A-004",
                "text": "No user constraints were provided; MCUM applies default safety and no-unrelated-change rules.",
                "status": "inferred",
                "risk": "medium",
            }
        )
    return assumptions


def _acceptance_criteria(task_brief: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    criteria = [
        {
            "code": "AC-001",
            "text": _clip(task_brief.get("success_criteria") or "The requested deliverable is completed."),
            "verification": _clip(task_brief.get("validation_required") or "Provide minimum validation evidence."),
            "required": True,
        },
        {
            "code": "AC-002",
            "text": "The MCUM session records task result, outcome, confidence and validation summary.",
            "verification": "project_registry.project_logs contains the task closure.",
            "required": True,
        },
    ]
    if mode == "full":
        criteria.append(
            {
                "code": "AC-003",
                "text": "The implementation maps back to this spec through files, artifacts, tests or notes.",
                "verification": "spec_trace_links or task metadata references the implementation evidence.",
                "required": True,
            }
        )
    return criteria


def _scenarios(task_brief: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    objective = _clip(task_brief.get("objective") or task_brief.get("task") or "requested task")
    scenarios = [
        {
            "kind": "happy_path",
            "title": "Successful MCUM execution",
            "given": "A confirmed task brief and available project context.",
            "when": f"MCUM executes: {objective}",
            "then": _clip(task_brief.get("expected_deliverable") or "The expected deliverable is produced."),
        },
        {
            "kind": "sad_path",
            "title": "Validation fails or evidence is missing",
            "given": "The task cannot be validated or fails during execution.",
            "when": "MCUM closes the session.",
            "then": "The outcome is recorded as partial/failure and learning is not promoted as a successful pattern.",
        },
    ]
    if mode == "full":
        scenarios.append(
            {
                "kind": "anti_loop",
                "title": "Avoid repeating a failed path",
                "given": "A similar task or error has failed before.",
                "when": "MCUM retries or delegates work.",
                "then": "The new attempt must include a material strategy change and explicit validation.",
            }
        )
    return scenarios


def _clarification_questions(task_brief: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    if not str(task_brief.get("objective") or "").strip():
        questions.append(
            {
                "code": "Q-001",
                "question": "What exact behavior or decision should MCUM optimize for?",
                "reason": "Missing objective increases guessing and token waste.",
                "required_for": "high_confidence_spec",
            }
        )
    if not str(task_brief.get("expected_deliverable") or "").strip():
        questions.append(
            {
                "code": "Q-002",
                "question": "What concrete artifact or outcome should exist at the end?",
                "reason": "A deliverable anchors acceptance criteria.",
                "required_for": "closure",
            }
        )
    if not str(task_brief.get("validation_required") or "").strip():
        questions.append(
            {
                "code": "Q-003",
                "question": "How should MCUM prove this result is correct?",
                "reason": "Validation evidence prevents false success and polluted memory.",
                "required_for": "memory_promotion",
            }
        )
    if not _as_list(task_brief.get("sources_to_review")):
        questions.append(
            {
                "code": "Q-004",
                "question": "Which files, URLs, screenshots, docs or examples should MCUM treat as source of truth?",
                "reason": "Explicit sources reduce irrelevant context retrieval.",
                "required_for": "retrieval_precision",
            }
        )
    if mode == "full" and not _as_list(task_brief.get("constraints")):
        questions.append(
            {
                "code": "Q-005",
                "question": "What must MCUM avoid changing or assuming?",
                "reason": "Complex tasks need scope-out boundaries to avoid regressions.",
                "required_for": "safe_execution",
            }
        )
    return questions


def build_spec_contract(
    task_brief: dict[str, Any],
    *,
    task_id: str,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = normalize_spec_policy(execution_policy)
    if not policy["enabled"]:
        return {"enabled": False, "required": False, "mode": "disabled"}

    mode = resolve_spec_mode(task_brief, policy)
    sources = _as_list(task_brief.get("sources_to_review"))
    constraints = _as_list(task_brief.get("constraints"))
    task_type = str(task_brief.get("task_type") or "analizar")
    risk_level = str(task_brief.get("risk_level") or "medio")
    objective = str(task_brief.get("objective") or "").strip()
    deliverable = str(task_brief.get("expected_deliverable") or "").strip()
    validation = str(task_brief.get("validation_required") or "").strip()

    contract = {
        "enabled": True,
        "task_id": task_id,
        "mode": mode,
        "required": bool(policy.get("always_attach", True)),
        "status": "auto_generated",
        "title": _clip(objective or task_brief.get("task") or "MCUM task spec", 160),
        "task_type": task_type,
        "execution_mode": str(task_brief.get("execution_mode") or "ejecutar"),
        "risk_level": risk_level,
        "objective": objective,
        "user_story": f"As the requester, I want MCUM to complete: {objective}",
        "scope_in": [
            _clip(objective or "Execute the requested task."),
            _clip(deliverable or "Produce the expected deliverable."),
            *[f"Review source: {source}" for source in sources[:4]],
        ],
        "scope_out": [
            "No unrelated file changes.",
            "No unvalidated success closure.",
            "No promotion of failed or weak evidence into durable learning.",
        ],
        "actors": ["requester", "mcum-orchestrator", "selected-skill-or-worker"],
        "preconditions": [
            "Task brief is normalized.",
            "Project path is known.",
            "PostgreSQL memory is available for retrieval and logging.",
        ],
        "trigger": "User task enters MCUM through workspace_session.",
        "assumptions": _assumptions(task_brief),
        "constraints": constraints,
        "business_rules": [
            "Project-first memory must be preferred before cross-project fallback.",
            "Validation evidence is mandatory before recording success.",
            "Runtime artifacts should be persisted when they add audit value.",
        ],
        "scenarios": _scenarios(task_brief, mode),
        "acceptance_criteria": _acceptance_criteria(task_brief, mode),
        "clarification_questions": _clarification_questions(task_brief, mode),
        "qa_plan": [
            _clip(validation or "Run the minimum relevant validation for the task."),
            "Record validation summary in MCUM task result.",
        ],
    }

    filled_sections = sum(1 for key, value in contract.items() if value not in (None, "", [], {}))
    score = round(min(1.0, filled_sections / 18.0), 4)
    if not objective:
        score = round(max(0.0, score - 0.18), 4)
    if not deliverable:
        score = round(max(0.0, score - 0.12), 4)
    if not validation:
        score = round(max(0.0, score - 0.08), 4)
    contract["confidence_score"] = score
    contract["summary"] = {
        "mode": mode,
        "required": contract["required"],
        "status": contract["status"],
        "confidence_score": score,
        "acceptance_count": len(contract["acceptance_criteria"]),
        "assumption_count": len(contract["assumptions"]),
        "scenario_count": len(contract["scenarios"]),
        "clarification_count": len(contract["clarification_questions"]),
    }
    return contract


def spec_guardrails(contract: dict[str, Any]) -> list[str]:
    if not contract.get("enabled"):
        return []
    return [
        f"Spec Contract: mode={contract.get('mode')} score={contract.get('confidence_score')}.",
        "Spec Contract: do not implement outside scope_out without explicit user confirmation.",
        "Spec Contract: close success only when acceptance criteria have validation evidence.",
    ]
