"""
State compiler for MCUM task context.

Builds a compact, utility-scored context package so downstream execution sees
the best operational evidence first instead of a flat dump of every retrieved
item.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db.project_registry import estimate_tokens


DEFAULT_STATE_COMPILER_POLICY = {
    "enabled": True,
    "max_context_tokens": 1400,
    "max_experiences": 4,
    "max_code_graph_hits": 4,
    "max_knowledge_library_hits": 2,
    "max_active_patterns": 2,
    "max_failure_patterns": 2,
    "max_conflict_cases": 1,
    "max_playbooks": 2,
    "max_warnings": 4,
    "max_sources_to_review": 4,
    "max_constraints": 4,
    "effectiveness_history_limit": 60,
    "min_effectiveness_samples": 3,
    "max_learned_reason_adjustments": 3,
    "adaptive_section_limits_enabled": True,
    "adaptive_token_targets_enabled": True,
    "max_adaptive_slot_shift": 1,
}

_SECTION_LIMIT_KEYS = {
    "experiences": "max_experiences",
    "code_graph": "max_code_graph_hits",
    "knowledge_library": "max_knowledge_library_hits",
    "active_patterns": "max_active_patterns",
    "failure_patterns": "max_failure_patterns",
    "conflict_cases": "max_conflict_cases",
    "playbooks": "max_playbooks",
}
_SECTION_ORDER = (
    "playbooks",
    "code_graph",
    "experiences",
    "knowledge_library",
    "active_patterns",
    "failure_patterns",
    "conflict_cases",
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*", re.IGNORECASE)
_UTILITY_REASON_PRIORITY = {
    "compact_playbook": 5,
    "validated_compact_playbook": 4,
    "reusable_compact_playbook": 3,
    "source_match": 2,
    "source_overlap": 1,
}


def _normalize_policy(execution_policy: dict[str, Any] | None) -> dict[str, Any]:
    policy = dict(DEFAULT_STATE_COMPILER_POLICY)
    policy.update((execution_policy or {}).get("state_compiler") or {})
    return policy


def _tokenize(text: Any) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(str(text or "")) if len(token) >= 3}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return loaded if isinstance(loaded, dict) else {"raw": loaded}
    return {}


def _clip_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_pathish(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return text.rstrip("/")


def _path_tokens(path: Any) -> set[str]:
    normalized = _normalize_pathish(path)
    if not normalized:
        return set()
    token_source = normalized.replace("/", " ").replace(".", " ").replace("-", " ").replace("_", " ")
    return _tokenize(token_source)


def _basename(path: Any) -> str:
    normalized = _normalize_pathish(path)
    if not normalized:
        return ""
    return Path(normalized).name.lower()


def _evidence_payload(item: dict[str, Any], section: str) -> dict[str, Any]:
    if section == "playbooks":
        return {
            "title": item.get("title"),
            "objective": item.get("objective"),
            "output_summary": item.get("output_summary"),
            "reusable_when": item.get("reusable_when"),
            "commands": list(item.get("commands") or [])[:3],
            "files_touched": list(item.get("files_touched") or [])[:4],
        }
    if section == "code_graph":
        content = _normalize_content(item.get("content"))
        evidence_refs = list(item.get("evidence_refs") or [])
        return {
            "title": item.get("title") or item.get("qualified_name"),
            "category": "code_graph",
            "content": {
                "conclusion": content.get("conclusion") or item.get("context_summary"),
                "context": content.get("context"),
            },
            "code_graph": {
                "relative_path": item.get("relative_path"),
                "language": item.get("language"),
                "node_kind": item.get("node_kind"),
                "qualified_name": item.get("qualified_name"),
                "signature": item.get("signature"),
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
                "score": item.get("score"),
            },
            "evidence_refs": evidence_refs[:2],
        }
    if section == "active_patterns":
        description = (
            item.get("description")
            or item.get("summary")
            or item.get("context")
            or item.get("conclusion")
            or item.get("name")
        )
        context_bits = [
            f"status={item.get('status') or 'unknown'}",
            f"avg_score={_as_float(item.get('avg_score'), 0.0):.2f}",
            f"experience_count={int(item.get('experience_count') or 0)}",
            f"context_diversity={int(item.get('context_diversity') or 0)}",
        ]
        return {
            "title": item.get("title") or item.get("name"),
            "name": item.get("name"),
            "category": item.get("category"),
            "status": item.get("status"),
            "content": {
                "conclusion": description,
                "context": "; ".join(context_bits),
            },
            "experience_count": item.get("experience_count"),
            "avg_score": item.get("avg_score"),
            "context_diversity": item.get("context_diversity"),
            "promotion_criteria_met": item.get("promotion_criteria_met"),
            "replacement_pattern_id": item.get("replacement_pattern_id"),
            "evidence_ids": list(item.get("evidence_ids") or [])[:4],
            "evidence_projects": list(item.get("evidence_projects") or [])[:4],
            "evidence_skills": list(item.get("evidence_skills") or [])[:4],
        }
    if section == "knowledge_library":
        knowledge = dict(item.get("knowledge_library") or {})
        content = _normalize_content(item.get("content"))
        return {
            "title": item.get("title") or knowledge.get("document_title"),
            "category": item.get("category"),
            "content": {
                "conclusion": content.get("conclusion"),
                "context": content.get("context"),
            },
            "knowledge_library": {
                "document_title": knowledge.get("document_title"),
                "section_heading": knowledge.get("section_heading"),
                "summary_level": knowledge.get("summary_level"),
                "mode": knowledge.get("mode"),
                "page_start": knowledge.get("page_start"),
                "page_end": knowledge.get("page_end"),
            },
        }

    content = _normalize_content(item.get("content"))
    return {
        "title": item.get("title") or item.get("name"),
        "category": item.get("category"),
        "conclusion": content.get("conclusion"),
        "context": content.get("context"),
        "applicability": item.get("applicability"),
        "not_applicable_cases": item.get("not_applicable_cases"),
    }


def _evidence_text(item: dict[str, Any], section: str) -> str:
    payload = _evidence_payload(item, section)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _project_bonus(item: dict[str, Any], project_id: str | None) -> float:
    if not project_id:
        return 0.0
    candidate_project = str(item.get("project_id") or "").strip()
    if candidate_project and candidate_project == str(project_id):
        return 0.05
    for field in ("evidence_projects", "project_ids"):
        values = item.get(field) or []
        if isinstance(values, (list, tuple, set)) and str(project_id) in {str(value).strip() for value in values if str(value).strip()}:
            return 0.04
    return 0.0


def _skill_bonus(item: dict[str, Any], skill_name: str | None) -> float:
    if not skill_name:
        return 0.0
    candidate_skill = str(item.get("skill_name") or "").strip()
    if candidate_skill and candidate_skill == str(skill_name):
        return 0.05
    for field in ("evidence_skills", "skill_names"):
        values = item.get(field) or []
        if isinstance(values, (list, tuple, set)) and str(skill_name) in {str(value).strip() for value in values if str(value).strip()}:
            return 0.04
    return 0.0


def _section_bias(section: str) -> float:
    if section == "playbooks":
        return 0.16
    if section == "code_graph":
        return 0.17
    if section == "knowledge_library":
        return 0.11
    if section == "active_patterns":
        return 0.15
    if section == "failure_patterns":
        return 0.14
    if section == "conflict_cases":
        return 0.10
    return 0.0


def _item_paths(item: dict[str, Any], section: str) -> list[str]:
    paths: list[str] = []
    if section == "playbooks":
        for path in item.get("files_touched") or []:
            normalized = _normalize_pathish(path)
            if normalized:
                paths.append(normalized)
        return paths

    for artifact in item.get("source_artifacts") or []:
        if isinstance(artifact, dict):
            normalized = _normalize_pathish(artifact.get("path"))
            if normalized:
                paths.append(normalized)

    for ref in item.get("evidence_refs") or []:
        if isinstance(ref, dict):
            normalized = _normalize_pathish(ref.get("path") or ref.get("file"))
        else:
            normalized = _normalize_pathish(ref)
        if normalized:
            paths.append(normalized)
    return paths


def _artifact_density_bonus(item: dict[str, Any], section: str) -> tuple[float, dict[str, float]]:
    components: dict[str, float] = {}
    if section == "playbooks":
        commands = len(item.get("commands") or [])
        files_touched = len(item.get("files_touched") or [])
        if commands:
            components["commands"] = min(0.04, commands * 0.012)
        if files_touched:
            components["files_touched"] = min(0.06, files_touched * 0.015)
    else:
        source_artifacts = len(item.get("source_artifacts") or [])
        evidence_refs = len(item.get("evidence_refs") or [])
        if source_artifacts:
            components["source_artifacts"] = min(0.05, source_artifacts * 0.012)
        if evidence_refs:
            components["evidence_refs"] = min(0.03, evidence_refs * 0.010)
        if section == "active_patterns":
            evidence_ids = len(item.get("evidence_ids") or [])
            evidence_projects = len(item.get("evidence_projects") or [])
            evidence_skills = len(item.get("evidence_skills") or [])
            if evidence_ids:
                components["evidence_ids"] = min(0.05, evidence_ids * 0.012)
            if evidence_projects:
                components["evidence_projects"] = min(0.03, evidence_projects * 0.010)
            if evidence_skills:
                components["evidence_skills"] = min(0.03, evidence_skills * 0.010)

    total = round(sum(components.values()), 4)
    return total, components


def _source_alignment_bonus(
    item: dict[str, Any],
    section: str,
    *,
    source_tokens: set[str],
    source_paths: list[str],
) -> tuple[float, dict[str, float]]:
    if not source_tokens and not source_paths:
        return 0.0, {}

    item_paths = _item_paths(item, section)
    if not item_paths:
        return 0.0, {}

    components: dict[str, float] = {}
    item_path_tokens = set().union(*(_path_tokens(path) for path in item_paths)) if item_paths else set()
    token_overlap = len(source_tokens & item_path_tokens) / max(1, len(source_tokens)) if source_tokens else 0.0
    if token_overlap:
        components["source_overlap"] = min(0.12, token_overlap * 0.12)

    source_basenames = {_basename(path) for path in source_paths if _basename(path)}
    item_basenames = {_basename(path) for path in item_paths if _basename(path)}
    exact_hits = len(source_basenames & item_basenames)
    if exact_hits:
        components["source_match"] = min(0.10, 0.05 + (exact_hits - 1) * 0.02)

    total = round(sum(components.values()), 4)
    return total, components


def _task_shape_bonus(
    section: str,
    *,
    task_type: str,
    execution_mode: str,
    risk_level: str,
) -> tuple[float, dict[str, float]]:
    components: dict[str, float] = {}
    task_shape = f"{task_type} {execution_mode}".lower()
    risk = str(risk_level or "").lower()

    if execution_mode == "ejecutar":
        if section == "playbooks":
            components["execution_fit"] = 0.08
        elif section == "experiences":
            components["execution_fit"] = 0.04
    elif execution_mode in {"analizar", "proponer"}:
        if section in {"failure_patterns", "active_patterns", "conflict_cases"}:
            components["analysis_fit"] = 0.05
    elif execution_mode == "validar":
        if section in {"failure_patterns", "active_patterns", "conflict_cases"}:
            components["validation_fit"] = 0.06
        elif section == "playbooks":
            components["validation_fit"] = 0.03

    if any(keyword in task_shape for keyword in ("debug", "fix", "bug", "correg", "repair", "incident")):
        if section in {"failure_patterns", "active_patterns", "conflict_cases"}:
            components["debug_fit"] = max(components.get("debug_fit", 0.0), 0.06)
        elif section == "playbooks":
            components["debug_fit"] = max(components.get("debug_fit", 0.0), 0.03)
    if any(keyword in task_shape for keyword in ("crear", "implement", "build", "dashboard", "feature", "refactor")):
        if section == "playbooks":
            components["build_fit"] = max(components.get("build_fit", 0.0), 0.07)
        elif section == "experiences":
            components["build_fit"] = max(components.get("build_fit", 0.0), 0.04)

    if risk in {"alto", "high", "critical", "critico"} and section in {"failure_patterns", "active_patterns", "conflict_cases"}:
        components["risk_fit"] = max(components.get("risk_fit", 0.0), 0.04)

    total = round(sum(components.values()), 4)
    return total, components


def _playbook_compactness_metrics(item: dict[str, Any]) -> dict[str, Any]:
    output_summary = _clean_text(item.get("output_summary"))
    validation_summary = _clean_text(item.get("validation_summary"))
    objective = _clean_text(item.get("objective"))
    reusable_when = _clean_text(item.get("reusable_when"))
    commands = list(item.get("commands") or [])
    files_touched = list(item.get("files_touched") or [])

    output_chars = len(output_summary)
    validation_chars = len(validation_summary)
    coverage_parts = sum(1 for value in (objective, output_summary, validation_summary, reusable_when) if value)
    coverage_score = coverage_parts / 4

    if 48 <= output_chars <= 320:
        output_fit = 1.0
    elif 24 <= output_chars <= 420:
        output_fit = 0.82
    elif 1 <= output_chars <= 600:
        output_fit = 0.45
    else:
        output_fit = 0.0

    if 20 <= validation_chars <= 220:
        validation_fit = 1.0
    elif 1 <= validation_chars <= 320:
        validation_fit = 0.65
    else:
        validation_fit = 0.0

    if commands or files_touched:
        if len(commands) <= 3 and len(files_touched) <= 4:
            execution_fit = 1.0
        elif len(commands) <= 5 and len(files_touched) <= 6:
            execution_fit = 0.72
        else:
            execution_fit = 0.38
    else:
        execution_fit = 0.18

    bloat_penalty = 0.0
    if output_chars > 420:
        bloat_penalty += min(0.24, (output_chars - 420) / 1800)
    if validation_chars > 240:
        bloat_penalty += min(0.12, (validation_chars - 240) / 1200)

    compactness_score = _clamp(
        (
            (coverage_score * 0.30)
            + (output_fit * 0.30)
            + (validation_fit * 0.18)
            + (execution_fit * 0.16)
            + (0.06 if reusable_when else 0.0)
        )
        - bloat_penalty,
        0.0,
        1.0,
    )
    return {
        "compactness_score": round(compactness_score, 4),
        "coverage_score": round(coverage_score, 4),
        "output_fit": round(output_fit, 4),
        "validation_fit": round(validation_fit, 4),
        "execution_fit": round(execution_fit, 4),
        "bloat_penalty": round(bloat_penalty, 4),
        "has_validation_summary": bool(validation_summary),
        "has_reusable_when": bool(reusable_when),
        "output_chars": output_chars,
        "validation_chars": validation_chars,
    }


def _playbook_compaction_bonus(item: dict[str, Any], section: str) -> tuple[float, dict[str, float]]:
    if section != "playbooks":
        return 0.0, {}
    metrics = item.get("_compactness_profile")
    if not isinstance(metrics, dict):
        metrics = _playbook_compactness_metrics(item)

    compactness_score = _as_float(metrics.get("compactness_score"), 0.0)
    if compactness_score <= 0:
        return 0.0, {}

    components: dict[str, float] = {
        "compact_playbook": round(compactness_score * 0.12, 4),
    }
    if metrics.get("has_validation_summary"):
        components["validated_compact_playbook"] = round(compactness_score * 0.04, 4)
    if metrics.get("has_reusable_when"):
        components["reusable_compact_playbook"] = round(compactness_score * 0.02, 4)
    return round(sum(components.values()), 4), components


def _freshness_bonus(item: dict[str, Any]) -> tuple[float, dict[str, float]]:
    freshness = item.get("_freshness_score")
    if freshness is None:
        return 0.0, {}
    freshness_value = _as_float(freshness, 0.5)
    bonus = round((freshness_value - 0.5) * 0.08, 4)
    if not bonus:
        return 0.0, {}
    return bonus, {"freshness": bonus}


def _budget_fit_bonus(
    *,
    token_cost: int,
    budget_pressure: float,
    max_context_tokens: int,
) -> tuple[float, dict[str, float]]:
    if token_cost <= 0 or max_context_tokens <= 0:
        return 0.0, {}

    pressure = max(0.0, min(1.0, budget_pressure))
    if pressure <= 0.0:
        return 0.0, {}

    reference_cost = max(60, min(220, int(max_context_tokens * 0.12)))
    spread = (reference_cost - token_cost) / max(1.0, float(reference_cost))
    pressure_weight = 0.03 + (pressure * 0.03)
    bonus = round(_clamp(spread, -1.0, 1.0) * pressure_weight, 4)
    if not bonus:
        return 0.0, {}
    return bonus, {"budget_fit": bonus}


def _summarize_learning_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    summary = {
        "scope": profile.get("scope"),
        "sample_count": int(profile.get("sample_count") or 0),
        "items_observed": int(profile.get("items_observed") or 0),
        "active": bool(profile.get("active")),
        "section_adjustments": {
            section: value
            for section, value in (profile.get("section_adjustments") or {}).items()
            if abs(_as_float(value)) >= 0.005
        },
        "reason_adjustments": {
            reason: value
            for reason, value in sorted(
                (profile.get("reason_adjustments") or {}).items(),
                key=lambda item: abs(_as_float(item[1])),
                reverse=True,
            )[:4]
            if abs(_as_float(value)) >= 0.005
        },
        "efficiency_adjustments": {
            section: value
            for section, value in (profile.get("efficiency_adjustments") or {}).items()
            if abs(_as_float(value)) >= 0.005
        },
        "token_target_multipliers": {
            section: value
            for section, value in (profile.get("token_target_multipliers") or {}).items()
            if abs(_as_float(value) - 1.0) >= 0.05
        },
    }
    if (
        summary["sample_count"] <= 0
        and not summary["section_adjustments"]
        and not summary["reason_adjustments"]
    ):
        return None
    return summary


def _historical_effectiveness_bonus(
    section: str,
    *,
    components: dict[str, float],
    effectiveness_profile: dict[str, Any] | None,
    max_reason_adjustments: int,
) -> tuple[float, dict[str, float]]:
    profile = effectiveness_profile or {}
    if not profile.get("active"):
        return 0.0, {}

    learned_components: dict[str, float] = {}
    section_adjustment = _as_float((profile.get("section_adjustments") or {}).get(section), 0.0)
    if section_adjustment:
        learned_components["history_section"] = round(section_adjustment, 4)
    efficiency_adjustment = _as_float((profile.get("efficiency_adjustments") or {}).get(section), 0.0)
    if efficiency_adjustment:
        learned_components["history_efficiency"] = round(efficiency_adjustment, 4)

    candidates: list[tuple[str, float]] = []
    for reason, adjustment in (profile.get("reason_adjustments") or {}).items():
        base_value = _as_float(components.get(reason), 0.0)
        if base_value <= 0:
            continue
        strength = min(1.0, max(0.35, base_value / 0.05))
        scaled = round(_as_float(adjustment) * strength, 4)
        if scaled:
            candidates.append((f"history_{reason}", scaled))

    for key, value in sorted(candidates, key=lambda item: abs(item[1]), reverse=True)[:max_reason_adjustments]:
        learned_components[key] = value

    total = round(sum(learned_components.values()), 4)
    total = max(-0.12, min(0.12, total))
    return total, learned_components


def _build_section_selection_plan(
    *,
    policy: dict[str, Any],
    raw_counts: dict[str, int],
    scored_by_section: dict[str, list[dict[str, Any]]],
    effectiveness_profile: dict[str, Any] | None,
    remaining_tokens: int,
) -> tuple[dict[str, int], dict[str, int], dict[str, Any]]:
    base_limits = {
        section: max(0, int(policy[_SECTION_LIMIT_KEYS[section]]))
        for section in _SECTION_ORDER
    }
    adaptive_limits = dict(base_limits)
    slot_transfers: list[dict[str, Any]] = []
    profile = effectiveness_profile or {}
    section_adjustments = {
        section: _as_float((profile.get("section_adjustments") or {}).get(section), 0.0)
        for section in _SECTION_ORDER
    }
    efficiency_adjustments = {
        section: _as_float((profile.get("efficiency_adjustments") or {}).get(section), 0.0)
        for section in _SECTION_ORDER
    }
    token_target_multipliers = {
        section: _as_float((profile.get("token_target_multipliers") or {}).get(section), 1.0)
        for section in _SECTION_ORDER
    }

    if profile.get("active") and policy.get("adaptive_section_limits_enabled", True):
        max_shift = max(0, int(policy.get("max_adaptive_slot_shift") or 1))
        shifts_done = 0
        while shifts_done < max_shift:
            recipient_candidates = [
                section
                for section in _SECTION_ORDER
                if raw_counts.get(section, 0) > adaptive_limits[section]
                and (section_adjustments.get(section, 0.0) + efficiency_adjustments.get(section, 0.0)) >= 0.035
            ]
            donor_candidates = [
                section
                for section in _SECTION_ORDER
                if adaptive_limits[section] > 0
                and (
                    raw_counts.get(section, 0) < adaptive_limits[section]
                    or (section_adjustments.get(section, 0.0) + efficiency_adjustments.get(section, 0.0)) <= -0.03
                    or not scored_by_section.get(section)
                )
            ]
            if not recipient_candidates or not donor_candidates:
                break

            recipient = sorted(
                recipient_candidates,
                key=lambda section: (
                    section_adjustments.get(section, 0.0) + efficiency_adjustments.get(section, 0.0),
                    raw_counts.get(section, 0),
                ),
                reverse=True,
            )[0]
            donor = sorted(
                [section for section in donor_candidates if section != recipient],
                key=lambda section: (
                    section_adjustments.get(section, 0.0) + efficiency_adjustments.get(section, 0.0),
                    -raw_counts.get(section, 0),
                    _SECTION_ORDER.index(section),
                ),
            )
            if not donor:
                break
            donor_section = donor[0]
            if adaptive_limits[donor_section] <= 0:
                break

            adaptive_limits[recipient] += 1
            adaptive_limits[donor_section] -= 1
            shifts_done += 1
            slot_transfers.append(
                {
                    "from": donor_section,
                    "to": recipient,
                    "reason": "historical_context_learning",
                    "donor_adjustment": round(
                        section_adjustments.get(donor_section, 0.0) + efficiency_adjustments.get(donor_section, 0.0),
                        4,
                    ),
                    "recipient_adjustment": round(
                        section_adjustments.get(recipient, 0.0) + efficiency_adjustments.get(recipient, 0.0),
                        4,
                    ),
                }
            )

    section_token_targets = {section: 0 for section in _SECTION_ORDER}
    if remaining_tokens > 0 and policy.get("adaptive_token_targets_enabled", True):
        active_sections = [
            section
            for section in _SECTION_ORDER
            if adaptive_limits.get(section, 0) > 0 and scored_by_section.get(section)
        ]
        if active_sections:
            fair_share = max(1, remaining_tokens // max(1, len(active_sections)))
            floors: dict[str, int] = {}
            weights: dict[str, float] = {}
            total_floor = 0
            for section in active_sections:
                first_cost = int(scored_by_section[section][0].get("_token_cost") or 0)
                floors[section] = min(first_cost, fair_share)
                total_floor += floors[section]
                scarcity_bonus = 0.18 if raw_counts.get(section, 0) > adaptive_limits.get(section, 0) else 0.0
                weights[section] = max(
                    0.35,
                    float(adaptive_limits.get(section, 0))
                    + ((section_adjustments.get(section, 0.0) + efficiency_adjustments.get(section, 0.0)) * 5.0)
                    + scarcity_bonus,
                )

            remaining_after_floors = max(0, remaining_tokens - total_floor)
            total_weight = sum(weights.values()) or 1.0
            extras = {section: 0 for section in active_sections}
            for section in active_sections:
                extras[section] = int(remaining_after_floors * (weights[section] / total_weight))

            distributed = total_floor + sum(extras.values())
            leftover = max(0, remaining_tokens - distributed)
            for section in sorted(active_sections, key=lambda item: weights[item], reverse=True):
                if leftover <= 0:
                    break
                extras[section] += 1
                leftover -= 1

            for section in active_sections:
                multiplier = max(0.70, min(1.35, token_target_multipliers.get(section, 1.0)))
                adjusted_target = int((floors[section] + extras[section]) * multiplier)
                section_token_targets[section] = max(floors[section], adjusted_target)

            total_targets = sum(section_token_targets.values())
            overflow = max(0, total_targets - remaining_tokens)
            if overflow > 0:
                for section in sorted(
                    active_sections,
                    key=lambda item: (
                        token_target_multipliers.get(item, 1.0),
                        section_adjustments.get(item, 0.0) + efficiency_adjustments.get(item, 0.0),
                    ),
                ):
                    if overflow <= 0:
                        break
                    floor = floors[section]
                    reducible = max(0, section_token_targets[section] - floor)
                    if reducible <= 0:
                        continue
                    reduction = min(reducible, overflow)
                    section_token_targets[section] -= reduction
                    overflow -= reduction

    plan_summary = {
        "base_limits": base_limits,
        "adaptive_limits": adaptive_limits,
        "slot_transfers": slot_transfers,
        "section_adjustments": {
            section: round(value, 4)
            for section, value in section_adjustments.items()
            if value
        },
        "efficiency_adjustments": {
            section: round(value, 4)
            for section, value in efficiency_adjustments.items()
            if value
        },
        "token_target_multipliers": {
            section: round(value, 4)
            for section, value in token_target_multipliers.items()
            if abs(value - 1.0) >= 0.01
        },
        "section_token_targets": section_token_targets,
    }
    return adaptive_limits, section_token_targets, plan_summary


def _top_utility_reasons(components: dict[str, float]) -> list[str]:
    ranked = [
        name
        for name, value in sorted(
            components.items(),
            key=lambda item: (item[1], _UTILITY_REASON_PRIORITY.get(item[0], 0)),
            reverse=True,
        )
        if value > 0
    ]
    return ranked[:4]


def _utility_score(
    item: dict[str, Any],
    section: str,
    *,
    text: str,
    token_cost: int,
    budget_pressure: float,
    max_context_tokens: int,
    task_tokens: set[str],
    source_tokens: set[str],
    source_paths: list[str],
    task_type: str,
    execution_mode: str,
    risk_level: str,
    skill_name: str,
    project_id: str | None,
    effectiveness_profile: dict[str, Any] | None,
    max_learned_reason_adjustments: int,
) -> tuple[float, dict[str, float], list[str]]:
    text_tokens = _tokenize(text)
    overlap = len(task_tokens & text_tokens) / max(1, len(task_tokens)) if task_tokens else 0.0
    similarity = _as_float(item.get("_similarity"), _as_float(item.get("_combined_score"), 0.0))
    confidence = _as_float(
        item.get("current_confidence"),
        _as_float(item.get("confidence_score"), _as_float(item.get("avg_score"), 0.5)),
    )
    brevity_bonus = max(0.0, 0.10 - min(token_cost, 240) / 2400)
    artifact_bonus, artifact_components = _artifact_density_bonus(item, section)
    source_bonus, source_components = _source_alignment_bonus(
        item,
        section,
        source_tokens=source_tokens,
        source_paths=source_paths,
    )
    shape_bonus, shape_components = _task_shape_bonus(
        section,
        task_type=task_type,
        execution_mode=execution_mode,
        risk_level=risk_level,
    )
    _, compact_components = _playbook_compaction_bonus(item, section)
    freshness_bonus, freshness_components = _freshness_bonus(item)
    _, budget_components = _budget_fit_bonus(
        token_cost=token_cost,
        budget_pressure=budget_pressure,
        max_context_tokens=max_context_tokens,
    )

    components = {
        "similarity": similarity * 0.42,
        "confidence": confidence * 0.18,
        "task_overlap": overlap * 0.14,
        "brevity": brevity_bonus,
        **budget_components,
        "section_bias": _section_bias(section),
        "skill_bias": _skill_bonus(item, skill_name),
        "project_bias": _project_bonus(item, project_id),
        **artifact_components,
        **source_components,
        **shape_components,
        **compact_components,
        **freshness_components,
    }
    historical_bonus, historical_components = _historical_effectiveness_bonus(
        section,
        components=components,
        effectiveness_profile=effectiveness_profile,
        max_reason_adjustments=max_learned_reason_adjustments,
    )
    if historical_components:
        components.update(historical_components)
    score = round(sum(components.values()), 4)
    return score, components, _top_utility_reasons(components)


def _dedupe_key(item: dict[str, Any], section: str) -> str:
    identifier = item.get("id") or item.get("title") or item.get("objective") or item.get("output_summary")
    if not identifier:
        identifier = json.dumps(_evidence_payload(item, section), ensure_ascii=False, default=str)
    return f"{section}:{str(identifier).strip().lower()}"


def _scored_items(
    items: list[dict[str, Any]],
    section: str,
    *,
    budget_pressure: float,
    max_context_tokens: int,
    task_tokens: set[str],
    source_tokens: set[str],
    source_paths: list[str],
    task_type: str,
    execution_mode: str,
    risk_level: str,
    skill_name: str,
    project_id: str | None,
    effectiveness_profile: dict[str, Any] | None,
    max_learned_reason_adjustments: int,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        dedupe = _dedupe_key(item, section)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        enriched = dict(item)
        if section == "playbooks":
            compactness_profile = _playbook_compactness_metrics(enriched)
            enriched["_compactness_score"] = compactness_profile["compactness_score"]
            enriched["_compactness_profile"] = compactness_profile
        payload = _evidence_payload(item, section)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        token_cost = max(1, estimate_tokens(payload))
        score, profile, reasons = _utility_score(
            item,
            section,
            text=text,
            token_cost=token_cost,
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_name,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=max_learned_reason_adjustments,
        )
        enriched["_utility_score"] = score
        enriched["_utility_profile"] = profile
        enriched["_utility_reasons"] = reasons
        enriched["_token_cost"] = token_cost
        scored.append(enriched)
    scored.sort(
        key=lambda item: (
            item.get("_utility_score", 0.0),
            _as_float(item.get("_similarity"), 0.0),
            _as_float(item.get("current_confidence"), 0.0),
        ),
        reverse=True,
    )
    return scored


def _render_experience(exp: dict[str, Any], prefix: str | None = None) -> list[str]:
    heading = exp.get("title") or exp.get("name") or "untitled"
    label = exp.get("category", "unknown")
    similarity = exp.get("_similarity")
    utility = exp.get("_utility_score")
    if prefix:
        line = f"- {prefix} [{label}] {heading}"
    else:
        line = f"- [{label}] {heading}"
    if similarity is not None:
        line += f" (sim={_as_float(similarity):.2f})"
    if utility is not None:
        line += f" [u={_as_float(utility):.2f}]"
    freshness_state = exp.get("_freshness_state")
    freshness_score = exp.get("_freshness_score")
    if freshness_state and freshness_score is not None:
        line += f" [fresh={_as_float(freshness_score):.2f}:{freshness_state}]"

    lines = [line]
    content = _normalize_content(exp.get("content"))
    if content.get("conclusion"):
        lines.append(f"  conclusion: {_clip_text(content['conclusion'])}")
    if content.get("context"):
        lines.append(f"  context: {_clip_text(content['context'])}")
    applicability = exp.get("applicability")
    if isinstance(applicability, dict) and applicability.get("when"):
        lines.append(f"  use when: {_clip_text(applicability['when'])}")
    not_applicable = exp.get("not_applicable_cases")
    if isinstance(not_applicable, dict) and not_applicable.get("when_not"):
        lines.append(f"  do not use when: {_clip_text(not_applicable['when_not'])}")
    return lines


def _render_active_pattern(pattern: dict[str, Any]) -> list[str]:
    lines = _render_experience(pattern, prefix="PATTERN")
    status = pattern.get("status")
    if status:
        lines.append(f"  status: {status}")
    avg_score = pattern.get("avg_score")
    if avg_score is not None:
        lines.append(f"  avg score: {_as_float(avg_score):.2f}")
    experience_count = pattern.get("experience_count")
    if experience_count is not None:
        lines.append(f"  evidence count: {int(experience_count)}")
    context_diversity = pattern.get("context_diversity")
    if context_diversity is not None:
        lines.append(f"  context diversity: {int(context_diversity)}")
    evidence_ids = list(pattern.get("evidence_ids") or [])
    if evidence_ids:
        lines.append(f"  evidence ids: {' | '.join(str(value) for value in evidence_ids[:4])}")
    return lines


def _render_playbook(playbook: dict[str, Any]) -> list[str]:
    similarity = _as_float(playbook.get("_similarity"), 0.0)
    utility = _as_float(playbook.get("_utility_score"), 0.0)
    freshness_state = playbook.get("_freshness_state")
    freshness_score = _as_float(playbook.get("_freshness_score"), 0.0)
    compactness = playbook.get("_compactness_score")
    line = f"- [playbook] {playbook.get('title', 'untitled')} (sim={similarity:.2f}) [u={utility:.2f}]"
    if freshness_state:
        line += f" [fresh={freshness_score:.2f}:{freshness_state}]"
    if compactness is not None:
        line += f" [compact={_as_float(compactness):.2f}]"
    lines = [line]
    if playbook.get("objective"):
        lines.append(f"  objective: {_clip_text(playbook['objective'])}")
    if playbook.get("output_summary"):
        lines.append(f"  summary: {_clip_text(playbook['output_summary'])}")
    commands = playbook.get("commands") or []
    if commands:
        lines.append(f"  commands: {' | '.join(str(cmd) for cmd in commands[:3])}")
    files_touched = playbook.get("files_touched") or []
    if files_touched:
        lines.append(f"  files: {' | '.join(str(path) for path in files_touched[:4])}")
    if playbook.get("reusable_when"):
        lines.append(f"  reuse when: {_clip_text(playbook['reusable_when'])}")
    return lines


def _render_code_graph_hit(item: dict[str, Any]) -> list[str]:
    utility = _as_float(item.get("_utility_score"), 0.0)
    score = _as_float(item.get("score"), 0.0)
    relative_path = item.get("relative_path") or "unknown"
    node_kind = item.get("node_kind") or "symbol"
    qualified_name = item.get("qualified_name") or item.get("title") or "unknown"
    line_start = item.get("line_start")
    line_end = item.get("line_end")
    locator = f"{relative_path}:{line_start or '?'}"
    if line_end and line_end != line_start:
        locator += f"-{line_end}"
    line = f"- [code_graph] {node_kind} {qualified_name} @ {locator} [score={score:.2f}] [u={utility:.2f}]"
    lines = [line]
    signature = str(item.get("signature") or "").strip()
    if signature:
        lines.append(f"  signature: {_clip_text(signature, limit=160)}")
    content = _normalize_content(item.get("content"))
    context = content.get("context")
    if context:
        lines.append(f"  context: {_clip_text(context, limit=180)}")
    refs = list(item.get("evidence_refs") or [])
    if refs:
        ref_parts = []
        for ref in refs[:2]:
            if isinstance(ref, dict):
                ref_parts.append(f"{ref.get('path')}:{ref.get('line_start') or '?'}")
            else:
                ref_parts.append(str(ref))
        ref_parts = [part for part in ref_parts if part and not part.startswith("None")]
        if ref_parts:
            lines.append(f"  read: {' | '.join(ref_parts)}")
    return lines


def _render_knowledge_library_hit(item: dict[str, Any]) -> list[str]:
    knowledge = dict(item.get("knowledge_library") or {})
    similarity = _as_float(item.get("_similarity"), 0.0)
    utility = _as_float(item.get("_utility_score"), 0.0)
    freshness_state = item.get("_freshness_state")
    freshness_score = _as_float(item.get("_freshness_score"), 0.0)
    doc_title = knowledge.get("document_title") or item.get("title") or "knowledge document"
    section_heading = knowledge.get("section_heading")
    mode = knowledge.get("mode") or "summary_first"
    matched_methodology = str(knowledge.get("matched_methodology_slug") or "").strip()
    line = f"- [knowledge] {doc_title} (sim={similarity:.2f}) [u={utility:.2f}] [mode={mode}]"
    if matched_methodology:
        line += f" [method={matched_methodology}]"
    if freshness_state:
        line += f" [fresh={freshness_score:.2f}:{freshness_state}]"
    lines = [line]
    if section_heading:
        lines.append(f"  section: {_clip_text(section_heading)}")
    content = _normalize_content(item.get("content"))
    conclusion = content.get("conclusion")
    if conclusion:
        lines.append(f"  guidance: {_clip_text(conclusion, limit=220)}")
    context = content.get("context")
    if context:
        lines.append(f"  citation: {_clip_text(context, limit=180)}")
    evidence_refs = list(item.get("evidence_refs") or [])
    if evidence_refs:
        refs = []
        for ref in evidence_refs[:3]:
            if isinstance(ref, dict):
                refs.append(str(ref.get("path") or ref.get("file") or ref.get("section") or "").strip())
            else:
                refs.append(str(ref).strip())
        refs = [ref for ref in refs if ref]
        if refs:
            lines.append(f"  refs: {' | '.join(refs)}")
    return lines


@dataclass
class CompiledState:
    session_id: str
    project_name: str
    project_scope: str
    task_brief: dict[str, Any]
    skill_selected: str
    skill_status: str
    dispatch_confidence: float
    dispatch_method: str
    auto_dispatch_result: dict[str, Any] | None
    retrieval_mode: str
    retrieval_latency_ms: int
    selected_items: dict[str, list[dict[str, Any]]]
    warnings: list[str]
    raw_counts: dict[str, int]
    selected_counts: dict[str, int]
    dropped_counts: dict[str, int]
    token_budget: int
    estimated_tokens: int
    was_truncated: bool
    knowledge_library_mode: str = "disabled"
    knowledge_library_metadata: dict[str, Any] | None = None
    effectiveness_profile: dict[str, Any] | None = None
    adaptive_section_limits: dict[str, int] | None = None
    section_token_targets: dict[str, int] | None = None
    selection_strategy: str = "utility_scored_budgeted_v9"

    def to_metadata(self) -> dict[str, Any]:
        budget_fill_ratio = round(self.estimated_tokens / max(1, self.token_budget), 4)
        return {
            "selection_strategy": self.selection_strategy,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "budget_fill_ratio": budget_fill_ratio,
            "was_truncated": self.was_truncated,
            "knowledge_library_mode": self.knowledge_library_mode,
            "knowledge_library": dict(self.knowledge_library_metadata or {}),
            "raw_counts": self.raw_counts,
            "selected_counts": self.selected_counts,
            "dropped_counts": self.dropped_counts,
            "learning_profile_summary": _summarize_learning_profile(self.effectiveness_profile),
            "adaptive_section_limits": dict(self.adaptive_section_limits or {}),
            "section_token_targets": dict(self.section_token_targets or {}),
            "selected_items_summary": {
                section: [
                    {
                        **{
                            "id": str(item.get("id")) if item.get("id") is not None else None,
                            "title": item.get("title")
                            or item.get("name")
                            or item.get("objective")
                            or item.get("output_summary"),
                            "utility_score": item.get("_utility_score"),
                            "token_cost": item.get("_token_cost"),
                            "freshness_score": item.get("_freshness_score"),
                            "freshness_state": item.get("_freshness_state"),
                            "utility_reasons": list(item.get("_utility_reasons") or [])[:4],
                        },
                        **(
                            {
                                "compactness_score": _as_float(item.get("_compactness_score"), 0.0),
                                "compactness_profile": {
                                    "coverage_score": _as_float(
                                        (item.get("_compactness_profile") or {}).get("coverage_score"),
                                        0.0,
                                    ),
                                    "output_fit": _as_float(
                                        (item.get("_compactness_profile") or {}).get("output_fit"),
                                        0.0,
                                    ),
                                    "validation_fit": _as_float(
                                        (item.get("_compactness_profile") or {}).get("validation_fit"),
                                        0.0,
                                    ),
                                    "execution_fit": _as_float(
                                        (item.get("_compactness_profile") or {}).get("execution_fit"),
                                        0.0,
                                    ),
                                    "bloat_penalty": _as_float(
                                        (item.get("_compactness_profile") or {}).get("bloat_penalty"),
                                        0.0,
                                    ),
                                },
                            }
                            if section == "playbooks"
                            else {}
                        ),
                        **(
                            {
                                "relative_path": item.get("relative_path"),
                                "language": item.get("language"),
                                "node_kind": item.get("node_kind"),
                                "qualified_name": item.get("qualified_name"),
                                "line_start": item.get("line_start"),
                                "line_end": item.get("line_end"),
                                "score": _as_float(item.get("score"), 0.0),
                            }
                            if section == "code_graph"
                            else {}
                        ),
                        **(
                            {
                                "document_title": (item.get("knowledge_library") or {}).get("document_title"),
                                "section_heading": (item.get("knowledge_library") or {}).get("section_heading"),
                                "mode": (item.get("knowledge_library") or {}).get("mode"),
                                "page_start": (item.get("knowledge_library") or {}).get("page_start"),
                                "page_end": (item.get("knowledge_library") or {}).get("page_end"),
                            }
                            if section == "knowledge_library"
                            else {}
                        ),
                        **(
                            {
                                "status": item.get("status"),
                                "avg_score": _as_float(item.get("avg_score"), 0.0)
                                if item.get("avg_score") is not None
                                else None,
                                "experience_count": int(item.get("experience_count") or 0)
                                if item.get("experience_count") is not None
                                else None,
                                "context_diversity": int(item.get("context_diversity") or 0)
                                if item.get("context_diversity") is not None
                                else None,
                                "evidence_ids": [str(value) for value in list(item.get("evidence_ids") or [])[:4]],
                            }
                            if section == "active_patterns"
                            else {}
                        ),
                    }
                    for item in self.selected_items.get(section, [])
                ]
                for section in _SECTION_ORDER
            },
        }

    def to_context_block(self) -> str:
        selected_nonzero = {key: value for key, value in self.selected_counts.items() if value}
        dropped_nonzero = {key: value for key, value in self.dropped_counts.items() if value}
        lines = [
            f"# MCUM context - session {self.session_id[:8]}",
            f"Project: {self.project_name}",
            f"Objective: {self.task_brief.get('objective', '')}",
            f"Deliverable: {self.task_brief.get('expected_deliverable', '')}",
            f"Success criteria: {self.task_brief.get('success_criteria', '')}",
            f"Execution mode: {self.task_brief.get('execution_mode', 'ejecutar')} | Risk: {self.task_brief.get('risk_level', 'medio')}",
            f"Selected skill: {self.skill_selected} (confidence: {self.dispatch_confidence:.2f})",
            f"Skill status: {self.skill_status}",
            f"Selection method: {self.dispatch_method}",
            f"Retrieval mode: {self.retrieval_mode} [{self.project_scope}] in {self.retrieval_latency_ms}ms",
            (
                "State compiler: "
                f"{self.selection_strategy} | tokens={self.estimated_tokens}/{self.token_budget} | "
                f"selected={selected_nonzero} | dropped={dropped_nonzero}"
            ),
            "",
        ]

        if self.knowledge_library_mode != "disabled" or self.raw_counts.get("knowledge_library", 0):
            knowledge_meta = dict(self.knowledge_library_metadata or {})
            retrieval_meta = dict(knowledge_meta.get("metadata") or {})
            route_plan = dict(retrieval_meta.get("route_plan") or {})
            conflict_profile = dict(route_plan.get("conflict_profile") or {})
            lines.insert(
                10,
                "Knowledge library: "
                f"{self.knowledge_library_mode} | hits={knowledge_meta.get('hits_retrieved', 0)} "
                f"| tokens~{knowledge_meta.get('tokens_used_estimate', 0)}",
            )
            top_methodologies = ", ".join(str(item) for item in (route_plan.get("top_methodologies") or []) if item)
            if top_methodologies:
                lines.insert(11, f"Knowledge route: methodologies={top_methodologies}")
            methodology_lenses = dict(route_plan.get("methodology_lenses") or {})
            compact_lenses: list[str] = []
            for methodology in route_plan.get("top_methodologies") or []:
                lens_items = list(methodology_lenses.get(methodology) or [])
                if lens_items:
                    compact_lenses.append(f"{methodology}: {lens_items[0]}")
            if compact_lenses:
                lines.insert(12, "Knowledge lenses: " + " | ".join(compact_lenses[:2]))
            if conflict_profile.get("active"):
                conflict_methods = ", ".join(str(item) for item in (conflict_profile.get("methodologies") or []) if item)
                summary = str(conflict_profile.get("summary") or "").strip()
                lines.insert(13, f"Knowledge conflict: {conflict_methods or 'multi-method'}")
                if summary:
                    lines.insert(14, f"Conflict guidance: {_clip_text(summary, limit=180)}")

        trace_fields = {
            "Task ID": self.task_brief.get("task_id"),
            "Primary metric": self.task_brief.get("primary_metric"),
            "Baseline": self.task_brief.get("baseline"),
            "Target": self.task_brief.get("target"),
            "Editable scope": self.task_brief.get("editable_scope"),
            "Read-only scope": self.task_brief.get("read_only_scope"),
            "Protected scope": self.task_brief.get("protected_scope"),
            "Iteration budget": self.task_brief.get("iteration_budget"),
        }
        trace_lines = [f"{label}: {value}" for label, value in trace_fields.items() if value not in (None, "", [], {})]
        if trace_lines:
            lines.extend(trace_lines)
            lines.append("")

        learning_summary = _summarize_learning_profile(self.effectiveness_profile)
        if learning_summary:
            lines.insert(
                10,
                "Historical context learning: "
                f"samples={learning_summary.get('sample_count', 0)} "
                f"scope={learning_summary.get('scope', 'none')} "
                f"sections={learning_summary.get('section_adjustments', {})}",
            )
        if self.adaptive_section_limits or self.section_token_targets:
            lines.insert(
                11,
                "Adaptive section plan: "
                f"limits={dict(self.adaptive_section_limits or {})} "
                f"tokens={dict(self.section_token_targets or {})}",
            )

        if self.auto_dispatch_result and self.auto_dispatch_result.get("skill_name") != self.skill_selected:
            lines.insert(
                8,
                "Auto-dispatch shadow: "
                f"{self.auto_dispatch_result.get('skill_name')} "
                f"({self.auto_dispatch_result.get('match_method')}, "
                f"{_as_float(self.auto_dispatch_result.get('confidence'), 0.0):.2f})",
            )

        sources = list(self.task_brief.get("sources_to_review") or [])
        if sources:
            lines.append("## Sources to Review:")
            for source in sources:
                lines.append(f"- {source}")
            lines.append("")

        constraints = list(self.task_brief.get("constraints") or [])
        if constraints:
            lines.append("## Constraints:")
            for constraint in constraints:
                lines.append(f"- {constraint}")
            lines.append("")

        playbooks = self.selected_items.get("playbooks") or []
        if playbooks:
            lines.append(f"## Session playbooks ({len(playbooks)}):")
            for playbook in playbooks:
                lines.extend(_render_playbook(playbook))
            lines.append("")

        experiences = self.selected_items.get("experiences") or []
        if experiences:
            lines.append(f"## Retrieved experiences ({len(experiences)}):")
            for exp in experiences:
                lines.extend(_render_experience(exp))
            lines.append("")

        code_graph_hits = self.selected_items.get("code_graph") or []
        if code_graph_hits:
            lines.append(f"## Code graph ({len(code_graph_hits)}):")
            for item in code_graph_hits:
                lines.extend(_render_code_graph_hit(item))
            lines.append("")

        knowledge_library_hits = self.selected_items.get("knowledge_library") or []
        if knowledge_library_hits:
            lines.append(f"## Knowledge library ({len(knowledge_library_hits)}):")
            for item in knowledge_library_hits:
                lines.extend(_render_knowledge_library_hit(item))
            lines.append("")

        failure_patterns = self.selected_items.get("failure_patterns") or []
        if failure_patterns:
            lines.append(f"## Failure patterns ({len(failure_patterns)}):")
            for exp in failure_patterns:
                lines.extend(_render_experience(exp, prefix="RISK"))
            lines.append("")

        active_patterns = self.selected_items.get("active_patterns") or []
        if active_patterns:
            lines.append(f"## Active patterns ({len(active_patterns)}):")
            for pattern in active_patterns:
                lines.extend(_render_active_pattern(pattern))
            lines.append("")

        conflict_cases = self.selected_items.get("conflict_cases") or []
        if conflict_cases:
            lines.append(f"## Conflicts ({len(conflict_cases)}):")
            for exp in conflict_cases:
                lines.extend(_render_experience(exp, prefix="CONFLICT"))
            lines.append("")

        if self.warnings:
            lines.append("## Warnings:")
            for warning in self.warnings:
                lines.append(f"- {warning}")

        return "\n".join(line for line in lines if line is not None).strip()


def compile_state(
    *,
    session_id: str,
    project_name: str,
    project_id: str | None,
    project_scope: str,
    task_description: str,
    task_brief: dict[str, Any],
    skill_selected: str,
    skill_status: str,
    dispatch_confidence: float,
    dispatch_method: str,
    auto_dispatch_result: dict[str, Any] | None,
    retrieval_mode: str,
    retrieval_latency_ms: int,
    experiences: list[dict[str, Any]] | None,
    failure_patterns: list[dict[str, Any]] | None,
    conflict_cases: list[dict[str, Any]] | None,
    playbooks: list[dict[str, Any]] | None,
    warnings: list[str] | None,
    execution_policy: dict[str, Any] | None,
    effectiveness_profile: dict[str, Any] | None = None,
    active_patterns: list[dict[str, Any]] | None = None,
    code_graph_hits: list[dict[str, Any]] | None = None,
    knowledge_library_hits: list[dict[str, Any]] | None = None,
    knowledge_library_mode: str = "disabled",
    knowledge_library_metadata: dict[str, Any] | None = None,
) -> CompiledState:
    policy = _normalize_policy(execution_policy)
    max_context_tokens = int(policy["max_context_tokens"])
    task_tokens = _tokenize(task_description) | _tokenize(task_brief.get("objective"))
    source_paths = [str(path) for path in (task_brief.get("sources_to_review") or []) if str(path).strip()]
    source_tokens = set().union(*(_path_tokens(path) for path in source_paths)) if source_paths else set()
    task_type = str(task_brief.get("task_type") or "")
    execution_mode = str(task_brief.get("execution_mode") or "ejecutar")
    risk_level = str(task_brief.get("risk_level") or "medio")
    resolved_active_patterns = (
        active_patterns if active_patterns is not None else list(task_brief.get("active_patterns") or [])
    )
    selected_items: dict[str, list[dict[str, Any]]] = {section: [] for section in _SECTION_ORDER}
    raw_counts = {
        "experiences": len(experiences or []),
        "code_graph": len(code_graph_hits or []),
        "knowledge_library": len(knowledge_library_hits or []),
        "active_patterns": len(resolved_active_patterns or []),
        "failure_patterns": len(failure_patterns or []),
        "conflict_cases": len(conflict_cases or []),
        "playbooks": len(playbooks or []),
        "warnings": len(warnings or []),
    }

    compiled_brief = dict(task_brief)
    compiled_brief["sources_to_review"] = list(task_brief.get("sources_to_review") or [])[
        : int(policy["max_sources_to_review"])
    ]
    compiled_brief["constraints"] = list(task_brief.get("constraints") or [])[
        : int(policy["max_constraints"])
    ]
    compiled_warnings = list(warnings or [])[: int(policy["max_warnings"])]

    fixed_context = CompiledState(
        session_id=session_id,
        project_name=project_name,
        project_scope=project_scope,
        task_brief=compiled_brief,
        skill_selected=skill_selected,
        skill_status=skill_status,
        dispatch_confidence=dispatch_confidence,
        dispatch_method=dispatch_method,
        auto_dispatch_result=auto_dispatch_result,
        retrieval_mode=retrieval_mode,
        retrieval_latency_ms=retrieval_latency_ms,
        selected_items=selected_items,
        warnings=compiled_warnings,
        raw_counts=raw_counts,
        selected_counts={section: 0 for section in _SECTION_ORDER} | {"warnings": len(compiled_warnings)},
        dropped_counts={section: 0 for section in _SECTION_ORDER} | {"warnings": max(0, raw_counts["warnings"] - len(compiled_warnings))},
        token_budget=max_context_tokens,
        estimated_tokens=0,
        was_truncated=False,
        knowledge_library_mode=knowledge_library_mode,
        knowledge_library_metadata=dict(knowledge_library_metadata or {}),
        effectiveness_profile=effectiveness_profile,
        adaptive_section_limits={
            section: max(0, int(policy[_SECTION_LIMIT_KEYS[section]]))
            for section in _SECTION_ORDER
        },
        section_token_targets={section: 0 for section in _SECTION_ORDER},
    )
    fixed_tokens = estimate_tokens(fixed_context.to_context_block())
    remaining_tokens = max(0, max_context_tokens - fixed_tokens)
    budget_pressure = 0.0 if max_context_tokens <= 0 else max(0.0, min(1.0, 1.0 - (remaining_tokens / max_context_tokens)))

    scored_by_section = {
        "experiences": _scored_items(
            experiences or [],
            "experiences",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "active_patterns": _scored_items(
            resolved_active_patterns or [],
            "active_patterns",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "code_graph": _scored_items(
            code_graph_hits or [],
            "code_graph",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "knowledge_library": _scored_items(
            knowledge_library_hits or [],
            "knowledge_library",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "failure_patterns": _scored_items(
            failure_patterns or [],
            "failure_patterns",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "conflict_cases": _scored_items(
            conflict_cases or [],
            "conflict_cases",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
        "playbooks": _scored_items(
            playbooks or [],
            "playbooks",
            budget_pressure=budget_pressure,
            max_context_tokens=max_context_tokens,
            task_tokens=task_tokens,
            source_tokens=source_tokens,
            source_paths=source_paths,
            task_type=task_type,
            execution_mode=execution_mode,
            risk_level=risk_level,
            skill_name=skill_selected,
            project_id=project_id,
            effectiveness_profile=effectiveness_profile,
            max_learned_reason_adjustments=int(policy.get("max_learned_reason_adjustments") or 3),
        ),
    }
    adaptive_section_limits, section_token_targets, section_plan = _build_section_selection_plan(
        policy=policy,
        raw_counts=raw_counts,
        scored_by_section=scored_by_section,
        effectiveness_profile=effectiveness_profile,
        remaining_tokens=remaining_tokens,
    )
    section_token_usage = {section: 0 for section in _SECTION_ORDER}

    for section_index, section in enumerate(_SECTION_ORDER):
        limit = int(adaptive_section_limits.get(section, 0))
        reserve_for_later = 0
        for later_section in _SECTION_ORDER[section_index + 1 :]:
            limit_for_later = int(adaptive_section_limits.get(later_section, 0))
            if limit_for_later <= 0:
                continue
            candidates = scored_by_section.get(later_section) or []
            if not candidates:
                continue
            target = int(section_token_targets.get(later_section, 0) or 0)
            reserve_for_later += max(int(candidates[0].get("_token_cost") or 0), min(target, remaining_tokens))
        for item in scored_by_section[section]:
            if len(selected_items[section]) >= limit:
                break
            token_cost = int(item.get("_token_cost") or 0)
            available_for_section = max(0, remaining_tokens - reserve_for_later)
            section_target = int(section_token_targets.get(section, 0) or 0)
            later_sections_pending = any(
                int(adaptive_section_limits.get(later_section, 0)) > len(selected_items.get(later_section, []))
                and bool(scored_by_section.get(later_section))
                for later_section in _SECTION_ORDER[_SECTION_ORDER.index(section) + 1 :]
            )
            if selected_items[section] and token_cost > remaining_tokens:
                continue
            if available_for_section > 0 and token_cost > available_for_section:
                continue
            if not selected_items[section] and token_cost > remaining_tokens and remaining_tokens > 0:
                continue
            if (
                section_target > 0
                and selected_items[section]
                and later_sections_pending
                and section_token_usage[section] + token_cost > section_target
            ):
                continue
            selected_items[section].append(item)
            section_token_usage[section] += token_cost
            remaining_tokens = max(0, remaining_tokens - token_cost)
            if remaining_tokens == 0:
                break

    selected_counts = {section: len(items) for section, items in selected_items.items()}
    selected_counts["warnings"] = len(compiled_warnings)
    dropped_counts = {
        section: max(0, raw_counts.get(section, 0) - selected_counts.get(section, 0))
        for section in raw_counts
    }
    estimated_tokens = fixed_tokens + sum(
        int(item.get("_token_cost") or 0)
        for items in selected_items.values()
        for item in items
    )
    was_truncated = any(dropped_counts.values())
    return CompiledState(
        session_id=session_id,
        project_name=project_name,
        project_scope=project_scope,
        task_brief=compiled_brief,
        skill_selected=skill_selected,
        skill_status=skill_status,
        dispatch_confidence=dispatch_confidence,
        dispatch_method=dispatch_method,
        auto_dispatch_result=auto_dispatch_result,
        retrieval_mode=retrieval_mode,
        retrieval_latency_ms=retrieval_latency_ms,
        selected_items=selected_items,
        warnings=compiled_warnings,
        raw_counts=raw_counts,
        selected_counts=selected_counts,
        dropped_counts=dropped_counts,
        token_budget=int(policy["max_context_tokens"]),
        estimated_tokens=estimated_tokens,
        was_truncated=was_truncated,
        knowledge_library_mode=knowledge_library_mode,
        knowledge_library_metadata=dict(knowledge_library_metadata or {}),
        effectiveness_profile=effectiveness_profile,
        adaptive_section_limits=section_plan.get("adaptive_limits", adaptive_section_limits),
        section_token_targets=section_plan.get("section_token_targets", section_token_targets),
    )
