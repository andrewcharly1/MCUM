"""
Soft memory-governance helpers for MCUM retrieval and playbook reuse.
"""

from __future__ import annotations

import json
from statistics import mean
from typing import Any


DEFAULT_MEMORY_GOVERNOR = {
    "enabled": True,
    "mode": "assist",
    "preserve_at_least": 1,
    "assist_bonus_weight": 0.08,
    "assist_penalty_weight": 0.12,
    "same_project_bonus": 0.05,
    "cross_project_risk": 0.06,
    "verbosity_soft_cap": 720,
    "cold_risk_threshold": 0.42,
    "quarantine_risk_threshold": 0.62,
    "quarantine_confidence_threshold": 0.42,
    "quarantine_states_filterable": ["quarantined"],
    "never_filter_item_kinds": ["active_pattern"],
    "adaptive_soft_filter_enabled": True,
    "adaptive_soft_filter_item_kinds": ["playbook", "failure_pattern", "conflict_case"],
    "adaptive_soft_filter_states_filterable": ["quarantined"],
    "adaptive_soft_filter_min_items": 3,
    "adaptive_soft_filter_min_filterable_items": 1,
    "adaptive_soft_filter_min_safe_items": 1,
    "adaptive_soft_filter_state_ratio_threshold": 0.34,
}

_STATE_PRIORITY = {
    "hot": 3,
    "warm": 2,
    "cold": 1,
    "quarantined": 0,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _text_length(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _resolve_governor_config(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = dict(DEFAULT_MEMORY_GOVERNOR)
    overrides = dict((policy or {}).get("memory_governor") or {})
    resolved.update(overrides)
    mode = str(resolved.get("mode") or "assist").strip().lower()
    if mode not in {"off", "shadow", "assist", "soft_filter"}:
        mode = "assist"
    resolved["mode"] = mode
    resolved["enabled"] = bool(resolved.get("enabled", True)) and mode != "off"
    resolved["preserve_at_least"] = max(0, _safe_int(resolved.get("preserve_at_least"), 1))
    return resolved


def _same_project(item: dict[str, Any], active_project_id: str | None) -> bool:
    if not active_project_id:
        return False
    project_id = str(item.get("project_id") or "").strip()
    return bool(project_id and project_id == str(active_project_id))


def _experience_like_scores(
    item: dict[str, Any],
    *,
    active_project_id: str | None,
    config: dict[str, Any],
) -> tuple[float, float, list[str]]:
    base_score = _safe_float(item.get("_combined_score"), _safe_float(item.get("_similarity")))
    confidence = _safe_float(
        item.get("current_confidence"),
        _safe_float(item.get("avg_score"), _safe_float(item.get("confidence_score"), 0.0)),
    )
    revalidation = min(1.0, _safe_int(item.get("revalidation_count")) / 3)
    unique_context = min(
        1.0,
        max(_safe_int(item.get("unique_context_count")), _safe_int(item.get("experience_count"))) / 3,
    )
    evidence_bonus = 0.08 if item.get("source_artifacts") else 0.0
    applicability_bonus = 0.05 if item.get("applicability") else 0.0
    not_applicable_bonus = 0.04 if item.get("not_applicable_cases") else 0.0
    feedback_bonus = 0.05 if item.get("_feedback_boost") else 0.0
    same_project_bonus = float(config.get("same_project_bonus") or 0.0) if _same_project(item, active_project_id) else 0.0

    value_score = _clamp(
        (base_score * 0.46)
        + (confidence * 0.22)
        + (revalidation * 0.10)
        + (unique_context * 0.08)
        + evidence_bonus
        + applicability_bonus
        + not_applicable_bonus
        + feedback_bonus
        + same_project_bonus
    )

    content_length = max(_text_length(item.get("content")), _text_length(item.get("title")))
    verbosity_soft_cap = max(160, _safe_int(config.get("verbosity_soft_cap"), 720))
    verbosity_risk = 0.0
    if content_length > verbosity_soft_cap:
        verbosity_risk = min(0.35, (content_length - verbosity_soft_cap) / max(verbosity_soft_cap * 2, 1))
    low_confidence_risk = max(0.0, 0.52 - confidence)
    low_validation_risk = 0.10 if (revalidation == 0.0 and unique_context == 0.0 and not item.get("source_artifacts")) else 0.0
    conflict_risk = 0.08 if item.get("conflict_refs") else 0.0
    cross_project_risk = float(config.get("cross_project_risk") or 0.0) if active_project_id and not _same_project(item, active_project_id) else 0.0

    contamination_risk = _clamp(
        (verbosity_risk * 0.45)
        + (low_confidence_risk * 0.65)
        + low_validation_risk
        + conflict_risk
        + cross_project_risk
    )

    reasons: list[str] = []
    if _same_project(item, active_project_id):
        reasons.append("same_project")
    if revalidation > 0:
        reasons.append("revalidated")
    if unique_context > 0:
        reasons.append("multi_context")
    if item.get("source_artifacts"):
        reasons.append("grounded")
    if item.get("_feedback_boost"):
        reasons.append("feedback_boost")
    if verbosity_risk > 0.0:
        reasons.append("verbose")
    if low_validation_risk > 0.0:
        reasons.append("weak_evidence")
    if cross_project_risk > 0.0:
        reasons.append("cross_project")
    return value_score, contamination_risk, reasons


def _playbook_scores(
    item: dict[str, Any],
    *,
    active_project_id: str | None,
    config: dict[str, Any],
) -> tuple[float, float, list[str]]:
    base_score = _safe_float(item.get("_combined_score"), _safe_float(item.get("_similarity")))
    confidence = _safe_float(item.get("confidence_score"), 0.0)
    reuse_count = _safe_int(item.get("reuse_count"))
    reuse_score = min(1.0, reuse_count / 3)
    compactness = _safe_float(item.get("_compactness_score"))
    validation_bonus = 0.08 if item.get("validation_summary") else 0.0
    reuse_when_bonus = 0.04 if item.get("reusable_when") else 0.0
    same_project_bonus = float(config.get("same_project_bonus") or 0.0) if _same_project(item, active_project_id) else 0.0

    value_score = _clamp(
        (base_score * 0.42)
        + (confidence * 0.16)
        + (reuse_score * 0.16)
        + (compactness * 0.16)
        + validation_bonus
        + reuse_when_bonus
        + same_project_bonus
    )

    output_length = _text_length(item.get("output_summary"))
    validation_length = _text_length(item.get("validation_summary"))
    total_length = output_length + validation_length
    verbosity_soft_cap = max(180, _safe_int(config.get("verbosity_soft_cap"), 720))
    verbosity_risk = 0.0
    if total_length > verbosity_soft_cap:
        verbosity_risk = min(0.35, (total_length - verbosity_soft_cap) / max(verbosity_soft_cap * 2, 1))
    low_reuse_risk = 0.16 if reuse_count <= 0 else 0.06 if reuse_count == 1 else 0.0
    low_confidence_risk = max(0.0, 0.48 - confidence)
    low_compactness_risk = max(0.0, 0.35 - compactness)
    outcome_risk = 0.05 if str(item.get("outcome") or "").strip().lower() == "partial" else 0.0
    cross_project_risk = float(config.get("cross_project_risk") or 0.0) if active_project_id and not _same_project(item, active_project_id) else 0.0

    contamination_risk = _clamp(
        (verbosity_risk * 0.45)
        + (low_reuse_risk * 0.40)
        + (low_confidence_risk * 0.55)
        + (low_compactness_risk * 0.35)
        + outcome_risk
        + cross_project_risk
    )

    reasons: list[str] = []
    if _same_project(item, active_project_id):
        reasons.append("same_project")
    if reuse_count > 1:
        reasons.append("reused")
    if compactness >= 0.75:
        reasons.append("compact")
    if item.get("validation_summary"):
        reasons.append("validated")
    if verbosity_risk > 0.0:
        reasons.append("verbose")
    if low_reuse_risk > 0.0:
        reasons.append("low_reuse")
    if cross_project_risk > 0.0:
        reasons.append("cross_project")
    return value_score, contamination_risk, reasons


def _annotate_item(
    item: dict[str, Any],
    *,
    item_kind: str,
    active_project_id: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(item)
    if item_kind == "playbook":
        value_score, contamination_risk, reasons = _playbook_scores(
            enriched,
            active_project_id=active_project_id,
            config=config,
        )
    else:
        value_score, contamination_risk, reasons = _experience_like_scores(
            enriched,
            active_project_id=active_project_id,
            config=config,
        )

    confidence = _safe_float(
        enriched.get("current_confidence"),
        _safe_float(enriched.get("avg_score"), _safe_float(enriched.get("confidence_score"), 0.0)),
    )
    quarantine_confidence_threshold = float(config.get("quarantine_confidence_threshold") or 0.42)
    cold_risk_threshold = float(config.get("cold_risk_threshold") or 0.42)
    quarantine_risk_threshold = float(config.get("quarantine_risk_threshold") or 0.62)
    if (
        confidence <= quarantine_confidence_threshold
        and (
            contamination_risk >= quarantine_risk_threshold
            or (contamination_risk >= cold_risk_threshold and value_score <= 0.45)
        )
    ):
        state = "quarantined"
    elif contamination_risk >= cold_risk_threshold:
        state = "cold"
    elif value_score >= 0.72 and contamination_risk <= 0.24:
        state = "hot"
    else:
        state = "warm"

    base_score = _safe_float(
        enriched.get("_combined_score"),
        _safe_float(enriched.get("current_confidence"), _safe_float(enriched.get("avg_score"), _safe_float(enriched.get("confidence_score"), 0.0))),
    )
    governor_score = _clamp(
        base_score
        + (_STATE_PRIORITY[state] * float(config.get("assist_bonus_weight") or 0.08) / 3)
        + (value_score * float(config.get("assist_bonus_weight") or 0.08))
        - (contamination_risk * float(config.get("assist_penalty_weight") or 0.12))
    )
    enriched["_memory_value_score"] = round(value_score, 4)
    enriched["_contamination_risk"] = round(contamination_risk, 4)
    enriched["_memory_governor_state"] = state
    enriched["_memory_governor_score"] = round(governor_score, 4)
    enriched["_memory_governor_reasons"] = reasons
    return enriched


def apply_memory_governor(
    items: list[dict[str, Any]],
    *,
    item_kind: str,
    policy: dict[str, Any] | None = None,
    active_project_id: str | None = None,
    preserve_at_least: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = _resolve_governor_config(policy)
    mode = str(config.get("mode") or "assist")
    preserve_floor = max(0, preserve_at_least if preserve_at_least is not None else _safe_int(config.get("preserve_at_least"), 1))

    if not items:
        return [], {
            "enabled": bool(config.get("enabled", True)),
            "mode": mode,
            "effective_mode": mode,
            "item_kind": item_kind,
            "input_count": 0,
            "kept_count": 0,
            "filtered_count": 0,
            "fallback_applied": False,
            "adaptive_filter_applied": False,
            "adaptive_filter_reason": None,
            "states": {"hot": 0, "warm": 0, "cold": 0, "quarantined": 0},
            "avg_value_score": 0.0,
            "avg_contamination_risk": 0.0,
            "warnings": [],
        }

    if not config.get("enabled", True):
        return list(items), {
            "enabled": False,
            "mode": "off",
            "effective_mode": "off",
            "item_kind": item_kind,
            "input_count": len(items),
            "kept_count": len(items),
            "filtered_count": 0,
            "fallback_applied": False,
            "adaptive_filter_applied": False,
            "adaptive_filter_reason": None,
            "states": {"hot": 0, "warm": 0, "cold": 0, "quarantined": 0},
            "avg_value_score": 0.0,
            "avg_contamination_risk": 0.0,
            "warnings": [],
        }

    annotated = [
        _annotate_item(
            item,
            item_kind=item_kind,
            active_project_id=active_project_id,
            config=config,
        )
        for item in items
    ]
    annotated.sort(
        key=lambda item: (
            _safe_float(item.get("_memory_governor_score")),
            _STATE_PRIORITY.get(str(item.get("_memory_governor_state") or "warm"), 0),
            _safe_float(item.get("_combined_score"), _safe_float(item.get("current_confidence"), _safe_float(item.get("avg_score"), _safe_float(item.get("confidence_score"), 0.0)))),
        ),
        reverse=True,
    )

    filtered_count = 0
    fallback_applied = False
    adaptive_filter_applied = False
    adaptive_filter_reason = None
    warnings: list[str] = []
    kept = list(annotated)
    effective_mode = mode
    if mode == "soft_filter" and item_kind not in set(config.get("never_filter_item_kinds") or []):
        filterable_states = {str(value) for value in (config.get("quarantine_states_filterable") or [])}
        filtered = [
            item
            for item in annotated
            if str(item.get("_memory_governor_state") or "warm") not in filterable_states
        ]
        filtered_count = len(annotated) - len(filtered)
        required_to_keep = 0 if preserve_floor <= 0 else max(1, preserve_floor)
        if len(filtered) >= required_to_keep and (filtered or required_to_keep == 0):
            kept = filtered
            if filtered_count:
                warnings.append(
                    f"Memory governor filtered {filtered_count} {item_kind} item(s) flagged as quarantined."
                )
        else:
            fallback_applied = filtered_count > 0
            if fallback_applied:
                warnings.append(
                    f"Memory governor kept fallback {item_kind} items to avoid an empty retrieval set."
                )
    elif (
        mode == "assist"
        and bool(config.get("adaptive_soft_filter_enabled", True))
        and item_kind in set(config.get("adaptive_soft_filter_item_kinds") or [])
        and item_kind not in set(config.get("never_filter_item_kinds") or [])
        and len(annotated) >= max(1, _safe_int(config.get("adaptive_soft_filter_min_items"), 3))
    ):
        filterable_states = {
            str(value)
            for value in (config.get("adaptive_soft_filter_states_filterable") or ["quarantined"])
        }
        filterable = [
            item
            for item in annotated
            if str(item.get("_memory_governor_state") or "warm") in filterable_states
        ]
        filterable_count = len(filterable)
        safe_items = [
            item
            for item in annotated
            if str(item.get("_memory_governor_state") or "warm") not in filterable_states
        ]
        filterable_ratio = filterable_count / max(1, len(annotated))
        min_filterable_items = max(1, _safe_int(config.get("adaptive_soft_filter_min_filterable_items"), 1))
        min_safe_items = max(0, _safe_int(config.get("adaptive_soft_filter_min_safe_items"), 1))
        ratio_threshold = _safe_float(config.get("adaptive_soft_filter_state_ratio_threshold"), 0.34)
        required_to_keep = max(preserve_floor, min_safe_items)
        if (
            filterable_count >= min_filterable_items
            and filterable_ratio >= ratio_threshold
            and len(safe_items) >= required_to_keep
        ):
            kept = safe_items
            filtered_count = filterable_count
            adaptive_filter_applied = filterable_count > 0
            adaptive_filter_reason = "local_quarantine_pressure"
            effective_mode = "assist_plus_local_filter"
            warnings.append(
                f"Memory governor adaptively filtered {filtered_count} {item_kind} item(s) under local quarantine pressure."
            )

    states = {
        state: sum(1 for item in annotated if item.get("_memory_governor_state") == state)
        for state in ("hot", "warm", "cold", "quarantined")
    }
    avg_value_score = round(mean(_safe_float(item.get("_memory_value_score")) for item in annotated), 4)
    avg_contamination_risk = round(mean(_safe_float(item.get("_contamination_risk")) for item in annotated), 4)
    summary = {
        "enabled": True,
        "mode": mode,
        "effective_mode": effective_mode,
        "item_kind": item_kind,
        "input_count": len(items),
        "kept_count": len(kept),
        "filtered_count": filtered_count,
        "fallback_applied": fallback_applied,
        "adaptive_filter_applied": adaptive_filter_applied,
        "adaptive_filter_reason": adaptive_filter_reason,
        "states": states,
        "avg_value_score": avg_value_score,
        "avg_contamination_risk": avg_contamination_risk,
        "warnings": warnings,
    }
    return kept, summary


def summarize_governor_sections(sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_input = sum(_safe_int(section.get("input_count")) for section in sections.values())
    total_kept = sum(_safe_int(section.get("kept_count")) for section in sections.values())
    total_filtered = sum(_safe_int(section.get("filtered_count")) for section in sections.values())
    adaptive_filter_sections = [
        section_name
        for section_name, section in sections.items()
        if bool(section.get("adaptive_filter_applied"))
    ]
    risk_values = [
        _safe_float(section.get("avg_contamination_risk"))
        for section in sections.values()
        if section.get("input_count")
    ]
    value_scores = [
        _safe_float(section.get("avg_value_score"))
        for section in sections.values()
        if section.get("input_count")
    ]
    return {
        "enabled": any(bool(section.get("enabled")) for section in sections.values()),
        "mode": next((section.get("mode") for section in sections.values() if section.get("mode")), "off"),
        "sections": sections,
        "total_input_count": total_input,
        "total_kept_count": total_kept,
        "total_filtered_count": total_filtered,
        "avg_contamination_risk": round(mean(risk_values), 4) if risk_values else 0.0,
        "avg_value_score": round(mean(value_scores), 4) if value_scores else 0.0,
        "fallback_applied": any(bool(section.get("fallback_applied")) for section in sections.values()),
        "adaptive_filter_applied": bool(adaptive_filter_sections),
        "adaptive_filter_sections": adaptive_filter_sections,
    }
