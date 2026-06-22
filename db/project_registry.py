"""
Project registry and immutable log helpers for MCUM.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .connection import get_db, get_cursor
from ..policy import load_execution_policy


def normalize_project_path(project_path: str) -> str:
    return str(Path(project_path)).replace("\\", "/")


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: (str(value) if hasattr(value, "hex") else value) for key, value in row.items()}


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def estimate_tokens(payload: Any) -> int:
    """Cheap token estimate for operational telemetry and retrieval budgets."""
    if payload is None:
        return 0

    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(payload)

    text = text.strip()
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _safe_ratio(numerator: Any, denominator: Any) -> float:
    try:
        num = float(numerator or 0)
        den = float(denominator or 0)
    except (TypeError, ValueError):
        return 0.0
    if den <= 0:
        return 0.0
    return round(num / den, 4)


def _coerce_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            cleaned = value.strip()
            return [cleaned] if cleaned else []
        value = loaded
    if not isinstance(value, list):
        return []

    identifiers: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            candidate = item.get("id") or item.get("pattern_id")
        else:
            candidate = item
        cleaned = str(candidate or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            identifiers.append(cleaned)
    return identifiers


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _effect_score(effectiveness: str, *, selected: bool) -> float:
    if selected:
        return {
            "high": 1.0,
            "medium": 0.65,
            "low": 0.15,
            "miss": -0.70,
        }.get(effectiveness, 0.0)
    return {
        "missed_opportunity": 0.80,
        "near_miss": 0.15,
        "irrelevant": -0.25,
    }.get(effectiveness, 0.0)


def _row_cost_pressure(
    *,
    context_tokens_in: float,
    retrieval_latency_ms: float,
    task_wall_clock_ms: float,
) -> float:
    context_pressure = _clamp((context_tokens_in - 900.0) / 900.0, 0.0, 1.0)
    retrieval_pressure = _clamp((retrieval_latency_ms - 300.0) / 900.0, 0.0, 1.0)
    wall_pressure = _clamp((task_wall_clock_ms - 8000.0) / 24000.0, 0.0, 1.0)
    return round(
        (context_pressure * 0.55) + (retrieval_pressure * 0.20) + (wall_pressure * 0.25),
        4,
    )


def _row_outcome_score(outcome: str) -> float:
    return {
        "success": 1.0,
        "partial": 0.35,
        "failure": -0.70,
    }.get(str(outcome or "").lower(), 0.0)


def derive_context_effectiveness_profile(
    logs: list[dict[str, Any]],
    *,
    skill_name: str,
    task_type: str | None = None,
    execution_mode: str | None = None,
    min_samples: int = 3,
    scope: str = "same_project",
) -> dict[str, Any]:
    section_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "total": 0,
            "selected": 0,
            "selected_helpful": 0,
            "selected_misses": 0,
            "missed_opportunities": 0,
            "score_sum": 0.0,
            "efficiency_sum": 0.0,
            "efficiency_rows": 0,
            "token_share_sum": 0.0,
            "token_cost_sum": 0.0,
            "context_pressure_sum": 0.0,
            "success_sum": 0.0,
        }
    )
    reason_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "score_sum": 0.0})
    matched_logs = 0
    used_rows = 0
    pattern_rows = 0
    pattern_id_total = 0
    pattern_boost_total = 0.0

    for row in logs or []:
        metadata = _json_object(row.get("log_metadata"))
        row_skill = str(row.get("skill_used") or metadata.get("final_skill") or metadata.get("selected_skill") or "")
        if row_skill != skill_name:
            continue

        brief = _json_object(metadata.get("task_brief"))
        if task_type and str(brief.get("task_type") or "") not in {"", task_type}:
            continue
        if execution_mode and str(brief.get("execution_mode") or "") not in {"", execution_mode}:
            continue

        context_effectiveness = _json_object(metadata.get("context_effectiveness"))
        items = list(context_effectiveness.get("items") or [])
        if not items:
            continue
        pattern_ids_used = _coerce_id_list(row.get("pattern_ids_used") or metadata.get("pattern_ids_used"))
        matched_logs += 1
        if pattern_ids_used:
            pattern_rows += 1
            pattern_id_total += len(pattern_ids_used)

        compiled_context = _json_object(metadata.get("compiled_context"))
        selected_items_summary = _json_object(compiled_context.get("selected_items_summary"))
        section_token_costs: dict[str, float] = {}
        total_selected_token_cost = 0.0
        for section_name, entries in selected_items_summary.items():
            if not isinstance(entries, list):
                continue
            token_cost = sum(float(_json_object(entry).get("token_cost") or 0.0) for entry in entries)
            section_token_costs[str(section_name)] = token_cost
            total_selected_token_cost += token_cost

        context_tokens_in = float(row.get("context_tokens_in") or 0.0)
        retrieval_latency_ms = float(row.get("retrieval_latency_ms") or 0.0)
        task_wall_clock_ms = float(row.get("task_wall_clock_ms") or 0.0)
        outcome_score = _row_outcome_score(str(row.get("outcome") or ""))
        cost_pressure = _row_cost_pressure(
            context_tokens_in=context_tokens_in,
            retrieval_latency_ms=retrieval_latency_ms,
            task_wall_clock_ms=task_wall_clock_ms,
        )
        row_section_effect_scores: dict[str, list[float]] = defaultdict(list)

        for item in items:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section") or "").strip()
            if not section:
                continue
            selected = bool(item.get("selected"))
            effectiveness = str(item.get("effectiveness") or "")
            score = _effect_score(effectiveness, selected=selected)
            stats = section_stats[section]
            stats["total"] += 1
            stats["score_sum"] += score
            if selected:
                stats["selected"] += 1
                if effectiveness in {"high", "medium"}:
                    stats["selected_helpful"] += 1
                elif effectiveness in {"low", "miss"}:
                    stats["selected_misses"] += 1
            elif effectiveness == "missed_opportunity":
                stats["missed_opportunities"] += 1
            row_section_effect_scores[section].append(score)

            support_score = float(item.get("support_score") or 0.0)
            weighted_reason_score = score * max(0.25, min(1.0, support_score or 0.0))
            for reason in list(item.get("utility_reasons") or [])[:4]:
                label = str(reason or "").strip()
                if not label:
                    continue
                reason_entry = reason_stats[label]
                reason_entry["count"] += 1
                reason_entry["score_sum"] += weighted_reason_score
            used_rows += 1

        pattern_bonus = _clamp((len(pattern_ids_used) / 3.0) * 0.03, 0.0, 0.03) if pattern_ids_used else 0.0
        for section, effect_scores in row_section_effect_scores.items():
            if not effect_scores:
                continue
            token_cost = float(section_token_costs.get(section, 0.0))
            token_share = token_cost / max(1.0, total_selected_token_cost)
            average_effect = sum(effect_scores) / max(1.0, len(effect_scores))
            efficiency_signal = (
                (average_effect * 0.55)
                + (outcome_score * 0.30)
                - (cost_pressure * token_share * 0.45)
            )
            if pattern_bonus:
                efficiency_signal += pattern_bonus
                pattern_boost_total += pattern_bonus
            stats = section_stats[section]
            stats["efficiency_sum"] += efficiency_signal
            stats["efficiency_rows"] += 1
            stats["token_share_sum"] += token_share
            stats["token_cost_sum"] += token_cost
            stats["context_pressure_sum"] += cost_pressure
            stats["success_sum"] += outcome_score

    section_adjustments: dict[str, float] = {}
    section_summary: dict[str, dict[str, float]] = {}
    efficiency_adjustments: dict[str, float] = {}
    token_target_multipliers: dict[str, float] = {}
    efficiency_summary: dict[str, dict[str, float]] = {}
    for section, stats in section_stats.items():
        if stats["total"] <= 0:
            continue
        helpful_rate = stats["selected_helpful"] / max(1.0, stats["selected"])
        miss_rate = stats["selected_misses"] / max(1.0, stats["selected"])
        missed_rate = stats["missed_opportunities"] / max(1.0, stats["total"])
        mean_score = stats["score_sum"] / max(1.0, stats["total"])
        adjustment = (
            (helpful_rate * 0.07)
            - (miss_rate * 0.06)
            + (missed_rate * 0.05)
            + (mean_score * 0.03)
        )
        section_adjustments[section] = round(_clamp(adjustment, -0.08, 0.10), 4)
        mean_efficiency = stats["efficiency_sum"] / max(1.0, stats["efficiency_rows"])
        mean_token_share = stats["token_share_sum"] / max(1.0, stats["efficiency_rows"])
        mean_context_pressure = stats["context_pressure_sum"] / max(1.0, stats["efficiency_rows"])
        mean_success = stats["success_sum"] / max(1.0, stats["efficiency_rows"])
        efficiency_adjustments[section] = round(_clamp(mean_efficiency * 0.06, -0.06, 0.06), 4)
        token_target_multipliers[section] = round(
            _clamp(1.0 + (mean_efficiency * 0.35) - (mean_token_share * 0.15), 0.70, 1.35),
            4,
        )
        section_summary[section] = {
            "total": int(stats["total"]),
            "selected": int(stats["selected"]),
            "selected_helpful": int(stats["selected_helpful"]),
            "selected_misses": int(stats["selected_misses"]),
            "missed_opportunities": int(stats["missed_opportunities"]),
            "mean_score": round(mean_score, 4),
        }
        efficiency_summary[section] = {
            "rows": int(stats["efficiency_rows"]),
            "mean_efficiency": round(mean_efficiency, 4),
            "mean_token_share": round(mean_token_share, 4),
            "mean_context_pressure": round(mean_context_pressure, 4),
            "mean_success": round(mean_success, 4),
            "token_target_multiplier": token_target_multipliers[section],
        }

    reason_adjustments: dict[str, float] = {}
    for reason, stats in reason_stats.items():
        if stats["count"] < 2:
            continue
        mean_score = stats["score_sum"] / max(1.0, stats["count"])
        adjustment = _clamp(mean_score * 0.05, -0.04, 0.05)
        if abs(adjustment) < 0.005:
            continue
        reason_adjustments[reason] = round(adjustment, 4)

    return {
        "scope": scope,
        "skill_name": skill_name,
        "task_type": task_type,
        "execution_mode": execution_mode,
        "sample_count": matched_logs,
        "items_observed": used_rows,
        "min_samples": int(min_samples),
        "active": matched_logs >= int(min_samples),
        "section_adjustments": section_adjustments,
        "reason_adjustments": reason_adjustments,
        "section_summary": section_summary,
        "efficiency_adjustments": efficiency_adjustments,
        "token_target_multipliers": token_target_multipliers,
        "efficiency_summary": efficiency_summary,
        "pattern_usage_summary": {
            "rows": int(pattern_rows),
            "pattern_ids_used": int(pattern_id_total),
            "mean_pattern_ids_used": round(pattern_id_total / max(1.0, float(pattern_rows)), 4),
            "usage_rate": round(pattern_rows / max(1.0, float(matched_logs)), 4),
            "mean_pattern_boost": round(pattern_boost_total / max(1.0, float(pattern_rows)), 4),
        },
    }


def get_context_effectiveness_profile(
    *,
    project_id: str | None,
    skill_name: str,
    task_type: str | None = None,
    execution_mode: str | None = None,
    limit: int = 60,
    min_samples: int = 3,
    allow_cross_project: bool = True,
) -> dict[str, Any]:
    fetch_limit = max(int(limit), int(min_samples) * 6, 12)

    def _fetch_rows(*, scope_project_id: str | None, exclude_project_id: str | None = None) -> list[dict[str, Any]]:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                clauses = [
                    "log_type = 'task'",
                    "(skill_used = %s OR log_metadata->>'selected_skill' = %s OR log_metadata->>'final_skill' = %s)",
                ]
                params: list[Any] = [skill_name, skill_name, skill_name]
                if scope_project_id:
                    clauses.append("project_id = %s")
                    params.append(scope_project_id)
                if exclude_project_id:
                    clauses.append("project_id <> %s")
                    params.append(exclude_project_id)

                cur.execute(
                    f"""
                    SELECT id, project_id, skill_used, pattern_ids_used, log_metadata, created_at
                         , outcome, context_tokens_in, task_wall_clock_ms, retrieval_latency_ms
                    FROM project_registry.project_logs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params + [fetch_limit],
                )
                return [dict(row) for row in cur.fetchall()]

    same_project_rows = _fetch_rows(scope_project_id=project_id) if project_id else []
    profile = derive_context_effectiveness_profile(
        same_project_rows,
        skill_name=skill_name,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="same_project" if project_id else "cross_project",
    )
    if profile.get("active") or not allow_cross_project:
        return profile

    cross_project_rows = _fetch_rows(scope_project_id=None, exclude_project_id=project_id if project_id else None)
    combined = same_project_rows + cross_project_rows
    blended = derive_context_effectiveness_profile(
        combined,
        skill_name=skill_name,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="blended" if project_id else "cross_project",
    )
    if blended.get("sample_count", 0) > profile.get("sample_count", 0):
        return blended
    return profile


def _resolve_router_skill(metadata: dict[str, Any], actual_skill: str) -> str:
    selected_skill = str(metadata.get("selected_skill") or "").strip()
    dispatch_method = str(metadata.get("dispatch_method") or "").strip()
    auto_dispatch = _json_object(metadata.get("auto_dispatch"))
    auto_skill = str(auto_dispatch.get("skill_name") or "").strip()
    if dispatch_method == "forced_by_user" and auto_skill:
        return auto_skill
    return selected_skill or auto_skill or actual_skill


def derive_dispatch_performance_profile(
    logs: list[dict[str, Any]],
    *,
    task_type: str | None = None,
    execution_mode: str | None = None,
    min_samples: int = 3,
    scope: str = "same_project",
) -> dict[str, Any]:
    skill_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "actual_rows": 0,
            "routed_rows": 0,
            "route_hits": 0,
            "route_misses": 0,
            "corrections_in": 0,
            "corrections_out": 0,
            "outcome_sum": 0.0,
        }
    )
    matched_logs = 0

    for row in logs or []:
        metadata = _json_object(row.get("log_metadata"))
        brief = _json_object(metadata.get("task_brief"))
        if task_type and str(brief.get("task_type") or "") not in {"", task_type}:
            continue
        if execution_mode and str(brief.get("execution_mode") or "") not in {"", execution_mode}:
            continue

        actual_skill = str(
            metadata.get("final_skill")
            or row.get("skill_used")
            or metadata.get("selected_skill")
            or ""
        ).strip()
        routed_skill = _resolve_router_skill(metadata, actual_skill)
        if not actual_skill and not routed_skill:
            continue

        matched_logs += 1
        outcome_score = _row_outcome_score(str(row.get("outcome") or ""))

        if actual_skill:
            actual_stats = skill_stats[actual_skill]
            actual_stats["actual_rows"] += 1
            actual_stats["outcome_sum"] += outcome_score

        if routed_skill:
            routed_stats = skill_stats[routed_skill]
            routed_stats["routed_rows"] += 1

        if actual_skill and routed_skill:
            if actual_skill == routed_skill:
                skill_stats[actual_skill]["route_hits"] += 1
            else:
                skill_stats[routed_skill]["route_misses"] += 1
                skill_stats[routed_skill]["corrections_out"] += 1
                skill_stats[actual_skill]["corrections_in"] += 1

    priority_adjustments: dict[str, float] = {}
    score_adjustments: dict[str, float] = {}
    skill_summary: dict[str, dict[str, float]] = {}
    for skill_name, stats in skill_stats.items():
        actual_rows = int(stats["actual_rows"])
        routed_rows = int(stats["routed_rows"])
        signal_rows = max(
            actual_rows,
            routed_rows,
            int(stats["corrections_in"] + stats["corrections_out"]),
        )
        if signal_rows <= 0:
            continue

        actual_mean = stats["outcome_sum"] / max(1.0, stats["actual_rows"])
        hit_rate = stats["route_hits"] / max(1.0, stats["routed_rows"])
        miss_rate = stats["route_misses"] / max(1.0, stats["routed_rows"])
        correction_balance = (
            (stats["corrections_in"] - stats["corrections_out"])
            / max(1.0, float(signal_rows))
        )
        sample_factor = _clamp(signal_rows / max(2.0, float(min_samples) + 1.0), 0.25, 1.0)
        raw_delta = (
            (actual_mean * 0.95)
            + ((hit_rate - miss_rate) * 0.55)
            + (correction_balance * 0.85)
        )
        priority_delta = _clamp(raw_delta * sample_factor, -1.6, 1.6)
        score_delta = _clamp(priority_delta * 0.03, -0.05, 0.05)

        priority_adjustments[skill_name] = round(priority_delta, 4)
        score_adjustments[skill_name] = round(score_delta, 4)
        skill_summary[skill_name] = {
            "signal_rows": signal_rows,
            "actual_rows": actual_rows,
            "routed_rows": routed_rows,
            "route_hits": int(stats["route_hits"]),
            "route_misses": int(stats["route_misses"]),
            "corrections_in": int(stats["corrections_in"]),
            "corrections_out": int(stats["corrections_out"]),
            "mean_outcome": round(actual_mean, 4),
            "hit_rate": round(hit_rate, 4),
            "miss_rate": round(miss_rate, 4),
            "correction_balance": round(correction_balance, 4),
            "sample_factor": round(sample_factor, 4),
            "priority_delta": round(priority_delta, 4),
            "score_delta": round(score_delta, 4),
        }

    return {
        "scope": scope,
        "task_type": task_type,
        "execution_mode": execution_mode,
        "sample_count": matched_logs,
        "min_samples": int(min_samples),
        "active": matched_logs >= int(min_samples),
        "priority_adjustments": priority_adjustments,
        "score_adjustments": score_adjustments,
        "skill_summary": skill_summary,
    }


def get_dispatch_performance_profile(
    *,
    project_id: str | None,
    task_type: str | None = None,
    execution_mode: str | None = None,
    limit: int = 80,
    min_samples: int = 3,
    allow_cross_project: bool = True,
) -> dict[str, Any]:
    fetch_limit = max(int(limit), int(min_samples) * 8, 16)

    def _fetch_rows(*, scope_project_id: str | None, exclude_project_id: str | None = None) -> list[dict[str, Any]]:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                clauses = ["log_type = 'task'"]
                params: list[Any] = []
                if scope_project_id:
                    clauses.append("project_id = %s")
                    params.append(scope_project_id)
                if exclude_project_id:
                    clauses.append("project_id <> %s")
                    params.append(exclude_project_id)

                cur.execute(
                    f"""
                    SELECT id, project_id, skill_used, log_metadata, created_at,
                           outcome, context_tokens_in, task_wall_clock_ms, retrieval_latency_ms
                    FROM project_registry.project_logs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params + [fetch_limit],
                )
                return [dict(row) for row in cur.fetchall()]

    same_project_rows = _fetch_rows(scope_project_id=project_id) if project_id else []
    profile = derive_dispatch_performance_profile(
        same_project_rows,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="same_project" if project_id else "cross_project",
    )
    if profile.get("active") or not allow_cross_project:
        return profile

    cross_project_rows = _fetch_rows(scope_project_id=None, exclude_project_id=project_id if project_id else None)
    combined = same_project_rows + cross_project_rows
    blended = derive_dispatch_performance_profile(
        combined,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="blended" if project_id else "cross_project",
    )
    if blended.get("sample_count", 0) > profile.get("sample_count", 0):
        return blended
    return profile


def derive_retrieval_scope_profile(
    logs: list[dict[str, Any]],
    *,
    skill_name: str,
    task_type: str | None = None,
    execution_mode: str | None = None,
    min_samples: int = 3,
    scope: str = "same_project",
) -> dict[str, Any]:
    stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "rows": 0,
            "success_sum": 0.0,
            "helpful_rate_sum": 0.0,
            "miss_rate_sum": 0.0,
            "latency_sum": 0.0,
            "tokens_sum": 0.0,
            "score_sum": 0.0,
        }
    )
    matched_logs = 0

    for row in logs or []:
        metadata = _json_object(row.get("log_metadata"))
        row_skill = str(row.get("skill_used") or metadata.get("final_skill") or metadata.get("selected_skill") or "")
        if row_skill != skill_name:
            continue

        brief = _json_object(metadata.get("task_brief"))
        if task_type and str(brief.get("task_type") or "") not in {"", task_type}:
            continue
        if execution_mode and str(brief.get("execution_mode") or "") not in {"", execution_mode}:
            continue

        project_scope = str(metadata.get("project_scope") or "").strip()
        if project_scope not in {"same_project", "cross_project_fallback"}:
            continue

        matched_logs += 1
        effectiveness = _json_object(metadata.get("context_effectiveness"))
        summary = _json_object(effectiveness.get("summary"))
        selected_items = float(summary.get("selected_items") or 0.0)
        high_value_selected = float(summary.get("high_value_selected") or 0.0)
        missed = float(summary.get("missed_opportunities") or 0.0)
        items_evaluated = float(summary.get("items_evaluated") or 0.0)

        helpful_rate = high_value_selected / max(1.0, selected_items)
        miss_rate = missed / max(1.0, items_evaluated)
        outcome_score = _row_outcome_score(str(row.get("outcome") or ""))
        latency = float(row.get("retrieval_latency_ms") or 0.0)
        context_tokens = float(row.get("context_tokens_in") or 0.0)
        latency_penalty = _clamp((latency - 260.0) / 1200.0, 0.0, 1.0) * 0.12
        token_penalty = _clamp((context_tokens - 1100.0) / 1400.0, 0.0, 1.0) * 0.08
        score = (outcome_score * 0.55) + (helpful_rate * 0.30) - (miss_rate * 0.18) - latency_penalty - token_penalty

        scope_stats = stats[project_scope]
        scope_stats["rows"] += 1
        scope_stats["success_sum"] += outcome_score
        scope_stats["helpful_rate_sum"] += helpful_rate
        scope_stats["miss_rate_sum"] += miss_rate
        scope_stats["latency_sum"] += latency
        scope_stats["tokens_sum"] += context_tokens
        scope_stats["score_sum"] += score

    summary_by_scope: dict[str, dict[str, float]] = {}
    for project_scope, scope_stats in stats.items():
        rows = max(1.0, scope_stats["rows"])
        summary_by_scope[project_scope] = {
            "rows": int(scope_stats["rows"]),
            "mean_success": round(scope_stats["success_sum"] / rows, 4),
            "mean_helpful_rate": round(scope_stats["helpful_rate_sum"] / rows, 4),
            "mean_miss_rate": round(scope_stats["miss_rate_sum"] / rows, 4),
            "mean_latency_ms": round(scope_stats["latency_sum"] / rows, 2),
            "mean_context_tokens": round(scope_stats["tokens_sum"] / rows, 2),
            "mean_scope_score": round(scope_stats["score_sum"] / rows, 4),
        }

    same_score = float(summary_by_scope.get("same_project", {}).get("mean_scope_score") or 0.0)
    cross_score = float(summary_by_scope.get("cross_project_fallback", {}).get("mean_scope_score") or 0.0)
    same_rows = int(summary_by_scope.get("same_project", {}).get("rows") or 0)
    cross_rows = int(summary_by_scope.get("cross_project_fallback", {}).get("rows") or 0)
    score_delta = round(cross_score - same_score, 4)

    active = matched_logs >= int(min_samples)
    eager_cross_project = (
        active
        and cross_rows >= 1
        and score_delta >= 0.10
    )
    prefer_same_project_only = (
        active
        and same_rows >= 1
        and score_delta <= -0.10
    )
    recommended_cross_project_memories = 1
    if eager_cross_project and score_delta >= 0.20:
        recommended_cross_project_memories = 2
    elif prefer_same_project_only:
        recommended_cross_project_memories = 1

    return {
        "scope": scope,
        "skill_name": skill_name,
        "task_type": task_type,
        "execution_mode": execution_mode,
        "sample_count": matched_logs,
        "min_samples": int(min_samples),
        "active": active,
        "score_delta": score_delta,
        "same_project": summary_by_scope.get("same_project", {}),
        "cross_project_fallback": summary_by_scope.get("cross_project_fallback", {}),
        "eager_cross_project": eager_cross_project,
        "prefer_same_project_only": prefer_same_project_only,
        "recommended_cross_project_memories": recommended_cross_project_memories,
        "cross_project_fallback_only_if_no_project_hits": not eager_cross_project,
    }


def get_retrieval_scope_profile(
    *,
    project_id: str | None,
    skill_name: str,
    task_type: str | None = None,
    execution_mode: str | None = None,
    limit: int = 60,
    min_samples: int = 3,
    allow_cross_project: bool = True,
) -> dict[str, Any]:
    fetch_limit = max(int(limit), int(min_samples) * 6, 12)

    def _fetch_rows(*, scope_project_id: str | None, exclude_project_id: str | None = None) -> list[dict[str, Any]]:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                clauses = [
                    "log_type = 'task'",
                    "(skill_used = %s OR log_metadata->>'selected_skill' = %s OR log_metadata->>'final_skill' = %s)",
                ]
                params: list[Any] = [skill_name, skill_name, skill_name]
                if scope_project_id:
                    clauses.append("project_id = %s")
                    params.append(scope_project_id)
                if exclude_project_id:
                    clauses.append("project_id <> %s")
                    params.append(exclude_project_id)

                cur.execute(
                    f"""
                    SELECT id, project_id, skill_used, log_metadata, created_at,
                           outcome, context_tokens_in, task_wall_clock_ms, retrieval_latency_ms
                    FROM project_registry.project_logs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params + [fetch_limit],
                )
                return [dict(row) for row in cur.fetchall()]

    same_project_rows = _fetch_rows(scope_project_id=project_id) if project_id else []
    profile = derive_retrieval_scope_profile(
        same_project_rows,
        skill_name=skill_name,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="same_project" if project_id else "cross_project",
    )
    if profile.get("active") or not allow_cross_project:
        return profile

    cross_project_rows = _fetch_rows(scope_project_id=None, exclude_project_id=project_id if project_id else None)
    combined = same_project_rows + cross_project_rows
    blended = derive_retrieval_scope_profile(
        combined,
        skill_name=skill_name,
        task_type=task_type,
        execution_mode=execution_mode,
        min_samples=min_samples,
        scope="blended" if project_id else "cross_project",
    )
    if blended.get("sample_count", 0) > profile.get("sample_count", 0):
        return blended
    return profile


def get_or_create_project(
    project_path: str,
    project_name: str | None = None,
    description: str | None = None,
    tech_stack: dict | None = None,
    client_or_context: str | None = None,
) -> dict:
    normalized_path = normalize_project_path(project_path)
    if not project_name:
        project_name = Path(project_path).name

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM project_registry.projects WHERE project_path = %s",
                (normalized_path,),
            )
            existing = _row_to_dict(cur.fetchone())
            if existing:
                cur.execute(
                    """
                    UPDATE project_registry.projects
                    SET updated_at = NOW(), last_activity_at = NOW()
                    WHERE id = %s
                    """,
                    (existing["id"],),
                )
                return existing

            project_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO project_registry.projects (
                    id, project_name, project_path, description,
                    tech_stack, client_or_context, status, phase
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', 'development')
                RETURNING *
                """,
                (
                    project_id,
                    project_name,
                    normalized_path,
                    description
                    or f"Project auto-registered by MCUM on {datetime.now().strftime('%Y-%m-%d')}",
                    json.dumps(tech_stack or {}),
                    client_or_context or "personal",
                ),
            )
            return _row_to_dict(cur.fetchone()) or {"id": project_id, "project_path": normalized_path}


def update_project_info(project_id: str, **kwargs: Any) -> bool:
    allowed_fields = {
        "project_name",
        "description",
        "tech_stack",
        "status",
        "phase",
        "client_or_context",
        "primary_language",
        "frameworks",
    }
    updates = {key: value for key, value in kwargs.items() if key in allowed_fields}
    if not updates:
        return False

    if "tech_stack" in updates and isinstance(updates["tech_stack"], dict):
        updates["tech_stack"] = json.dumps(updates["tech_stack"])

    set_clause = ", ".join(f"{key} = %s" for key in updates)
    values = list(updates.values()) + [project_id]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                UPDATE project_registry.projects
                SET {set_clause}, updated_at = NOW(), last_activity_at = NOW()
                WHERE id = %s
                """,
                values,
            )
            return cur.rowcount > 0


def get_project_by_path(project_path: str) -> dict | None:
    normalized_path = normalize_project_path(project_path)
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM project_registry.projects WHERE project_path = %s",
                (normalized_path,),
            )
            return _row_to_dict(cur.fetchone())


def list_projects(status: str = "active") -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if status == "all":
                cur.execute(
                    "SELECT * FROM project_registry.v_project_summary ORDER BY last_activity_at DESC"
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM project_registry.v_project_summary
                    WHERE status = %s
                    ORDER BY last_activity_at DESC
                    """,
                    (status,),
                )
            return [dict(row) for row in cur.fetchall()]


def log_entry(
    project_id: str,
    log_type: str,
    title: str,
    description: str | None = None,
    skill_used: str | None = None,
    skills_orchestrated: list[str] | None = None,
    outcome: str | None = None,
    outcome_details: str | None = None,
    artifacts_generated: list[dict] | None = None,
    experience_ids: list[str] | None = None,
    pattern_ids_used: list[str] | None = None,
    retrieval_run_id: str | None = None,
    session_duration_sec: int | None = None,
    confidence_score: float | None = None,
    tokens_estimated: int | None = None,
    context_tokens_in: int | None = None,
    context_tokens_out: int | None = None,
    task_wall_clock_ms: int | None = None,
    retrieval_latency_ms: int | None = None,
    git_commit: str | None = None,
    log_metadata: dict | None = None,
) -> str:
    log_id = str(uuid.uuid4())
    if tokens_estimated is None:
        combined_tokens = (context_tokens_in or 0) + (context_tokens_out or 0)
        tokens_estimated = combined_tokens or None

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.project_logs (
                    id, project_id, log_type, title, description,
                    skill_used, skills_orchestrated, outcome, outcome_details,
                    artifacts_generated, experience_ids, pattern_ids_used,
                    retrieval_run_id, session_duration_sec, confidence_score,
                    tokens_estimated, context_tokens_in, context_tokens_out,
                    task_wall_clock_ms, retrieval_latency_ms,
                    git_commit, log_metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s
                )
                RETURNING id
                """,
                (
                    log_id,
                    project_id,
                    log_type,
                    title,
                    description,
                    skill_used,
                    skills_orchestrated or [],
                    outcome,
                    outcome_details,
                    json.dumps(artifacts_generated or [], ensure_ascii=False, default=str),
                    experience_ids or [],
                    pattern_ids_used or [],
                    retrieval_run_id,
                    session_duration_sec,
                    confidence_score,
                    tokens_estimated,
                    context_tokens_in,
                    context_tokens_out,
                    task_wall_clock_ms,
                    retrieval_latency_ms,
                    git_commit,
                    json.dumps(log_metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else log_id


def record_agent_invocation(
    *,
    project_id: str,
    session_id: str | None,
    task_log_id: str | None,
    task_id: str | None,
    agent_role: str,
    runner: str,
    provider: str | None = None,
    model: str | None = None,
    protocol: str | None = None,
    credential_source: str | None = None,
    outcome: str | None = None,
    exit_code: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    prompt_tokens_estimate: int | None = None,
    cost_usd: float | None = None,
    wall_clock_ms: int | None = None,
    started_at: str | datetime | None = None,
    finished_at: str | datetime | None = None,
    metadata: dict | None = None,
) -> str:
    invocation_id = str(uuid.uuid4())
    if total_tokens is None:
        total = (input_tokens or 0) + (output_tokens or 0)
        total_tokens = total or None

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS project_registry.agent_invocations (
                    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    session_id              TEXT,
                    task_log_id             UUID REFERENCES project_registry.project_logs(id) ON DELETE SET NULL,
                    task_id                 TEXT,
                    agent_role              TEXT NOT NULL,
                    runner                  TEXT NOT NULL,
                    provider                TEXT,
                    model                   TEXT,
                    protocol                TEXT,
                    credential_source       TEXT,
                    outcome                 TEXT,
                    exit_code               INT,
                    input_tokens            INT,
                    output_tokens           INT,
                    total_tokens            INT,
                    prompt_tokens_estimate  INT,
                    cost_usd                NUMERIC(12,6),
                    wall_clock_ms           INT,
                    started_at              TIMESTAMPTZ,
                    finished_at             TIMESTAMPTZ,
                    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at              TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_invocations_project_created
                    ON project_registry.agent_invocations (project_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_invocations_task_log
                    ON project_registry.agent_invocations (task_log_id)
                """
            )
            cur.execute(
                """
                INSERT INTO project_registry.agent_invocations (
                    id, project_id, session_id, task_log_id, task_id,
                    agent_role, runner, provider, model, protocol,
                    credential_source, outcome, exit_code,
                    input_tokens, output_tokens, total_tokens,
                    prompt_tokens_estimate, cost_usd, wall_clock_ms,
                    started_at, finished_at, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING id
                """,
                (
                    invocation_id,
                    project_id,
                    session_id,
                    task_log_id,
                    task_id,
                    agent_role,
                    runner,
                    provider,
                    model,
                    protocol,
                    credential_source,
                    outcome,
                    exit_code,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    prompt_tokens_estimate,
                    cost_usd,
                    wall_clock_ms,
                    started_at,
                    finished_at,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else invocation_id


def get_project_logs(
    project_id: str,
    log_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if log_type:
                cur.execute(
                    """
                    SELECT * FROM project_registry.project_logs
                    WHERE project_id = %s AND log_type = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (project_id, log_type, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM project_registry.project_logs
                    WHERE project_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (project_id, limit, offset),
                )
            return [dict(row) for row in cur.fetchall()]


def get_recent_logs(project_id: str, limit: int = 10) -> list[dict]:
    return get_project_logs(project_id, limit=limit)


def log_session_start(
    project_path: str,
    skill_used: str = "mcum-orchestrator",
    task_description: str | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    project = get_or_create_project(project_path)
    metadata = {
        "session_start": datetime.now().isoformat(),
        "project_path": normalize_project_path(project_path),
    }
    if task_description:
        metadata["task_description"] = task_description
    if extra_metadata:
        metadata.update(extra_metadata)

    log_id = log_entry(
        project_id=project["id"],
        log_type="session_start",
        title=f"Session started - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        skill_used=skill_used,
        log_metadata=metadata,
    )
    return {"project": project, "log_id": log_id}


def log_session_end(
    project_id: str,
    session_duration_sec: int,
    tasks_completed: int = 0,
    skill_used: str = "mcum-orchestrator",
    outcome: str | None = None,
    context_tokens_in: int | None = None,
    context_tokens_out: int | None = None,
    task_wall_clock_ms: int | None = None,
    retrieval_latency_ms: int | None = None,
    pattern_ids_used: list[str] | None = None,
    extra_metadata: dict | None = None,
) -> str:
    metadata = {
        "tasks_completed": tasks_completed,
        "duration_minutes": round(session_duration_sec / 60, 1),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    return log_entry(
        project_id=project_id,
        log_type="session_end",
        title=f"Session finished - {tasks_completed} task(s)",
        skill_used=skill_used,
        session_duration_sec=session_duration_sec,
        outcome=outcome or ("success" if tasks_completed > 0 else "partial"),
        context_tokens_in=context_tokens_in,
        context_tokens_out=context_tokens_out,
        task_wall_clock_ms=task_wall_clock_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        pattern_ids_used=pattern_ids_used,
        log_metadata=metadata,
    )


def refresh_daily_metrics(project_id: str | None = None) -> dict[str, Any]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("REFRESH MATERIALIZED VIEW project_registry.mv_daily_metrics")
            if project_id:
                cur.execute(
                    """
                    SELECT COUNT(*) AS rows_refreshed, MAX(day) AS latest_day
                    FROM project_registry.mv_daily_metrics
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*) AS rows_refreshed, MAX(day) AS latest_day
                    FROM project_registry.mv_daily_metrics
                    """
                )
            row = dict(cur.fetchone() or {})
    return {
        "project_id": project_id,
        "rows_refreshed": int(row.get("rows_refreshed") or 0),
        "latest_day": row.get("latest_day"),
    }


def snapshot_project_kpis(
    *,
    project_id: str | None = None,
    snapshot_date: date | None = None,
    window_days: int = 14,
    notes: str | None = None,
) -> list[dict]:
    snapshot_day = snapshot_date or datetime.now(timezone.utc).date()
    lookback_days = max(1, int(window_days))
    window_start = snapshot_day - timedelta(days=lookback_days - 1)
    project_clause = "WHERE p.id = %s" if project_id else ""
    params: list[Any] = []
    if project_id:
        params.append(project_id)
    params.extend(
        [
            window_start,
            snapshot_day,
            window_start,
            snapshot_day,
            window_start,
            snapshot_day,
            window_start,
            snapshot_day,
            window_start,
            snapshot_day,
            snapshot_day,
            notes or f"window_days={lookback_days}",
        ]
    )

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                WITH target_projects AS (
                    SELECT p.id
                    FROM project_registry.projects p
                    {project_clause}
                ),
                task_stats AS (
                    SELECT
                        tp.id AS project_id,
                        COUNT(*) FILTER (
                            WHERE pl.log_type = 'task'
                              AND pl.created_at >= %s
                              AND pl.created_at < (%s::date + INTERVAL '1 day')
                        ) AS tasks_this_period,
                        COUNT(*) FILTER (
                            WHERE pl.log_type = 'task'
                              AND pl.outcome = 'success'
                              AND pl.created_at >= %s
                              AND pl.created_at < (%s::date + INTERVAL '1 day')
                        ) AS success_tasks,
                        AVG(pl.confidence_score) FILTER (
                            WHERE pl.log_type = 'task'
                              AND pl.confidence_score IS NOT NULL
                              AND pl.created_at >= %s
                              AND pl.created_at < (%s::date + INTERVAL '1 day')
                        ) AS avg_confidence
                    FROM target_projects tp
                    LEFT JOIN project_registry.project_logs pl
                        ON pl.project_id = tp.id
                    GROUP BY tp.id
                ),
                improvement_entries AS (
                    SELECT
                        tp.id AS project_id,
                        COALESCE(NULLIF(unnested.skill_name, ''), pl.skill_used) AS skill_name
                    FROM target_projects tp
                    LEFT JOIN project_registry.project_logs pl
                        ON pl.project_id = tp.id
                       AND pl.log_type = 'improvement'
                       AND pl.created_at >= %s
                       AND pl.created_at < (%s::date + INTERVAL '1 day')
                    LEFT JOIN LATERAL UNNEST(
                        CASE
                            WHEN COALESCE(array_length(pl.skills_orchestrated, 1), 0) > 0 THEN pl.skills_orchestrated
                            ELSE ARRAY[COALESCE(pl.skill_used, '')]
                        END
                    ) AS unnested(skill_name) ON TRUE
                ),
                improvement_stats AS (
                    SELECT
                        project_id,
                        COUNT(DISTINCT skill_name) FILTER (WHERE skill_name <> '') AS skills_improved
                    FROM improvement_entries
                    GROUP BY project_id
                ),
                experience_stats AS (
                    SELECT
                        tp.id AS project_id,
                        COUNT(exp.id) AS total_experiences_added
                    FROM target_projects tp
                    LEFT JOIN core_brain.experiences exp
                        ON exp.project_id = tp.id
                       AND exp.created_at >= %s
                       AND exp.created_at < (%s::date + INTERVAL '1 day')
                    GROUP BY tp.id
                )
                INSERT INTO project_registry.project_kpis (
                    project_id,
                    snapshot_date,
                    tasks_this_period,
                    success_rate,
                    avg_confidence,
                    skills_improved,
                    total_experiences_added,
                    notes
                )
                SELECT
                    ts.project_id,
                    %s::date,
                    ts.tasks_this_period,
                    CASE
                        WHEN ts.tasks_this_period > 0
                            THEN ROUND(ts.success_tasks::numeric / ts.tasks_this_period::numeric, 4)
                        ELSE NULL
                    END AS success_rate,
                    ts.avg_confidence,
                    COALESCE(improvement_stats.skills_improved, 0) AS skills_improved,
                    COALESCE(experience_stats.total_experiences_added, 0) AS total_experiences_added,
                    %s
                FROM task_stats ts
                LEFT JOIN improvement_stats
                    ON improvement_stats.project_id = ts.project_id
                LEFT JOIN experience_stats
                    ON experience_stats.project_id = ts.project_id
                ON CONFLICT (project_id, snapshot_date) DO UPDATE
                SET
                    tasks_this_period = EXCLUDED.tasks_this_period,
                    success_rate = EXCLUDED.success_rate,
                    avg_confidence = EXCLUDED.avg_confidence,
                    skills_improved = EXCLUDED.skills_improved,
                    total_experiences_added = EXCLUDED.total_experiences_added,
                    notes = EXCLUDED.notes,
                    created_at = NOW()
                RETURNING *
                """,
                params,
            )
            return [_row_to_dict(dict(row)) or {} for row in cur.fetchall()]


def audit_memory_governance(
    *,
    project_id: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    memory_targets = _json_object(policy.get("memory_targets"))
    experience_verbose_threshold = int(memory_targets.get("experience_verbose_chars", 720) or 720)
    playbook_verbose_threshold = int(memory_targets.get("playbook_verbose_chars", 720) or 720)
    playbook_compact_min = int(memory_targets.get("playbook_compact_min_chars", 48) or 48)
    playbook_compact_max = int(memory_targets.get("playbook_compact_max_chars", 320) or 320)

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                WITH scoped AS (
                    SELECT
                        id,
                        LOWER(TRIM(COALESCE(title, ''))) AS normalized_title,
                        current_confidence,
                        COALESCE(revalidation_count, 0) AS revalidation_count,
                        COALESCE(unique_context_count, 0) AS unique_context_count,
                        LENGTH(COALESCE(content::text, '')) AS content_chars,
                        category
                    FROM core_brain.experiences
                    WHERE project_id = %s
                      AND superseded_by IS NULL
                ),
                category_counts AS (
                    SELECT category, COUNT(*) AS category_count
                    FROM scoped
                    GROUP BY category
                )
                SELECT
                    COUNT(*) AS total_experiences,
                    COUNT(DISTINCT NULLIF(normalized_title, '')) AS unique_titles,
                    COUNT(*) FILTER (WHERE current_confidence < 0.45) AS low_confidence_experiences,
                    COUNT(*) FILTER (
                        WHERE revalidation_count = 0
                          AND unique_context_count = 0
                    ) AS low_validation_experiences,
                    COUNT(*) FILTER (WHERE content_chars > %s) AS verbose_experiences,
                    COALESCE((SELECT MAX(category_count) FROM category_counts), 0) AS dominant_category_count
                FROM scoped
                """,
                (project_id, experience_verbose_threshold),
            )
            experience_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                WITH grouped AS (
                    SELECT COUNT(*) AS group_size
                    FROM core_brain.experiences
                    WHERE project_id = %s
                      AND superseded_by IS NULL
                    GROUP BY
                        category,
                        skill_name,
                        LOWER(TRIM(COALESCE(title, ''))),
                        COALESCE(task_description, ''),
                        COALESCE(content->>'conclusion', '')
                    HAVING COUNT(*) > 1
                )
                SELECT
                    COUNT(*) AS exact_duplicate_groups,
                    COALESCE(SUM(group_size - 1), 0) AS exact_duplicate_experiences
                FROM grouped
                """,
                (project_id,),
            )
            exact_duplicate_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                WITH scoped AS (
                    SELECT
                        id,
                        COALESCE(reuse_count, 0) AS reuse_count,
                        LENGTH(COALESCE(output_summary, '')) + LENGTH(COALESCE(validation_summary, '')) AS summary_chars,
                        LENGTH(COALESCE(output_summary, '')) AS output_chars,
                        LENGTH(COALESCE(validation_summary, '')) AS validation_chars
                    FROM core_brain.session_playbooks
                    WHERE project_id = %s
                )
                SELECT
                    COUNT(*) AS total_playbooks,
                    COUNT(*) FILTER (WHERE reuse_count = 0) AS never_reused_playbooks,
                    COUNT(*) FILTER (WHERE reuse_count <= 1) AS low_reuse_playbooks,
                    COUNT(*) FILTER (WHERE summary_chars > %s) AS verbose_playbooks,
                    COUNT(*) FILTER (
                        WHERE output_chars BETWEEN %s AND %s
                          AND validation_chars > 0
                    ) AS compact_playbooks
                FROM scoped
                """,
                (
                    project_id,
                    playbook_verbose_threshold,
                    playbook_compact_min,
                    playbook_compact_max,
                ),
            )
            playbook_row = dict(cur.fetchone() or {})

    total_experiences = int(experience_row.get("total_experiences") or 0)
    total_playbooks = int(playbook_row.get("total_playbooks") or 0)
    unique_titles = int(experience_row.get("unique_titles") or 0)
    duplicate_experiences = max(0, total_experiences - unique_titles)
    exact_duplicate_groups = int(exact_duplicate_row.get("exact_duplicate_groups") or 0)
    exact_duplicate_experiences = int(exact_duplicate_row.get("exact_duplicate_experiences") or 0)

    duplicate_ratio = _safe_ratio(duplicate_experiences, total_experiences)
    exact_duplicate_ratio = _safe_ratio(exact_duplicate_experiences, total_experiences)
    experience_verbose_ratio = _safe_ratio(experience_row.get("verbose_experiences"), total_experiences)
    low_validation_ratio = _safe_ratio(experience_row.get("low_validation_experiences"), total_experiences)
    low_confidence_ratio = _safe_ratio(experience_row.get("low_confidence_experiences"), total_experiences)
    dominant_category_ratio = _safe_ratio(experience_row.get("dominant_category_count"), total_experiences)

    never_reused_ratio = _safe_ratio(playbook_row.get("never_reused_playbooks"), total_playbooks)
    low_reuse_ratio = _safe_ratio(playbook_row.get("low_reuse_playbooks"), total_playbooks)
    playbook_verbose_ratio = _safe_ratio(playbook_row.get("verbose_playbooks"), total_playbooks)
    playbook_compact_ratio = _safe_ratio(playbook_row.get("compact_playbooks"), total_playbooks)
    exact_duplicate_ratio_threshold = float(memory_targets.get("max_exact_duplicate_ratio", 0.01) or 0.01)

    reasons: list[str] = []
    if exact_duplicate_experiences > 0 and exact_duplicate_ratio >= max(0.0, exact_duplicate_ratio_threshold):
        reasons.append("memory_exact_duplicates_found")
    if duplicate_ratio > float(memory_targets.get("max_experience_duplicate_ratio", 0.18) or 0.18):
        reasons.append("memory_duplicates_high")
    if experience_verbose_ratio > float(memory_targets.get("max_experience_verbose_ratio", 0.22) or 0.22):
        reasons.append("memory_verbosity_high")
    if low_validation_ratio > float(memory_targets.get("max_low_validation_experience_ratio", 0.35) or 0.35):
        reasons.append("memory_low_validation_high")
    if never_reused_ratio > float(memory_targets.get("max_playbook_never_reused_ratio", 0.45) or 0.45):
        reasons.append("playbook_reuse_low")
    if playbook_verbose_ratio > float(memory_targets.get("max_playbook_verbose_ratio", 0.20) or 0.20):
        reasons.append("playbook_verbosity_high")
    if playbook_compact_ratio < float(memory_targets.get("min_playbook_compact_ratio", 0.40) or 0.40):
        reasons.append("playbook_compactness_low")

    contamination_score = round(
        min(
            1.0,
            (duplicate_ratio * 0.20)
            + (exact_duplicate_ratio * 0.08)
            + (experience_verbose_ratio * 0.18)
            + (low_validation_ratio * 0.22)
            + (never_reused_ratio * 0.12)
            + (playbook_verbose_ratio * 0.12)
            + (max(0.0, 1.0 - playbook_compact_ratio) * 0.12),
        ),
        4,
    )
    if contamination_score >= 0.45:
        severity = "high"
    elif contamination_score >= 0.25:
        severity = "medium"
    else:
        severity = "low"

    return {
        "project_id": project_id,
        "severity": severity,
        "contamination_score": contamination_score,
        "reasons": reasons,
        "experience_metrics": {
            "total_experiences": total_experiences,
            "duplicate_experiences": duplicate_experiences,
            "duplicate_ratio": duplicate_ratio,
            "exact_duplicate_groups": exact_duplicate_groups,
            "exact_duplicate_experiences": exact_duplicate_experiences,
            "exact_duplicate_ratio": exact_duplicate_ratio,
            "low_confidence_ratio": low_confidence_ratio,
            "low_validation_ratio": low_validation_ratio,
            "verbose_ratio": experience_verbose_ratio,
            "dominant_category_ratio": dominant_category_ratio,
        },
        "playbook_metrics": {
            "total_playbooks": total_playbooks,
            "never_reused_ratio": never_reused_ratio,
            "low_reuse_ratio": low_reuse_ratio,
            "verbose_ratio": playbook_verbose_ratio,
            "compact_ratio": playbook_compact_ratio,
        },
    }


def analyze_anti_loop_dispatch_effectiveness(
    *,
    project_id: str,
    since: datetime | str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    tuning = _json_object(policy.get("anti_loop_dispatch_tuning"))
    if not bool(tuning.get("enabled", True)):
        return {"project_id": project_id, "enabled": False}

    execution_policy = load_execution_policy()
    anti_loop_policy = _json_object(execution_policy.get("anti_loop"))
    lookback_days = max(3, int(tuning.get("lookback_days", 21) or 21))
    since_dt = _coerce_datetime(since) or (datetime.now(timezone.utc) - timedelta(days=lookback_days))
    min_hinted_tasks = max(1, int(tuning.get("min_hinted_tasks", 6) or 6))
    min_preferred_selected = max(1, int(tuning.get("min_preferred_selected", 2) or 2))
    target_success_rate = float(tuning.get("target_success_rate", 0.82) or 0.82)
    low_success_rate = float(tuning.get("low_success_rate", 0.55) or 0.55)
    success_margin = float(tuning.get("success_margin", 0.08) or 0.08)
    target_preferred_selection_rate = float(tuning.get("target_preferred_selection_rate", 0.45) or 0.45)
    low_preferred_selection_rate = float(tuning.get("low_preferred_selection_rate", 0.20) or 0.20)
    min_hours_between_adjustments = max(
        0,
        int(tuning.get("min_hours_between_adjustments", 24) or 24),
    )
    reversal_cooldown_hours = max(
        0,
        int(tuning.get("reversal_cooldown_hours", 72) or 72),
    )

    current_score_boost = float(anti_loop_policy.get("dispatch_preference_score_boost", 0.08) or 0.08)
    current_priority_boost = float(anti_loop_policy.get("dispatch_preference_priority_boost", 0.5) or 0.5)
    tuning_history = summarize_anti_loop_dispatch_tuning_history(
        project_id=project_id,
        maintenance_name=str(policy.get("maintenance_name") or tuning.get("maintenance_name") or "daily_guard").strip()
        or "daily_guard",
        lookback_days=max(
            lookback_days,
            int(tuning.get("history_lookback_days", lookback_days) or lookback_days),
        ),
        limit=max(3, int(tuning.get("history_limit", 6) or 6)),
    )

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    outcome,
                    confidence_score,
                    context_tokens_in,
                    task_wall_clock_ms,
                    skill_used,
                    log_metadata,
                    created_at
                FROM project_registry.project_logs
                WHERE project_id = %s
                  AND log_type = 'task'
                  AND created_at >= %s
                  AND COALESCE(log_metadata->>'maintenance_mode', 'false') <> 'true'
                ORDER BY created_at DESC
                """,
                (project_id, since_dt),
            )
            rows = [_row_to_dict(dict(row)) or {} for row in cur.fetchall()]

    total_tasks = len(rows)
    hinted_tasks = 0
    high_risk_hinted_tasks = 0
    preferred_selected_tasks = 0
    preferred_selected_successes = 0
    non_preferred_tasks = 0
    non_preferred_successes = 0
    forced_override_tasks = 0
    hinted_tokens_total = 0.0
    hinted_token_rows = 0
    preferred_tokens_total = 0.0
    preferred_token_rows = 0
    preferred_skill_counts: defaultdict[str, int] = defaultdict(int)

    for row in rows:
        metadata = _json_object(row.get("log_metadata"))
        dispatch_hints = _json_object(metadata.get("dispatch_hints"))
        preferred_skills = [
            str(item).strip()
            for item in list(dispatch_hints.get("preferred_skills") or [])
            if str(item).strip()
        ]
        if not preferred_skills:
            continue

        hinted_tasks += 1
        loop_risk = float(dispatch_hints.get("loop_risk") or 0.0)
        warning_risk_threshold = float(
            dispatch_hints.get("warning_risk_threshold")
            or anti_loop_policy.get("warning_risk_threshold", 0.35)
            or 0.35
        )
        if loop_risk >= warning_risk_threshold:
            high_risk_hinted_tasks += 1

        selected_skill = str(metadata.get("selected_skill") or row.get("skill_used") or "").strip()
        dispatch_method = str(metadata.get("dispatch_method") or "").strip()
        outcome = str(row.get("outcome") or "").strip().lower()
        if dispatch_method == "forced_by_user":
            forced_override_tasks += 1

        context_tokens_in = row.get("context_tokens_in")
        if context_tokens_in is not None:
            hinted_tokens_total += float(context_tokens_in or 0.0)
            hinted_token_rows += 1

        if selected_skill in preferred_skills:
            preferred_selected_tasks += 1
            preferred_skill_counts[selected_skill] += 1
            if outcome == "success":
                preferred_selected_successes += 1
            if context_tokens_in is not None:
                preferred_tokens_total += float(context_tokens_in or 0.0)
                preferred_token_rows += 1
        else:
            non_preferred_tasks += 1
            if outcome == "success":
                non_preferred_successes += 1

    preferred_selected_rate = _safe_ratio(preferred_selected_tasks, hinted_tasks)
    preferred_success_rate = _safe_ratio(preferred_selected_successes, preferred_selected_tasks)
    non_preferred_success_rate = _safe_ratio(non_preferred_successes, non_preferred_tasks)
    avg_hinted_context_tokens_in = round(hinted_tokens_total / hinted_token_rows, 2) if hinted_token_rows else None
    avg_preferred_context_tokens_in = round(preferred_tokens_total / preferred_token_rows, 2) if preferred_token_rows else None

    reasons: list[str] = []
    recommendation = "no_change"
    proposed_score_boost = current_score_boost
    proposed_priority_boost = current_priority_boost
    recommended_action = None
    reason = "insufficient_signal"

    if hinted_tasks < min_hinted_tasks:
        reason = "insufficient_hinted_tasks"
    elif preferred_selected_tasks < min_preferred_selected:
        if preferred_selected_rate <= low_preferred_selection_rate and high_risk_hinted_tasks >= min_hinted_tasks:
            recommendation = "increase_bias"
            recommended_action = "tune_anti_loop_dispatch_bias"
            reason = "preferred_paths_under_selected"
            proposed_score_boost = current_score_boost + float(tuning.get("score_step", 0.01) or 0.01)
            proposed_priority_boost = current_priority_boost + float(tuning.get("priority_step", 0.1) or 0.1)
            reasons.append("anti_loop_dispatch_tuning_needed")
        else:
            reason = "insufficient_preferred_selections"
    elif (
        preferred_success_rate >= target_success_rate
        and preferred_success_rate >= (non_preferred_success_rate + success_margin)
        and preferred_selected_rate < target_preferred_selection_rate
    ):
        recommendation = "increase_bias"
        recommended_action = "tune_anti_loop_dispatch_bias"
        reason = "successful_preferred_paths_need_more_weight"
        proposed_score_boost = current_score_boost + float(tuning.get("score_step", 0.01) or 0.01)
        proposed_priority_boost = current_priority_boost + float(tuning.get("priority_step", 0.1) or 0.1)
        reasons.append("anti_loop_dispatch_tuning_needed")
    elif (
        preferred_success_rate <= low_success_rate
        and preferred_selected_rate >= target_preferred_selection_rate
    ) or (non_preferred_success_rate >= preferred_success_rate + success_margin):
        recommendation = "decrease_bias"
        recommended_action = "tune_anti_loop_dispatch_bias"
        reason = "preferred_paths_underperforming"
        proposed_score_boost = current_score_boost - float(tuning.get("score_step", 0.01) or 0.01)
        proposed_priority_boost = current_priority_boost - float(tuning.get("priority_step", 0.1) or 0.1)
        reasons.append("anti_loop_dispatch_tuning_needed")
    else:
        reason = "within_target_band"

    min_score_boost = float(tuning.get("min_score_boost", 0.04) or 0.04)
    max_score_boost = float(tuning.get("max_score_boost", 0.14) or 0.14)
    min_priority_boost = float(tuning.get("min_priority_boost", 0.2) or 0.2)
    max_priority_boost = float(tuning.get("max_priority_boost", 1.0) or 1.0)
    proposed_score_boost = round(_clamp(proposed_score_boost, min_score_boost, max_score_boost), 4)
    proposed_priority_boost = round(_clamp(proposed_priority_boost, min_priority_boost, max_priority_boost), 4)
    stability_guard = {
        "active": False,
        "reason": None,
        "last_direction": tuning_history.get("last_direction"),
        "last_updated_at": tuning_history.get("last_updated_at"),
        "hours_since_last_update": tuning_history.get("hours_since_last_update"),
    }
    if recommended_action == "tune_anti_loop_dispatch_bias":
        hours_since_last_update = tuning_history.get("hours_since_last_update")
        last_direction = str(tuning_history.get("last_direction") or "").strip()
        proposed_direction = "increase" if recommendation == "increase_bias" else "decrease"
        if (
            hours_since_last_update is not None
            and hours_since_last_update < min_hours_between_adjustments
        ):
            recommended_action = None
            recommendation = "no_change"
            reason = "recent_adjustment_cooldown"
            reasons = []
            proposed_score_boost = round(current_score_boost, 4)
            proposed_priority_boost = round(current_priority_boost, 4)
            stability_guard.update(
                {
                    "active": True,
                    "reason": "recent_adjustment_cooldown",
                }
            )
        elif (
            last_direction
            and proposed_direction != last_direction
            and hours_since_last_update is not None
            and hours_since_last_update < reversal_cooldown_hours
        ):
            recommended_action = None
            recommendation = "no_change"
            reason = "reversal_cooldown_active"
            reasons = []
            proposed_score_boost = round(current_score_boost, 4)
            proposed_priority_boost = round(current_priority_boost, 4)
            stability_guard.update(
                {
                    "active": True,
                    "reason": "reversal_cooldown_active",
                }
            )

    return {
        "project_id": project_id,
        "enabled": True,
        "since": since_dt.isoformat(),
        "lookback_days": lookback_days,
        "current_score_boost": round(current_score_boost, 4),
        "current_priority_boost": round(current_priority_boost, 4),
        "suggested_score_boost": proposed_score_boost,
        "suggested_priority_boost": proposed_priority_boost,
        "recommended_action": recommended_action,
        "recommendation": recommendation,
        "reason": reason,
        "reasons": reasons,
        "stability_guard": stability_guard,
        "history": tuning_history,
        "metrics": {
            "total_tasks": total_tasks,
            "hinted_tasks": hinted_tasks,
            "high_risk_hinted_tasks": high_risk_hinted_tasks,
            "preferred_selected_tasks": preferred_selected_tasks,
            "preferred_selected_rate": preferred_selected_rate,
            "preferred_success_rate": preferred_success_rate,
            "non_preferred_tasks": non_preferred_tasks,
            "non_preferred_success_rate": non_preferred_success_rate,
            "forced_override_tasks": forced_override_tasks,
            "avg_hinted_context_tokens_in": avg_hinted_context_tokens_in,
            "avg_preferred_context_tokens_in": avg_preferred_context_tokens_in,
            "top_preferred_selected_skills": [
                {"skill_name": skill_name, "count": count}
                for skill_name, count in sorted(
                    preferred_skill_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:3]
            ],
        },
        "targets": {
            "min_hinted_tasks": min_hinted_tasks,
            "min_preferred_selected": min_preferred_selected,
            "target_success_rate": target_success_rate,
            "low_success_rate": low_success_rate,
            "success_margin": success_margin,
            "target_preferred_selection_rate": target_preferred_selection_rate,
            "low_preferred_selection_rate": low_preferred_selection_rate,
            "min_score_boost": min_score_boost,
            "max_score_boost": max_score_boost,
            "min_priority_boost": min_priority_boost,
            "max_priority_boost": max_priority_boost,
            "min_hours_between_adjustments": min_hours_between_adjustments,
            "reversal_cooldown_hours": reversal_cooldown_hours,
        },
    }


def get_recent_maintenance_runs(
    *,
    project_id: str | None = None,
    maintenance_name: str | None = None,
    since: datetime | str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = %s")
        params.append(project_id)
    if maintenance_name:
        clauses.append("maintenance_name = %s")
        params.append(maintenance_name)
    since_dt = _coerce_datetime(since)
    if since_dt is not None:
        clauses.append("COALESCE(finished_at, started_at) >= %s")
        params.append(since_dt)

    params.append(max(1, int(limit or 10)))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM project_registry.maintenance_runs
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(finished_at, started_at) DESC, created_at DESC
                LIMIT %s
                """,
                params,
            )
            return [_row_to_dict(dict(row)) or {} for row in cur.fetchall()]


def summarize_anti_loop_dispatch_tuning_history(
    *,
    project_id: str,
    maintenance_name: str = "daily_guard",
    lookback_days: int = 30,
    limit: int = 6,
) -> dict[str, Any]:
    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days or 30)))
    runs = get_recent_maintenance_runs(
        project_id=project_id,
        maintenance_name=maintenance_name,
        since=since_dt,
        limit=max(1, int(limit or 6)),
    )

    updates: list[dict[str, Any]] = []
    for run in runs:
        actions = _json_list(run.get("actions_applied"))
        finished_at = _coerce_datetime(run.get("finished_at") or run.get("started_at"))
        for action in actions:
            if not isinstance(action, dict):
                continue
            if str(action.get("action") or "").strip() != "tune_anti_loop_dispatch_bias":
                continue
            if str(action.get("status") or "").strip() != "success":
                continue
            result = _json_object(action.get("result"))
            if not bool(result.get("policy_updated")):
                continue
            previous = _json_object(result.get("previous_values"))
            updated = _json_object(result.get("updated_values"))
            analysis = _json_object(result.get("analysis"))
            previous_score = previous.get("score_boost")
            updated_score = updated.get("score_boost")
            previous_priority = previous.get("priority_boost")
            updated_priority = updated.get("priority_boost")
            direction = ""
            try:
                if previous_score is not None and updated_score is not None:
                    delta_score = float(updated_score) - float(previous_score)
                    if delta_score > 0.0001:
                        direction = "increase"
                    elif delta_score < -0.0001:
                        direction = "decrease"
            except (TypeError, ValueError):
                direction = ""
            if not direction:
                recommendation = str(analysis.get("recommendation") or "").strip()
                if recommendation == "increase_bias":
                    direction = "increase"
                elif recommendation == "decrease_bias":
                    direction = "decrease"
            updates.append(
                {
                    "maintenance_run_id": run.get("id"),
                    "finished_at": finished_at.isoformat() if finished_at else None,
                    "direction": direction or None,
                    "recommendation": analysis.get("recommendation"),
                    "score_boost_before": previous_score,
                    "score_boost_after": updated_score,
                    "priority_boost_before": previous_priority,
                    "priority_boost_after": updated_priority,
                }
            )

    last_update = updates[0] if updates else None
    hours_since_last_update = None
    last_updated_at = _coerce_datetime((last_update or {}).get("finished_at"))
    if last_updated_at is not None:
        hours_since_last_update = round(
            (datetime.now(timezone.utc) - last_updated_at).total_seconds() / 3600.0,
            2,
        )

    same_direction_streak = 0
    last_direction = str((last_update or {}).get("direction") or "").strip() or None
    if last_direction:
        for update in updates:
            if str(update.get("direction") or "").strip() == last_direction:
                same_direction_streak += 1
            else:
                break

    return {
        "maintenance_name": maintenance_name,
        "lookback_days": max(1, int(lookback_days or 30)),
        "runs_considered": len(runs),
        "updated_runs": len(updates),
        "last_direction": last_direction,
        "last_updated_at": last_updated_at.isoformat() if last_updated_at else None,
        "hours_since_last_update": hours_since_last_update,
        "same_direction_streak": same_direction_streak,
        "recent_updates": updates[: max(1, int(limit or 6))],
    }


def summarize_recent_operational_metrics(
    *,
    project_id: str,
    since: datetime | str | None = None,
) -> dict[str, Any]:
    since_dt = _coerce_datetime(since) or (datetime.now(timezone.utc) - timedelta(days=7))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    outcome,
                    confidence_score,
                    context_tokens_in,
                    context_tokens_out,
                    retrieval_latency_ms,
                    task_wall_clock_ms,
                    log_metadata,
                    created_at
                FROM project_registry.project_logs
                WHERE project_id = %s
                  AND log_type = 'task'
                  AND created_at >= %s
                  AND COALESCE(log_metadata->>'maintenance_mode', 'false') <> 'true'
                ORDER BY created_at DESC
                """,
                (project_id, since_dt),
            )
            rows = [_row_to_dict(dict(row)) or {} for row in cur.fetchall()]

    total_tasks = len(rows)
    successes = 0
    partials = 0
    failures = 0
    confidence_total = 0.0
    confidence_rows = 0
    tokens_in_total = 0.0
    tokens_in_rows = 0
    tokens_out_total = 0.0
    tokens_out_rows = 0
    retrieval_total = 0.0
    retrieval_rows = 0
    hinted_tasks = 0
    preferred_selected_tasks = 0
    preferred_selected_successes = 0
    memory_governor_local_filter_tasks = 0
    playbook_governor_local_filter_tasks = 0
    governor_local_filter_tasks = 0
    governor_filtered_items_total = 0
    governor_fallback_tasks = 0

    for row in rows:
        outcome = str(row.get("outcome") or "").strip().lower()
        if outcome == "success":
            successes += 1
        elif outcome == "partial":
            partials += 1
        elif outcome == "failure":
            failures += 1

        confidence_score = row.get("confidence_score")
        if confidence_score is not None:
            confidence_total += float(confidence_score or 0.0)
            confidence_rows += 1

        context_tokens_in = row.get("context_tokens_in")
        if context_tokens_in is not None:
            tokens_in_total += float(context_tokens_in or 0.0)
            tokens_in_rows += 1

        context_tokens_out = row.get("context_tokens_out")
        if context_tokens_out is not None:
            tokens_out_total += float(context_tokens_out or 0.0)
            tokens_out_rows += 1

        retrieval_latency_ms = row.get("retrieval_latency_ms")
        if retrieval_latency_ms is not None:
            retrieval_total += float(retrieval_latency_ms or 0.0)
            retrieval_rows += 1

        metadata = _json_object(row.get("log_metadata"))
        memory_governance = _json_object(metadata.get("memory_governance"))
        playbook_memory_governance = _json_object(metadata.get("playbook_memory_governance"))
        memory_governor_local_filter = bool(memory_governance.get("adaptive_filter_applied"))
        playbook_governor_local_filter = bool(playbook_memory_governance.get("adaptive_filter_applied"))
        if memory_governor_local_filter:
            memory_governor_local_filter_tasks += 1
        if playbook_governor_local_filter:
            playbook_governor_local_filter_tasks += 1
        if memory_governor_local_filter or playbook_governor_local_filter:
            governor_local_filter_tasks += 1
        governor_filtered_items_total += int(memory_governance.get("total_filtered_count") or 0)
        governor_filtered_items_total += int(playbook_memory_governance.get("filtered_count") or 0)
        if bool(memory_governance.get("fallback_applied")) or bool(playbook_memory_governance.get("fallback_applied")):
            governor_fallback_tasks += 1

        dispatch_hints = _json_object(metadata.get("dispatch_hints"))
        preferred_skills = [
            str(item).strip()
            for item in list(dispatch_hints.get("preferred_skills") or [])
            if str(item).strip()
        ]
        if preferred_skills:
            hinted_tasks += 1
            selected_skill = str(metadata.get("selected_skill") or "").strip()
            if selected_skill in preferred_skills:
                preferred_selected_tasks += 1
                if outcome == "success":
                    preferred_selected_successes += 1

    total_context_tokens = tokens_in_total + tokens_out_total
    token_efficiency_per_1k = round((successes * 1000.0) / max(1.0, total_context_tokens), 4)

    return {
        "since": since_dt.isoformat(),
        "total_tasks": total_tasks,
        "success_rate": _safe_ratio(successes, total_tasks),
        "partial_rate": _safe_ratio(partials, total_tasks),
        "failure_rate": _safe_ratio(failures, total_tasks),
        "avg_confidence": round(confidence_total / confidence_rows, 4) if confidence_rows else None,
        "avg_context_tokens_in": round(tokens_in_total / tokens_in_rows, 2) if tokens_in_rows else None,
        "avg_context_tokens_out": round(tokens_out_total / tokens_out_rows, 2) if tokens_out_rows else None,
        "avg_total_context_tokens": round(total_context_tokens / max(1, total_tasks), 2) if total_tasks else None,
        "token_efficiency_per_1k": token_efficiency_per_1k,
        "avg_retrieval_latency_ms": round(retrieval_total / retrieval_rows, 2) if retrieval_rows else None,
        "memory_governor_local_filter_rate": _safe_ratio(memory_governor_local_filter_tasks, total_tasks),
        "playbook_governor_local_filter_rate": _safe_ratio(playbook_governor_local_filter_tasks, total_tasks),
        "governor_local_filter_activation_rate": _safe_ratio(governor_local_filter_tasks, total_tasks),
        "governor_filtered_items_total": governor_filtered_items_total,
        "governor_filtered_items_per_task": round(governor_filtered_items_total / max(1, total_tasks), 4) if total_tasks else 0.0,
        "governor_fallback_rate": _safe_ratio(governor_fallback_tasks, total_tasks),
        "anti_loop_hinted_rate": _safe_ratio(hinted_tasks, total_tasks),
        "anti_loop_preferred_selected_rate": _safe_ratio(preferred_selected_tasks, hinted_tasks),
        "anti_loop_preferred_success_rate": _safe_ratio(preferred_selected_successes, preferred_selected_tasks),
    }


def summarize_memory_governor_tuning_history(
    *,
    project_id: str,
    maintenance_name: str = "daily_guard",
    lookback_days: int = 30,
    limit: int = 6,
) -> dict[str, Any]:
    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days or 30)))
    runs = get_recent_maintenance_runs(
        project_id=project_id,
        maintenance_name=maintenance_name,
        since=since_dt,
        limit=max(1, int(limit or 6)),
    )

    updates: list[dict[str, Any]] = []
    for run in runs:
        actions = _json_list(run.get("actions_applied"))
        finished_at = _coerce_datetime(run.get("finished_at") or run.get("started_at"))
        for action in actions:
            if not isinstance(action, dict):
                continue
            if str(action.get("action") or "").strip() != "tune_memory_governor":
                continue
            if str(action.get("status") or "").strip() != "success":
                continue
            result = _json_object(action.get("result"))
            if not bool(result.get("policy_updated")):
                continue
            previous = _json_object(result.get("previous_values"))
            updated = _json_object(result.get("updated_values"))
            analysis = _json_object(result.get("analysis"))
            direction = str(analysis.get("recommendation") or "").strip()
            if direction not in {"tighten", "relax"}:
                direction = ""
                try:
                    previous_penalty = float(previous.get("assist_penalty_weight"))
                    updated_penalty = float(updated.get("assist_penalty_weight"))
                    if updated_penalty > previous_penalty:
                        direction = "tighten"
                    elif updated_penalty < previous_penalty:
                        direction = "relax"
                except (TypeError, ValueError):
                    direction = ""
            updates.append(
                {
                    "maintenance_run_id": run.get("id"),
                    "finished_at": finished_at.isoformat() if finished_at else None,
                    "direction": direction or None,
                    "assist_penalty_weight_before": previous.get("assist_penalty_weight"),
                    "assist_penalty_weight_after": updated.get("assist_penalty_weight"),
                    "cross_project_risk_before": previous.get("cross_project_risk"),
                    "cross_project_risk_after": updated.get("cross_project_risk"),
                    "verbosity_soft_cap_before": previous.get("verbosity_soft_cap"),
                    "verbosity_soft_cap_after": updated.get("verbosity_soft_cap"),
                }
            )

    last_update = updates[0] if updates else None
    hours_since_last_update = None
    last_updated_at = _coerce_datetime((last_update or {}).get("finished_at"))
    if last_updated_at is not None:
        hours_since_last_update = round(
            (datetime.now(timezone.utc) - last_updated_at).total_seconds() / 3600.0,
            2,
        )

    same_direction_streak = 0
    last_direction = str((last_update or {}).get("direction") or "").strip() or None
    if last_direction:
        for update in updates:
            if str(update.get("direction") or "").strip() == last_direction:
                same_direction_streak += 1
            else:
                break

    return {
        "maintenance_name": maintenance_name,
        "lookback_days": max(1, int(lookback_days or 30)),
        "runs_considered": len(runs),
        "updated_runs": len(updates),
        "last_direction": last_direction,
        "last_updated_at": last_updated_at.isoformat() if last_updated_at else None,
        "hours_since_last_update": hours_since_last_update,
        "same_direction_streak": same_direction_streak,
        "recent_updates": updates[: max(1, int(limit or 6))],
    }


def analyze_memory_governor_effectiveness(
    *,
    project_id: str,
    since: datetime | str | None = None,
    policy: dict[str, Any] | None = None,
    operational_summary: dict[str, Any] | None = None,
    memory_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    tuning = _json_object(policy.get("memory_governor_tuning"))
    if not bool(tuning.get("enabled", True)):
        return {"project_id": project_id, "enabled": False}

    execution_policy = load_execution_policy()
    governor_policy = _json_object(execution_policy.get("memory_governor"))
    lookback_days = max(3, int(tuning.get("lookback_days", 21) or 21))
    since_dt = _coerce_datetime(since) or (datetime.now(timezone.utc) - timedelta(days=lookback_days))
    min_tasks = max(1, int(tuning.get("min_tasks", 6) or 6))
    high_contamination_threshold = float(tuning.get("high_contamination_threshold", 0.45) or 0.45)
    low_contamination_threshold = float(tuning.get("low_contamination_threshold", 0.22) or 0.22)
    target_success_rate_tighten = float(tuning.get("target_success_rate_tighten", 0.78) or 0.78)
    low_success_rate_relax = float(tuning.get("low_success_rate_relax", 0.65) or 0.65)
    target_avg_context_tokens_in = float(tuning.get("target_avg_context_tokens_in", 980) or 980)
    high_local_filter_activation_rate = float(
        tuning.get("high_local_filter_activation_rate", 0.18) or 0.18
    )
    high_filtered_items_per_task = float(
        tuning.get("high_filtered_items_per_task", 0.45) or 0.45
    )
    min_hours_between_adjustments = max(0, int(tuning.get("min_hours_between_adjustments", 24) or 24))
    reversal_cooldown_hours = max(0, int(tuning.get("reversal_cooldown_hours", 72) or 72))

    current_assist_penalty_weight = float(governor_policy.get("assist_penalty_weight", 0.12) or 0.12)
    current_cross_project_risk = float(governor_policy.get("cross_project_risk", 0.06) or 0.06)
    current_verbosity_soft_cap = int(governor_policy.get("verbosity_soft_cap", 720) or 720)

    operational_summary = dict(operational_summary or summarize_recent_operational_metrics(project_id=project_id, since=since_dt))
    memory_audit = dict(memory_audit or audit_memory_governance(project_id=project_id, policy={"memory_targets": policy.get("memory_targets", {})}))
    tuning_history = summarize_memory_governor_tuning_history(
        project_id=project_id,
        maintenance_name=str(policy.get("maintenance_name") or tuning.get("maintenance_name") or "daily_guard").strip()
        or "daily_guard",
        lookback_days=max(lookback_days, int(tuning.get("history_lookback_days", lookback_days) or lookback_days)),
        limit=max(3, int(tuning.get("history_limit", 6) or 6)),
    )

    total_tasks = int(operational_summary.get("total_tasks") or 0)
    success_rate = float(operational_summary.get("success_rate") or 0.0)
    avg_context_tokens_in = operational_summary.get("avg_context_tokens_in")
    local_filter_activation_rate = float(operational_summary.get("governor_local_filter_activation_rate") or 0.0)
    filtered_items_per_task = float(operational_summary.get("governor_filtered_items_per_task") or 0.0)
    contamination_score = float(memory_audit.get("contamination_score") or 0.0)

    reasons: list[str] = []
    recommendation = "no_change"
    recommended_action = None
    reason = "insufficient_signal"
    suggested_assist_penalty_weight = current_assist_penalty_weight
    suggested_cross_project_risk = current_cross_project_risk
    suggested_verbosity_soft_cap = current_verbosity_soft_cap

    if total_tasks < min_tasks:
        reason = "insufficient_tasks"
    elif (
        (
            contamination_score >= high_contamination_threshold
            or local_filter_activation_rate >= high_local_filter_activation_rate
            or filtered_items_per_task >= high_filtered_items_per_task
        )
        and success_rate >= target_success_rate_tighten
        and (avg_context_tokens_in is None or float(avg_context_tokens_in) >= target_avg_context_tokens_in)
    ):
        recommendation = "tighten"
        recommended_action = "tune_memory_governor"
        if contamination_score >= high_contamination_threshold:
            reason = "memory_noise_needs_more_penalty"
        else:
            reason = "local_filter_pressure_high"
        suggested_assist_penalty_weight = current_assist_penalty_weight + float(
            tuning.get("step_assist_penalty_weight", 0.01) or 0.01
        )
        suggested_cross_project_risk = current_cross_project_risk + float(
            tuning.get("step_cross_project_risk", 0.01) or 0.01
        )
        suggested_verbosity_soft_cap = current_verbosity_soft_cap - int(
            tuning.get("step_verbosity_soft_cap", 40) or 40
        )
        reasons.append("memory_governor_tuning_needed")
    elif contamination_score <= low_contamination_threshold and success_rate <= low_success_rate_relax:
        recommendation = "relax"
        recommended_action = "tune_memory_governor"
        reason = "memory_penalty_may_be_too_strict"
        suggested_assist_penalty_weight = current_assist_penalty_weight - float(
            tuning.get("step_assist_penalty_weight", 0.01) or 0.01
        )
        suggested_cross_project_risk = current_cross_project_risk - float(
            tuning.get("step_cross_project_risk", 0.01) or 0.01
        )
        suggested_verbosity_soft_cap = current_verbosity_soft_cap + int(
            tuning.get("step_verbosity_soft_cap", 40) or 40
        )
        reasons.append("memory_governor_tuning_needed")
    else:
        reason = "within_target_band"

    suggested_assist_penalty_weight = round(
        _clamp(
            suggested_assist_penalty_weight,
            float(tuning.get("min_assist_penalty_weight", 0.08) or 0.08),
            float(tuning.get("max_assist_penalty_weight", 0.22) or 0.22),
        ),
        4,
    )
    suggested_cross_project_risk = round(
        _clamp(
            suggested_cross_project_risk,
            float(tuning.get("min_cross_project_risk", 0.03) or 0.03),
            float(tuning.get("max_cross_project_risk", 0.14) or 0.14),
        ),
        4,
    )
    suggested_verbosity_soft_cap = int(
        _clamp(
            float(suggested_verbosity_soft_cap),
            float(tuning.get("min_verbosity_soft_cap", 520) or 520),
            float(tuning.get("max_verbosity_soft_cap", 920) or 920),
        )
    )

    stability_guard = {
        "active": False,
        "reason": None,
        "last_direction": tuning_history.get("last_direction"),
        "last_updated_at": tuning_history.get("last_updated_at"),
        "hours_since_last_update": tuning_history.get("hours_since_last_update"),
    }
    if recommended_action == "tune_memory_governor":
        hours_since_last_update = tuning_history.get("hours_since_last_update")
        last_direction = str(tuning_history.get("last_direction") or "").strip()
        if (
            hours_since_last_update is not None
            and hours_since_last_update < min_hours_between_adjustments
        ):
            recommended_action = None
            recommendation = "no_change"
            reason = "recent_adjustment_cooldown"
            reasons = []
            suggested_assist_penalty_weight = round(current_assist_penalty_weight, 4)
            suggested_cross_project_risk = round(current_cross_project_risk, 4)
            suggested_verbosity_soft_cap = current_verbosity_soft_cap
            stability_guard.update({"active": True, "reason": "recent_adjustment_cooldown"})
        elif (
            last_direction
            and recommendation != "no_change"
            and recommendation != last_direction
            and hours_since_last_update is not None
            and hours_since_last_update < reversal_cooldown_hours
        ):
            recommended_action = None
            recommendation = "no_change"
            reason = "reversal_cooldown_active"
            reasons = []
            suggested_assist_penalty_weight = round(current_assist_penalty_weight, 4)
            suggested_cross_project_risk = round(current_cross_project_risk, 4)
            suggested_verbosity_soft_cap = current_verbosity_soft_cap
            stability_guard.update({"active": True, "reason": "reversal_cooldown_active"})

    return {
        "project_id": project_id,
        "enabled": True,
        "since": since_dt.isoformat(),
        "lookback_days": lookback_days,
        "current_assist_penalty_weight": round(current_assist_penalty_weight, 4),
        "current_cross_project_risk": round(current_cross_project_risk, 4),
        "current_verbosity_soft_cap": current_verbosity_soft_cap,
        "suggested_assist_penalty_weight": suggested_assist_penalty_weight,
        "suggested_cross_project_risk": suggested_cross_project_risk,
        "suggested_verbosity_soft_cap": suggested_verbosity_soft_cap,
        "recommended_action": recommended_action,
        "recommendation": recommendation,
        "reason": reason,
        "reasons": reasons,
        "stability_guard": stability_guard,
        "history": tuning_history,
        "metrics": {
            "total_tasks": total_tasks,
            "success_rate": round(success_rate, 4),
            "avg_context_tokens_in": avg_context_tokens_in,
            "token_efficiency_per_1k": operational_summary.get("token_efficiency_per_1k"),
            "contamination_score": contamination_score,
            "governor_local_filter_activation_rate": local_filter_activation_rate,
            "governor_filtered_items_per_task": filtered_items_per_task,
            "memory_severity": memory_audit.get("severity"),
        },
        "targets": {
            "min_tasks": min_tasks,
            "high_contamination_threshold": high_contamination_threshold,
            "low_contamination_threshold": low_contamination_threshold,
            "target_success_rate_tighten": target_success_rate_tighten,
            "low_success_rate_relax": low_success_rate_relax,
            "target_avg_context_tokens_in": target_avg_context_tokens_in,
            "high_local_filter_activation_rate": high_local_filter_activation_rate,
            "high_filtered_items_per_task": high_filtered_items_per_task,
            "min_hours_between_adjustments": min_hours_between_adjustments,
            "reversal_cooldown_hours": reversal_cooldown_hours,
        },
    }


def get_latest_maintenance_run(
    *,
    project_id: str | None = None,
    maintenance_name: str | None = None,
) -> dict[str, Any] | None:
    clauses = ["1=1"]
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = %s")
        params.append(project_id)
    if maintenance_name:
        clauses.append("maintenance_name = %s")
        params.append(maintenance_name)

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM project_registry.maintenance_runs
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(finished_at, started_at) DESC, created_at DESC
                LIMIT 1
                """,
                params,
            )
            return _row_to_dict(cur.fetchone())


def record_maintenance_run(
    *,
    project_id: str | None,
    maintenance_name: str,
    scope: str = "project",
    status: str = "success",
    trigger_reason: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    last_seen_activity_at: datetime | None = None,
    metrics_snapshot: dict[str, Any] | None = None,
    findings: dict[str, Any] | None = None,
    actions_applied: list[dict[str, Any]] | None = None,
    tokens_estimated: int | None = None,
    notes: str | None = None,
) -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.maintenance_runs (
                    project_id,
                    maintenance_name,
                    scope,
                    status,
                    trigger_reason,
                    started_at,
                    finished_at,
                    last_seen_activity_at,
                    metrics_snapshot,
                    findings,
                    actions_applied,
                    tokens_estimated,
                    notes
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    project_id,
                    maintenance_name,
                    scope,
                    status,
                    trigger_reason,
                    started_at or datetime.now(timezone.utc),
                    finished_at,
                    last_seen_activity_at,
                    json.dumps(metrics_snapshot or {}, ensure_ascii=False, default=str),
                    json.dumps(findings or {}, ensure_ascii=False, default=str),
                    json.dumps(actions_applied or [], ensure_ascii=False, default=str),
                    tokens_estimated or 0,
                    notes,
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else ""


def update_maintenance_run(
    *,
    maintenance_run_id: str,
    status: str,
    trigger_reason: str | None = None,
    finished_at: datetime | None = None,
    last_seen_activity_at: datetime | None = None,
    metrics_snapshot: dict[str, Any] | None = None,
    findings: dict[str, Any] | None = None,
    actions_applied: list[dict[str, Any]] | None = None,
    tokens_estimated: int | None = None,
    notes: str | None = None,
) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE project_registry.maintenance_runs
                SET status = %s,
                    trigger_reason = COALESCE(%s, trigger_reason),
                    finished_at = COALESCE(%s, finished_at),
                    last_seen_activity_at = COALESCE(%s, last_seen_activity_at),
                    metrics_snapshot = COALESCE(%s::jsonb, metrics_snapshot),
                    findings = COALESCE(%s::jsonb, findings),
                    actions_applied = COALESCE(%s::jsonb, actions_applied),
                    tokens_estimated = COALESCE(%s, tokens_estimated),
                    notes = COALESCE(%s, notes)
                WHERE id = %s
                RETURNING id
                """,
                (
                    status,
                    trigger_reason,
                    finished_at or datetime.now(timezone.utc),
                    last_seen_activity_at,
                    json.dumps(metrics_snapshot, ensure_ascii=False, default=str)
                    if metrics_snapshot is not None
                    else None,
                    json.dumps(findings, ensure_ascii=False, default=str)
                    if findings is not None
                    else None,
                    json.dumps(actions_applied, ensure_ascii=False, default=str)
                    if actions_applied is not None
                    else None,
                    tokens_estimated,
                    notes,
                    maintenance_run_id,
                ),
            )
            return cur.fetchone() is not None


def reap_stale_maintenance_runs(*, max_age_minutes: int = 30) -> dict[str, Any]:
    """Close abandoned queued/running maintenance rows without retrying work."""
    threshold = max(1, int(max_age_minutes))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE project_registry.maintenance_runs
                SET status = 'failure',
                    finished_at = NOW(),
                    last_seen_activity_at = NOW(),
                    trigger_reason = COALESCE(trigger_reason, 'maintenance_lease_expired'),
                    findings = COALESCE(findings, '{}'::jsonb) || jsonb_build_object(
                        'reaper',
                        jsonb_build_object(
                            'reason', 'maintenance_lease_expired',
                            'max_age_minutes', %s,
                            'reaped_at', NOW()
                        )
                    ),
                    notes = CONCAT_WS(
                        E'\n',
                        NULLIF(notes, ''),
                        'Closed by maintenance reaper after lease expiry.'
                    )
                WHERE status IN ('queued', 'running')
                  AND COALESCE(last_seen_activity_at, started_at, created_at)
                      < NOW() - make_interval(mins => %s)
                RETURNING id::text, project_id::text, maintenance_name, status
                """,
                (threshold, threshold),
            )
            rows = [dict(row) for row in cur.fetchall()]
    return {
        "status": "success",
        "max_age_minutes": threshold,
        "reaped_count": len(rows),
        "runs": rows,
    }


def register_connector(
    *,
    connector_key: str,
    connector_type: str,
    display_name: str,
    enabled: bool = True,
    health_mode: str = "probe",
    metadata: dict[str, Any] | None = None,
) -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.connector_registry (
                    connector_key, connector_type, display_name, enabled,
                    health_mode, metadata, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (connector_key) DO UPDATE SET
                    connector_type = EXCLUDED.connector_type,
                    display_name = EXCLUDED.display_name,
                    enabled = EXCLUDED.enabled,
                    health_mode = EXCLUDED.health_mode,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                RETURNING connector_key
                """,
                (
                    connector_key,
                    connector_type,
                    display_name,
                    enabled,
                    health_mode,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = cur.fetchone()
            return str(row["connector_key"]) if row else ""


def record_connector_health_event(
    *,
    connector_key: str,
    status: str,
    project_id: str | None = None,
    latency_ms: int | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.connector_health_events (
                    connector_key, project_id, status, latency_ms, message, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    connector_key,
                    project_id,
                    status,
                    latency_ms,
                    message,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else ""


def get_connector_health_summary() -> list[dict[str, Any]]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (registry.connector_key)
                    registry.connector_key,
                    registry.connector_type,
                    registry.display_name,
                    registry.enabled,
                    registry.health_mode,
                    event.status,
                    event.latency_ms,
                    event.message,
                    event.created_at AS last_health_at
                FROM project_registry.connector_registry registry
                LEFT JOIN project_registry.connector_health_events event
                  ON event.connector_key = registry.connector_key
                ORDER BY registry.connector_key, event.created_at DESC NULLS LAST
                """
            )
            return [dict(row) for row in cur.fetchall()]


def detect_maintenance_delta(
    *,
    project_id: str,
    since: datetime | str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    token_targets = _json_object(policy.get("token_targets"))
    latency_targets = _json_object(policy.get("latency_targets"))
    quality_targets = _json_object(policy.get("quality_targets"))
    catalog_targets = _json_object(policy.get("catalog_targets"))
    memory_targets = _json_object(policy.get("memory_targets"))
    delta_window_hours = max(1, int(policy.get("delta_window_hours", 24) or 24))
    since_dt = _coerce_datetime(since) or (datetime.now(timezone.utc) - timedelta(hours=delta_window_hours))

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    MAX(created_at) AS last_activity_at,
                    MAX(created_at::date) FILTER (WHERE log_type = 'task') AS latest_task_day,
                    COUNT(*) AS new_logs,
                    COUNT(*) FILTER (WHERE log_type = 'task') AS new_tasks,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'success') AS new_successes,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'failure') AS new_failures,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'partial') AS new_partials,
                    COUNT(*) FILTER (
                        WHERE log_type = 'task'
                          AND outcome = 'partial'
                          AND (
                              COALESCE(outcome_details, '') ILIKE '%%missing_artifacts%%'
                              OR COALESCE(description, '') ILIKE '%%missing_artifacts%%'
                              OR COALESCE(log_metadata::text, '') ILIKE '%%missing_artifacts%%'
                          )
                    ) AS partial_missing_artifacts,
                    AVG(confidence_score) FILTER (
                        WHERE log_type = 'task'
                          AND confidence_score IS NOT NULL
                    ) AS avg_confidence,
                    AVG(context_tokens_in) FILTER (
                        WHERE log_type = 'task'
                          AND context_tokens_in IS NOT NULL
                    ) AS avg_context_tokens_in,
                    AVG(context_tokens_out) FILTER (
                        WHERE log_type = 'task'
                          AND context_tokens_out IS NOT NULL
                    ) AS avg_context_tokens_out,
                    AVG(task_wall_clock_ms) FILTER (
                        WHERE log_type = 'task'
                          AND task_wall_clock_ms IS NOT NULL
                    ) AS avg_task_wall_clock_ms,
                    AVG(retrieval_latency_ms) FILTER (
                        WHERE log_type = 'task'
                          AND retrieval_latency_ms IS NOT NULL
                    ) AS avg_retrieval_latency_ms,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY retrieval_latency_ms
                    ) FILTER (
                        WHERE log_type = 'task'
                          AND retrieval_latency_ms IS NOT NULL
                    ) AS p90_retrieval_latency_ms,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY task_wall_clock_ms
                    ) FILTER (
                        WHERE log_type = 'task'
                          AND task_wall_clock_ms IS NOT NULL
                    ) AS p90_task_wall_clock_ms,
                    COUNT(*) FILTER (WHERE log_type = 'improvement') AS new_improvements
                FROM project_registry.project_logs
                WHERE project_id = %s
                  AND created_at > %s
                  AND COALESCE(log_metadata->>'maintenance_mode', 'false') <> 'true'
                """,
                (project_id, since_dt),
            )
            delta_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT MAX(day) AS latest_metrics_day
                FROM project_registry.mv_daily_metrics
                WHERE project_id = %s
                """,
                (project_id,),
            )
            metrics_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT MAX(snapshot_date) AS latest_kpi_day
                FROM project_registry.project_kpis
                WHERE project_id = %s
                """,
                (project_id,),
            )
            kpi_row = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'candidate') AS candidate_skills,
                    COUNT(*) FILTER (WHERE status = 'active') AS active_skills
                FROM project_registry.skill_catalog
                """
            )
            catalog_row = dict(cur.fetchone() or {})

    memory_audit = audit_memory_governance(project_id=project_id, policy={"memory_targets": memory_targets})
    operational_summary = summarize_recent_operational_metrics(
        project_id=project_id,
        since=since_dt,
    )
    anti_loop_dispatch_audit = analyze_anti_loop_dispatch_effectiveness(
        project_id=project_id,
        since=since_dt,
        policy={
            "maintenance_name": str(policy.get("maintenance_name") or "daily_guard"),
            "anti_loop_dispatch_tuning": policy.get("anti_loop_dispatch_tuning", {}),
        },
    )
    memory_governor_audit = analyze_memory_governor_effectiveness(
        project_id=project_id,
        since=since_dt,
        policy={
            "maintenance_name": str(policy.get("maintenance_name") or "daily_guard"),
            "memory_targets": memory_targets,
            "memory_governor_tuning": policy.get("memory_governor_tuning", {}),
        },
        operational_summary=operational_summary,
        memory_audit=memory_audit,
    )
    latest_task_day = delta_row.get("latest_task_day")
    latest_metrics_day = metrics_row.get("latest_metrics_day")
    latest_kpi_day = kpi_row.get("latest_kpi_day")
    candidate_skills = int(catalog_row.get("candidate_skills") or 0)
    active_skills = int(catalog_row.get("active_skills") or 0)
    candidate_active_ratio = round(candidate_skills / max(1, active_skills), 4)

    new_tasks = int(delta_row.get("new_tasks") or 0)
    new_successes = int(delta_row.get("new_successes") or 0)
    recent_success_rate = round(new_successes / max(1, new_tasks), 4) if new_tasks else None
    metrics_stale = bool(latest_task_day and (latest_metrics_day is None or latest_metrics_day < latest_task_day))
    kpi_stale = bool(latest_task_day and (latest_kpi_day is None or latest_kpi_day < latest_task_day))
    reasons: list[str] = []

    if int(delta_row.get("new_logs") or 0) > 0:
        reasons.append("new_logs")
    if metrics_stale:
        reasons.append("metrics_stale")
    if kpi_stale:
        reasons.append("kpi_stale")
    if float(delta_row.get("avg_context_tokens_in") or 0.0) > float(token_targets.get("max_recent_context_tokens_in") or 0.0):
        reasons.append("context_tokens_high")
    if float(delta_row.get("avg_context_tokens_out") or 0.0) > float(token_targets.get("max_recent_context_tokens_out") or 0.0):
        reasons.append("response_tokens_high")
    if float(delta_row.get("p90_retrieval_latency_ms") or 0.0) > float(latency_targets.get("max_recent_retrieval_p90_ms") or 0.0):
        reasons.append("retrieval_p90_high")
    if float(delta_row.get("p90_task_wall_clock_ms") or 0.0) > float(latency_targets.get("max_recent_task_p90_ms") or 0.0):
        reasons.append("task_p90_high")
    if recent_success_rate is not None and recent_success_rate < float(quality_targets.get("min_recent_success_rate") or 0.0):
        reasons.append("success_rate_low")
    if int(delta_row.get("partial_missing_artifacts") or 0) > int(quality_targets.get("max_partial_missing_artifacts") or 0):
        reasons.append("partial_missing_artifacts")
    if candidate_active_ratio > float(catalog_targets.get("max_candidate_active_ratio") or 999.0):
        reasons.append("candidate_pressure")
    for reason in list(memory_audit.get("reasons") or []):
        if reason not in reasons:
            reasons.append(reason)
    for reason in list(anti_loop_dispatch_audit.get("reasons") or []):
        if reason not in reasons:
            reasons.append(reason)
    for reason in list(memory_governor_audit.get("reasons") or []):
        if reason not in reasons:
            reasons.append(reason)

    recommended_actions: list[str] = []
    if policy.get("refresh_daily_metrics", True) and (metrics_stale or int(delta_row.get("new_logs") or 0) > 0):
        recommended_actions.append("refresh_daily_metrics")
    if kpi_stale or int(delta_row.get("new_logs") or 0) > 0:
        recommended_actions.append("snapshot_project_kpis")
    pattern_intelligence = _json_object(policy.get("pattern_intelligence"))
    if (
        pattern_intelligence.get("enabled", True)
        and pattern_intelligence.get("run_on_new_tasks", True)
        and new_tasks > 0
    ):
        recommended_actions.append("analyze_pattern_candidates")
    if (
        _json_object(policy.get("skill_factory")).get("enabled", True)
        and (
            int(delta_row.get("new_failures") or 0) > 0
            or int(delta_row.get("new_partials") or 0) > 0
            or "candidate_pressure" in reasons
            or "success_rate_low" in reasons
        )
    ):
        recommended_actions.append("run_skill_factory")
    if memory_audit.get("reasons"):
        recommended_actions.append("audit_memory_governance")
    exact_duplicate_ratio = float(memory_audit.get("experience_metrics", {}).get("exact_duplicate_ratio") or 0.0)
    if (
        int(memory_audit.get("experience_metrics", {}).get("exact_duplicate_experiences") or 0) > 0
        and exact_duplicate_ratio >= max(0.0, float(memory_targets.get("max_exact_duplicate_ratio", 0.01) or 0.01))
    ):
        recommended_actions.append("consolidate_duplicate_experiences")
    if anti_loop_dispatch_audit.get("recommended_action") == "tune_anti_loop_dispatch_bias":
        recommended_actions.append("tune_anti_loop_dispatch_bias")
    if memory_governor_audit.get("recommended_action") == "tune_memory_governor":
        recommended_actions.append("tune_memory_governor")

    if not reasons and not latest_metrics_day:
        recommended_actions.append("refresh_daily_metrics")
        reasons.append("metrics_not_materialized")
    if not reasons and not latest_kpi_day:
        recommended_actions.append("snapshot_project_kpis")
        reasons.append("kpi_not_materialized")

    fresh_signal_present = bool(
        int(delta_row.get("new_logs") or 0) > 0
        or metrics_stale
        or kpi_stale
        or latest_metrics_day is None
        or latest_kpi_day is None
        or bool(memory_audit.get("reasons"))
        or bool(anti_loop_dispatch_audit.get("reasons"))
        or bool(memory_governor_audit.get("reasons"))
    )
    should_run = bool(reasons or recommended_actions)
    if bool(policy.get("skip_if_no_new_logs", False)) and not fresh_signal_present:
        should_run = False

    return {
        "project_id": project_id,
        "since": since_dt.isoformat(),
        "last_activity_at": _coerce_datetime(delta_row.get("last_activity_at")),
        "latest_task_day": latest_task_day,
        "latest_metrics_day": latest_metrics_day,
        "latest_kpi_day": latest_kpi_day,
        "new_logs": int(delta_row.get("new_logs") or 0),
        "new_tasks": new_tasks,
        "new_successes": new_successes,
        "new_failures": int(delta_row.get("new_failures") or 0),
        "new_partials": int(delta_row.get("new_partials") or 0),
        "partial_missing_artifacts": int(delta_row.get("partial_missing_artifacts") or 0),
        "new_improvements": int(delta_row.get("new_improvements") or 0),
        "avg_confidence": round(float(delta_row.get("avg_confidence") or 0.0), 4) if delta_row.get("avg_confidence") is not None else None,
        "avg_context_tokens_in": round(float(delta_row.get("avg_context_tokens_in") or 0.0), 2) if delta_row.get("avg_context_tokens_in") is not None else None,
        "avg_context_tokens_out": round(float(delta_row.get("avg_context_tokens_out") or 0.0), 2) if delta_row.get("avg_context_tokens_out") is not None else None,
        "avg_task_wall_clock_ms": round(float(delta_row.get("avg_task_wall_clock_ms") or 0.0), 2) if delta_row.get("avg_task_wall_clock_ms") is not None else None,
        "avg_retrieval_latency_ms": round(float(delta_row.get("avg_retrieval_latency_ms") or 0.0), 2) if delta_row.get("avg_retrieval_latency_ms") is not None else None,
        "p90_retrieval_latency_ms": round(float(delta_row.get("p90_retrieval_latency_ms") or 0.0), 2) if delta_row.get("p90_retrieval_latency_ms") is not None else None,
        "p90_task_wall_clock_ms": round(float(delta_row.get("p90_task_wall_clock_ms") or 0.0), 2) if delta_row.get("p90_task_wall_clock_ms") is not None else None,
        "recent_success_rate": recent_success_rate,
        "candidate_skills": candidate_skills,
        "active_skills": active_skills,
        "candidate_active_ratio": candidate_active_ratio,
        "operational_summary": operational_summary,
        "memory_audit": memory_audit,
        "anti_loop_dispatch_audit": anti_loop_dispatch_audit,
        "memory_governor_audit": memory_governor_audit,
        "metrics_stale": metrics_stale,
        "kpi_stale": kpi_stale,
        "fresh_signal_present": fresh_signal_present,
        "reasons": reasons,
        "recommended_actions": recommended_actions,
        "should_run": should_run,
    }


if __name__ == "__main__":
    test_path = "C:/Users/dev/workspace"
    project = get_or_create_project(
        project_path=test_path,
        project_name="the workspace",
        description="MCUM project registry smoke test",
        tech_stack={"language": "Python", "db": "PostgreSQL"},
        client_or_context="Carlos",
    )
    session = log_session_start(test_path, task_description="Registry smoke test")
    task_log = log_entry(
        project_id=project["id"],
        log_type="task",
        title="MCUM registry smoke test",
        description="Registry helper functions executed successfully.",
        skill_used="mcum-orchestrator",
        outcome="success",
        confidence_score=0.95,
    )
    end_log = log_session_end(project["id"], session_duration_sec=1, tasks_completed=1)
    print(project["project_name"])
    print(session["log_id"])
    print(task_log)
    print(end_log)
