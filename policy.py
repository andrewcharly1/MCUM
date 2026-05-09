"""
Policy loading and task brief normalization for MCUM strict mode.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INTAKE_POLICY_FILE = ROOT / "directives" / "intake_policy.json"
EXECUTION_POLICY_FILE = ROOT / "directives" / "execution_policy.json"


DEFAULT_INTAKE_POLICY = {
    "version": "1.0.0",
    "strict_intake": True,
    "required_fields": [
        "project_path",
        "task_type",
        "objective",
        "expected_deliverable",
        "success_criteria",
        "execution_mode",
    ],
    "optional_fields": [
        "sources_to_review",
        "constraints",
        "risk_level",
        "validation_required",
    ],
    "allowed_task_types": [
        "analizar",
        "crear",
        "corregir",
        "mejorar",
        "planificar",
        "validar",
        "automatizar",
    ],
    "allowed_execution_modes": [
        "analizar",
        "proponer",
        "ejecutar",
    ],
    "require_user_confirmation": True,
    "block_if_missing_required_fields": True,
}


DEFAULT_EXECUTION_POLICY = {
    "version": "1.0.0",
    "strict_mode": True,
    "min_dispatch_confidence": 0.80,
    "require_active_session": True,
    "require_task_brief": True,
    "require_validation_before_success": True,
    "require_artifacts_for_success": True,
    "project_first_retrieval": True,
    "allow_cross_project_fallback": True,
    "cross_project_fallback_only_if_no_project_hits": True,
    "max_cross_project_memories": 1,
    "max_playbooks": 3,
    "min_playbook_similarity": 0.28,
    "autonomous_writeback": "candidate",
    "autonomous_writeback_targets": [
        "html-dashboard-expert",
        "go-industrial-expert",
        "ui-ux-pro-max",
    ],
    "autonomous_writeback_exclude": [],
    "sisl_mode": "db_only",
    "skill_factory_mode": "candidate_bootstrap",
    "skill_factory_min_gap_occurrences": 2,
    "skill_factory_low_confidence_threshold": 0.72,
    "skill_factory_max_candidates_per_cycle": 1,
    "skill_factory_min_active_tests": 8,
    "skill_factory_min_successful_uses": 2,
    "skill_factory_min_success_rate": 0.75,
    "block_on_unconfirmed_brief": True,
    "block_on_policy_violation": True,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_policy(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(defaults)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(defaults)
    return _deep_merge(defaults, raw)


def load_intake_policy() -> dict[str, Any]:
    return _load_policy(INTAKE_POLICY_FILE, DEFAULT_INTAKE_POLICY)


def load_execution_policy() -> dict[str, Any]:
    return _load_policy(EXECUTION_POLICY_FILE, DEFAULT_EXECUTION_POLICY)


def infer_task_type(task_description: str) -> str:
    text = (task_description or "").lower()
    mapping = [
        ("analizar", ("analiza", "analizar", "review", "revisar", "diagnost", "inspeccion")),
        ("crear", ("crear", "construir", "generar", "nuevo", "build")),
        ("corregir", ("corregir", "fix", "arreglar", "resolver error", "reparar")),
        ("mejorar", ("mejorar", "optimizar", "refactor", "hardening")),
        ("planificar", ("plan", "planificar", "roadmap", "estrategia")),
        ("validar", ("validar", "test", "prueba", "verificar", "compilar")),
        ("automatizar", ("automatizar", "pipeline", "batch", "script", "orquestar")),
    ]
    for task_type, tokens in mapping:
        if any(token in text for token in tokens):
            return task_type
    return "analizar"


def normalize_task_brief(
    project_path: str,
    task_description: str,
    task_brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    incoming = dict(task_brief or {})
    normalized = {
        "project_path": incoming.get("project_path") or project_path,
        "task_type": incoming.get("task_type") or infer_task_type(task_description),
        "objective": incoming.get("objective") or task_description,
        "expected_deliverable": incoming.get("expected_deliverable") or "Resultado validado y registrado en MCUM.",
        "sources_to_review": list(incoming.get("sources_to_review") or []),
        "constraints": list(incoming.get("constraints") or []),
        "success_criteria": incoming.get("success_criteria") or "La tarea queda registrada con outcome claro, validación y artifacts cuando aplique.",
        "execution_mode": incoming.get("execution_mode") or "ejecutar",
        "risk_level": incoming.get("risk_level") or "medio",
        "validation_required": incoming.get("validation_required") or "validación mínima obligatoria",
        "confirmed": bool(incoming.get("confirmed", False)),
        "brief_source": incoming.get("brief_source") or ("user" if task_brief else "inferred"),
    }
    return normalized


def missing_required_fields(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field_name in intake_policy.get("required_fields", []):
        value = task_brief.get(field_name)
        if value in (None, "", [], {}):
            missing.append(field_name)
    return missing


def validate_task_brief(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> list[str]:
    issues = missing_required_fields(task_brief, intake_policy)

    allowed_task_types = set(intake_policy.get("allowed_task_types", []))
    task_type = task_brief.get("task_type")
    if task_type and allowed_task_types and task_type not in allowed_task_types:
        issues.append(f"task_type:{task_type}")

    allowed_execution_modes = set(intake_policy.get("allowed_execution_modes", []))
    execution_mode = task_brief.get("execution_mode")
    if execution_mode and allowed_execution_modes and execution_mode not in allowed_execution_modes:
        issues.append(f"execution_mode:{execution_mode}")

    return issues


def task_brief_metrics(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> dict[str, Any]:
    required_fields = list(intake_policy.get("required_fields", []))
    optional_fields = list(intake_policy.get("optional_fields", []))

    required_completed = sum(
        1 for field_name in required_fields if task_brief.get(field_name) not in (None, "", [], {})
    )
    optional_completed = sum(
        1 for field_name in optional_fields if task_brief.get(field_name) not in (None, "", [], {})
    )
    total_fields = len(required_fields) + len(optional_fields)
    completed_fields = required_completed + optional_completed

    completeness = round(completed_fields / total_fields, 2) if total_fields else 1.0
    return {
        "required_completed": required_completed,
        "required_total": len(required_fields),
        "optional_completed": optional_completed,
        "optional_total": len(optional_fields),
        "completeness_score": completeness,
    }
