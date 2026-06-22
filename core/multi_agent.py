"""
Supervised multi-agent planning helpers for MCUM.

This module keeps multi-agent logic policy-driven and isolated from the
session runtime so the existing single-session workflow remains intact.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .model_router import (
    normalize_model_router_policy,
    route_model_for_coordinator,
    route_model_for_worker,
    summarize_model_routes,
)
from .project_context_orchestrator import build_worker_context_slice


_TASK_ROLE_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "analizar": [
        {
            "role": "context_analyst",
            "mode": "read_only",
            "task_type": "analizar",
            "execution_mode": "analizar",
            "goal": "Levantar contexto, riesgos y evidencia relevante sin editar.",
            "validation_required": "Entrega findings claros y evidencia revisada.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "analizar",
            "goal": "Contrastar hallazgos y detectar vacios o inconsistencias.",
            "validation_required": "Corrobora o contradice los hallazgos principales.",
        },
    ],
    "planificar": [
        {
            "role": "strategist",
            "mode": "read_only",
            "task_type": "planificar",
            "execution_mode": "proponer",
            "goal": "Proponer estrategia y orden de trabajo.",
            "validation_required": "Plan con riesgos y supuestos explicitos.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "analizar",
            "goal": "Revisar el plan y marcar trade-offs ocultos.",
            "validation_required": "Checklist de riesgos del plan.",
        },
    ],
    "corregir": [
        {
            "role": "context_analyst",
            "mode": "read_only",
            "task_type": "analizar",
            "execution_mode": "analizar",
            "goal": "Aislar causa raiz y superficie afectada.",
            "validation_required": "Lista de causas probables y evidencia.",
        },
        {
            "role": "implementer",
            "mode": "write",
            "task_type": "corregir",
            "execution_mode": "ejecutar",
            "goal": "Aplicar la correccion principal sin salir del scope editable.",
            "validation_required": "Cambio implementado y validado.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "ejecutar",
            "goal": "Revisar regresiones, tests y consistencia final.",
            "validation_required": "Validacion independiente o smoke test.",
        },
    ],
    "mejorar": [
        {
            "role": "context_analyst",
            "mode": "read_only",
            "task_type": "analizar",
            "execution_mode": "analizar",
            "goal": "Detectar cuellos de botella y oportunidades reales.",
            "validation_required": "Diagnostico claro del estado actual.",
        },
        {
            "role": "implementer",
            "mode": "write",
            "task_type": "mejorar",
            "execution_mode": "ejecutar",
            "goal": "Aplicar la mejora prioritaria dentro del scope permitido.",
            "validation_required": "Cambio medible o verificable.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "ejecutar",
            "goal": "Verificar impacto y evitar regresiones.",
            "validation_required": "Comparacion antes/despues o smoke test.",
        },
    ],
    "crear": [
        {
            "role": "planner",
            "mode": "read_only",
            "task_type": "planificar",
            "execution_mode": "proponer",
            "goal": "Desglosar la construccion y definir guardrails.",
            "validation_required": "Plan implementable y delimitado.",
        },
        {
            "role": "implementer",
            "mode": "write",
            "task_type": "crear",
            "execution_mode": "ejecutar",
            "goal": "Construir la solucion principal.",
            "validation_required": "Entrega funcional o artefacto creado.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "ejecutar",
            "goal": "Revisar cumplimiento de entregable y riesgos.",
            "validation_required": "Validacion independiente del entregable.",
        },
    ],
    "automatizar": [
        {
            "role": "planner",
            "mode": "read_only",
            "task_type": "planificar",
            "execution_mode": "proponer",
            "goal": "Definir pipeline, constraints y puntos de fallo.",
            "validation_required": "Plan del flujo automatizado.",
        },
        {
            "role": "implementer",
            "mode": "write",
            "task_type": "automatizar",
            "execution_mode": "ejecutar",
            "goal": "Implementar el pipeline o script principal.",
            "validation_required": "Pipeline ejecuta o compila correctamente.",
        },
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "ejecutar",
            "goal": "Verificar estabilidad, retries y salidas.",
            "validation_required": "Validacion o smoke del pipeline.",
        },
    ],
    "validar": [
        {
            "role": "validator",
            "mode": "read_only",
            "task_type": "validar",
            "execution_mode": "ejecutar",
            "goal": "Ejecutar la validacion principal.",
            "validation_required": "Resultado verificable.",
        },
        {
            "role": "auditor",
            "mode": "read_only",
            "task_type": "analizar",
            "execution_mode": "analizar",
            "goal": "Revisar cobertura, huecos y riesgos remanentes.",
            "validation_required": "Auditoria de la validacion realizada.",
        },
    ],
}


def _normalize_role_text(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalize_scope(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def estimate_task_complexity(task_brief: dict[str, Any]) -> float:
    task_type = str(task_brief.get("task_type") or "analizar").lower()
    execution_mode = str(task_brief.get("execution_mode") or "ejecutar").lower()
    risk_level = str(task_brief.get("risk_level") or "medio").lower()
    sources = len(list(task_brief.get("sources_to_review") or []))
    constraints = len(list(task_brief.get("constraints") or []))
    validation_required = bool(task_brief.get("validation_required"))
    editable_scope = bool(task_brief.get("editable_scope"))
    read_only_scope = bool(task_brief.get("read_only_scope"))

    score = 0.18
    score += min(0.18, sources * 0.03)
    score += min(0.14, constraints * 0.035)
    if validation_required:
        score += 0.08
    if editable_scope and read_only_scope:
        score += 0.08

    task_type_weights = {
        "analizar": 0.10,
        "planificar": 0.12,
        "validar": 0.12,
        "corregir": 0.20,
        "mejorar": 0.22,
        "crear": 0.24,
        "automatizar": 0.25,
    }
    execution_weights = {
        "analizar": 0.02,
        "proponer": 0.05,
        "ejecutar": 0.11,
    }
    risk_weights = {
        "bajo": 0.02,
        "medio": 0.07,
        "alto": 0.14,
        "high": 0.14,
        "critico": 0.18,
        "critical": 0.18,
    }

    score += task_type_weights.get(task_type, 0.10)
    score += execution_weights.get(execution_mode, 0.05)
    score += risk_weights.get(risk_level, 0.07)
    return round(min(score, 1.0), 3)


def _worker_templates_for_task(task_type: str) -> list[dict[str, Any]]:
    return deepcopy(
        _TASK_ROLE_TEMPLATES.get(task_type) or _TASK_ROLE_TEMPLATES["analizar"]
    )


def _apply_worker_limits(
    workers: list[dict[str, Any]],
    *,
    max_workers: int,
    max_write_workers: int,
) -> list[dict[str, Any]]:
    limited: list[dict[str, Any]] = []
    write_workers = 0
    for worker in workers:
        is_write = worker.get("mode") == "write"
        if is_write and write_workers >= max_write_workers:
            continue
        limited.append(worker)
        if is_write:
            write_workers += 1
        if max_workers and len(limited) >= max_workers:
            break
    return limited


def _build_worker_brief(
    task_brief: dict[str, Any],
    worker: dict[str, Any],
    *,
    model_route: dict[str, Any] | None,
    selected_skill: str | None,
    worker_index: int,
    worker_count: int,
    parent_task_id: str | None,
    parent_session_id: str | None,
    suppress_autonomy_hooks: bool,
    allow_learning_writes: bool,
) -> dict[str, Any]:
    role = str(worker.get("role") or f"worker_{worker_index}")
    context_slice = build_worker_context_slice(
        task_brief.get("project_context_envelope"),
        role=role,
        mode=str(worker.get("mode") or "read_only"),
        max_tokens=int(
            worker.get("context_token_budget")
            or task_brief.get("worker_context_token_budget")
            or 900
        ),
    )
    brief = {
        "project_path": task_brief.get("project_path"),
        "task_type": worker.get("task_type") or task_brief.get("task_type"),
        "objective": worker.get("goal") or task_brief.get("objective"),
        "expected_deliverable": worker.get("deliverable")
        or f"Resultado del worker {role} para alimentar al coordinador.",
        "sources_to_review": list(task_brief.get("sources_to_review") or []),
        "constraints": list(task_brief.get("constraints") or []),
        "success_criteria": worker.get("success_criteria")
        or "El worker entrega evidencia util y delimitada para el coordinador.",
        "execution_mode": worker.get("execution_mode") or task_brief.get("execution_mode"),
        "risk_level": task_brief.get("risk_level"),
        "validation_required": worker.get("validation_required")
        or task_brief.get("validation_required"),
        "confirmed": True,
        "brief_source": "multi_agent_plan",
        "supervised_multi_agent": True,
        "orchestration_role": "worker",
        "worker_role": role,
        "parent_task_id": parent_task_id,
        "parent_session_id": parent_session_id,
        "worker_index": worker_index,
        "worker_count": worker_count,
        "suppress_autonomy_hooks": suppress_autonomy_hooks,
        "allow_worker_learning_writes": allow_learning_writes,
        "selected_skill_hint": selected_skill,
        "agent_profile": (model_route or {}).get("agent_profile"),
        "recommended_model": (model_route or {}).get("recommended_model"),
        "model_decision": (model_route or {}).get("decision"),
        "model_reasoning_effort": (model_route or {}).get("reasoning_effort"),
        "model_token_budget": (model_route or {}).get("token_budget"),
        "editable_scope": worker.get("editable_scope"),
        "read_only_scope": worker.get("read_only_scope"),
        "protected_scope": worker.get("protected_scope"),
        "iteration_budget": worker.get("iteration_budget") or 1,
        "context_intent": worker.get("context_intent") or (
            (context_slice.get("query_plan") or {}).get("primary_intent")
            if context_slice
            else "delegate"
        ),
        "context_pack_id": context_slice.get("context_pack_id") if context_slice else None,
        "graph_snapshot_id": context_slice.get("graph_snapshot_id") if context_slice else None,
        "worker_context_slice": context_slice,
    }
    return {key: value for key, value in brief.items() if value not in (None, "", [], {})}


def build_multi_agent_plan(
    task_brief: dict[str, Any],
    execution_policy: dict[str, Any],
    *,
    selected_skill: str | None = None,
    parent_session_id: str | None = None,
) -> dict[str, Any]:
    multi_policy = deepcopy(execution_policy.get("multi_agent") or {})
    worker_policy = deepcopy(multi_policy.get("worker_sessions") or {})
    merge_policy = deepcopy(multi_policy.get("merge_policy") or {})
    execution_runtime = deepcopy(multi_policy.get("execution") or {})
    enabled = bool(multi_policy.get("enabled", True))
    task_type = str(task_brief.get("task_type") or "analizar").lower()
    complexity_score = estimate_task_complexity(task_brief)
    threshold = float(multi_policy.get("parallelize_min_complexity", 0.58) or 0.58)
    allowed_task_types = set(multi_policy.get("allowed_task_types") or [])
    recommended = enabled and complexity_score >= threshold and (
        not allowed_task_types or task_type in allowed_task_types
    )
    supervised_requested = bool(task_brief.get("supervised_multi_agent", False))
    max_workers = max(1, int(task_brief.get("max_workers") or multi_policy.get("default_max_workers", 3) or 3))
    max_write_workers = max(
        1,
        int(multi_policy.get("max_write_workers", 1) or 1),
    )
    selected_skill_hint = selected_skill or str(task_brief.get("selected_skill_hint") or "").strip() or None
    preferred_write_skill_hint = (
        str(task_brief.get("preferred_write_skill_hint") or "").strip() or selected_skill_hint
    )

    workers = _worker_templates_for_task(task_type)
    editable_scope = _normalize_scope(task_brief.get("editable_scope") or task_brief.get("project_path"))
    read_only_scope = _normalize_scope(task_brief.get("read_only_scope") or task_brief.get("project_path"))
    protected_scope = _normalize_scope(task_brief.get("protected_scope"))
    default_iteration_budget = max(1, int(task_brief.get("iteration_budget") or multi_policy.get("default_iteration_budget", 2) or 2))

    for worker in workers:
        if worker.get("mode") == "write":
            worker["editable_scope"] = editable_scope
            worker["read_only_scope"] = read_only_scope
        else:
            worker["editable_scope"] = None
            worker["read_only_scope"] = read_only_scope
        worker["protected_scope"] = protected_scope
        worker["iteration_budget"] = default_iteration_budget if worker.get("mode") == "write" else 1
        worker["skill_hint"] = preferred_write_skill_hint if worker.get("mode") == "write" else "mcum-orchestrator"

    workers = _apply_worker_limits(
        workers,
        max_workers=max_workers,
        max_write_workers=max_write_workers,
    )
    worker_count = len(workers)
    task_id = str(task_brief.get("task_id") or "").strip() or None
    suppress_autonomy_hooks = bool(
        worker_policy.get("suppress_autonomy_hooks", True)
    )
    allow_learning_writes = str(worker_policy.get("writeback_mode", "coordinator_only")) != "coordinator_only"
    model_policy = normalize_model_router_policy(execution_policy)
    coordinator_model_route = route_model_for_coordinator(
        task_brief,
        complexity_score=complexity_score,
        execution_policy=execution_policy,
    )
    for worker in workers:
        model_route = route_model_for_worker(
            worker,
            task_brief,
            complexity_score=complexity_score,
            execution_policy=execution_policy,
        )
        worker["agent_profile"] = model_route.get("agent_profile")
        worker["model_route"] = model_route
        worker["recommended_model"] = model_route.get("recommended_model")
        worker["model_reasoning_effort"] = model_route.get("reasoning_effort")
        worker["model_token_budget"] = model_route.get("token_budget")

    worker_briefs = [
        _build_worker_brief(
            task_brief,
            worker,
            model_route=dict(worker.get("model_route") or {}),
            selected_skill=selected_skill_hint,
            worker_index=index + 1,
            worker_count=worker_count,
            parent_task_id=task_id,
            parent_session_id=parent_session_id,
            suppress_autonomy_hooks=suppress_autonomy_hooks,
            allow_learning_writes=allow_learning_writes,
        )
        for index, worker in enumerate(workers)
    ]

    return {
        "enabled": enabled,
        "recommended": recommended,
        "supervised_requested": supervised_requested,
        "mode": "supervised" if enabled and (recommended or supervised_requested) else "single_agent",
        "complexity_score": complexity_score,
        "complexity_threshold": threshold,
        "selected_skill_hint": selected_skill_hint,
        "preferred_write_skill_hint": preferred_write_skill_hint,
        "anti_loop_recommended": bool(task_brief.get("anti_loop_force_multi_run", False)),
        "coordinator": {
            "skill": "mcum-orchestrator",
            "agent_profile": coordinator_model_route.get("agent_profile"),
            "recommended_model": coordinator_model_route.get("recommended_model"),
            "model_route": coordinator_model_route,
            "merge_policy": merge_policy.get("default", "coordinator_decides"),
            "require_validator": bool(merge_policy.get("require_validator_for_write_tasks", True)),
        },
        "worker_policy": {
            "max_workers": max_workers,
            "max_write_workers": max_write_workers,
            "suppress_autonomy_hooks": suppress_autonomy_hooks,
            "writeback_mode": worker_policy.get("writeback_mode", "coordinator_only"),
            "parallel_read_only_workers": bool(execution_runtime.get("parallel_read_only_workers", True)),
            "max_parallel_read_only": max(1, int(execution_runtime.get("max_parallel_read_only", 2) or 2)),
            "stop_on_first_failure": bool(execution_runtime.get("stop_on_first_failure", True)),
            "auto_promote_run_when_complex": bool(execution_runtime.get("auto_promote_run_when_complex", False)),
            "require_worker_commands_for_auto_promote": bool(
                execution_runtime.get("require_worker_commands_for_auto_promote", True)
            ),
            "map_primary_command_to_write_worker": bool(execution_runtime.get("map_primary_command_to_write_worker", True)),
        },
        "workers": workers,
        "worker_briefs": worker_briefs,
        "model_routing": {
            "enabled": bool(model_policy.get("enabled", True)),
            "strategy": model_policy.get("strategy", "cost_aware"),
            "coordinator": coordinator_model_route,
            "summary": summarize_model_routes(
                [coordinator_model_route]
                + [dict(worker.get("model_route") or {}) for worker in workers]
            ),
        },
        "merge_policy": merge_policy,
    }


def resolve_orchestration_context(
    task_brief: dict[str, Any],
    execution_policy: dict[str, Any],
) -> dict[str, Any]:
    multi_policy = deepcopy(execution_policy.get("multi_agent") or {})
    worker_policy = deepcopy(multi_policy.get("worker_sessions") or {})
    role = _normalize_role_text(task_brief.get("orchestration_role"))
    parent_task_id = _normalize_role_text(task_brief.get("parent_task_id"))
    parent_session_id = _normalize_role_text(task_brief.get("parent_session_id"))
    if role is None:
        role = "worker" if parent_task_id else "coordinator"
    supervised = bool(task_brief.get("supervised_multi_agent", False) or role == "worker")
    suppress_autonomy_hooks = bool(
        task_brief.get(
            "suppress_autonomy_hooks",
            role == "worker" and worker_policy.get("suppress_autonomy_hooks", True),
        )
    )
    writeback_mode = str(worker_policy.get("writeback_mode", "coordinator_only") or "coordinator_only")
    allow_learning_writes = bool(
        task_brief.get(
            "allow_worker_learning_writes",
            role != "worker" or writeback_mode != "coordinator_only",
        )
    )
    worker_index = int(task_brief.get("worker_index") or 0)
    worker_count = int(task_brief.get("worker_count") or 0)
    return {
        "enabled": bool(multi_policy.get("enabled", True)),
        "mode": "supervised" if supervised else "single_agent",
        "role": role,
        "worker_role": _normalize_role_text(task_brief.get("worker_role")),
        "parent_task_id": parent_task_id,
        "parent_session_id": parent_session_id,
        "worker_index": worker_index,
        "worker_count": worker_count,
        "suppress_autonomy_hooks": suppress_autonomy_hooks,
        "allow_learning_writes": allow_learning_writes,
        "writeback_mode": writeback_mode,
        "selected_skill_hint": _normalize_role_text(task_brief.get("selected_skill_hint")),
        "recommended_model": _normalize_role_text(task_brief.get("recommended_model")),
        "agent_profile": _normalize_role_text(task_brief.get("agent_profile")),
    }
