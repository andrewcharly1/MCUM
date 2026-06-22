"""
Anti-loop detection and advisory scoring for MCUM.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .db.connection import get_db, get_cursor

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+./:-]*", re.IGNORECASE)
_STOPWORDS = {
    "para",
    "desde",
    "hasta",
    "sobre",
    "entre",
    "como",
    "esta",
    "este",
    "estos",
    "estas",
    "that",
    "with",
    "from",
    "then",
    "need",
    "have",
    "should",
    "must",
    "would",
    "could",
}
_FAILURE_OUTCOMES = {"failure", "partial"}


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


def _tokenize_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if len(token) < 3 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _fingerprint(prefix: str, *parts: Any) -> tuple[str, list[str], str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for token in _tokenize_text(part):
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    if not tokens:
        return "", [], ""
    digest = hashlib.sha1("|".join(tokens).encode("utf-8")).hexdigest()[:16]
    signature = " ".join(tokens[:8])
    return f"{prefix}:{digest}", tokens, signature


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
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


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _fetch_recent_task_rows(project_id: str, *, lookback_limit: int, recent_days: int) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, recent_days))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    title,
                    description,
                    outcome,
                    outcome_details,
                    skill_used,
                    log_metadata,
                    created_at
                FROM project_registry.project_logs
                WHERE project_id = %s
                  AND log_type = 'task'
                  AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project_id, since, max(1, lookback_limit)),
            )
            return [dict(row) for row in cur.fetchall()]


def _row_problem_fingerprint(row: dict[str, Any]) -> str:
    metadata = _json_object(row.get("log_metadata"))
    anti_loop = _json_object(metadata.get("anti_loop"))
    fingerprint = str(anti_loop.get("problem_fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    task_brief = _json_object(metadata.get("task_brief"))
    fingerprint, _, _ = _fingerprint(
        "problem",
        metadata.get("task_description") or row.get("title"),
        task_brief.get("task_type"),
        task_brief.get("objective"),
        task_brief.get("expected_deliverable"),
        task_brief.get("success_criteria"),
    )
    return fingerprint


def _row_strategy_fingerprint(row: dict[str, Any]) -> str:
    metadata = _json_object(row.get("log_metadata"))
    anti_loop = _json_object(metadata.get("anti_loop"))
    fingerprint = str(anti_loop.get("strategy_fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    orchestration = _json_object(metadata.get("orchestration"))
    fingerprint, _, _ = _fingerprint(
        "strategy",
        metadata.get("final_skill") or metadata.get("selected_skill") or row.get("skill_used"),
        metadata.get("dispatch_method"),
        metadata.get("retrieval_mode"),
        orchestration.get("mode"),
        metadata.get("playbook_scope"),
        _json_object(metadata.get("task_brief")).get("execution_mode"),
    )
    return fingerprint


def _row_error_fingerprint(row: dict[str, Any]) -> str:
    metadata = _json_object(row.get("log_metadata"))
    anti_loop = _json_object(metadata.get("anti_loop"))
    fingerprint = str(anti_loop.get("error_fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    if str(row.get("outcome") or "").strip().lower() not in _FAILURE_OUTCOMES:
        return ""
    fingerprint, _, _ = _fingerprint(
        "error",
        row.get("outcome"),
        row.get("outcome_details"),
        metadata.get("validation_summary"),
        metadata.get("task_result_metadata"),
    )
    return fingerprint


def _success_skills(rows: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("outcome") or "").strip().lower() != "success":
            continue
        metadata = _json_object(row.get("log_metadata"))
        skill_name = str(
            metadata.get("final_skill")
            or metadata.get("selected_skill")
            or row.get("skill_used")
            or ""
        ).strip()
        if skill_name and skill_name not in seen:
            seen.add(skill_name)
            ordered.append(skill_name)
    return ordered


def _risk_level(score: float, *, medium_threshold: float, high_threshold: float) -> str:
    if score >= high_threshold:
        return "high"
    if score >= medium_threshold:
        return "medium"
    return "low"


def sanitize_loop_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    return {
        key: value
        for key, value in state.items()
        if not str(key).startswith("_")
    }


def analyze_problem_loop(
    *,
    project_id: str | None,
    task_description: str,
    task_brief: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    if not bool(policy.get("enabled", True)) or not project_id:
        return {"enabled": False}

    lookback_limit = max(1, int(policy.get("lookback_limit", 30) or 30))
    recent_days = max(1, int(policy.get("recent_days", 21) or 21))
    repeat_problem_threshold = max(1, int(policy.get("repeat_problem_threshold", 2) or 2))
    medium_threshold = float(policy.get("warning_risk_threshold", 0.35) or 0.35)
    high_threshold = float(policy.get("high_risk_threshold", 0.65) or 0.65)
    max_recent_outcomes = max(1, int(policy.get("max_recent_outcomes", 5) or 5))
    brief = dict(task_brief or {})

    problem_fingerprint, _, problem_signature = _fingerprint(
        "problem",
        task_description,
        brief.get("task_type"),
        brief.get("objective"),
        brief.get("expected_deliverable"),
        brief.get("success_criteria"),
    )
    if not problem_fingerprint:
        return {"enabled": True}

    try:
        recent_rows = _fetch_recent_task_rows(
            project_id,
            lookback_limit=lookback_limit,
            recent_days=recent_days,
        )
    except Exception:
        return {
            "enabled": True,
            "problem_fingerprint": problem_fingerprint,
            "problem_signature": problem_signature,
            "loop_risk": 0.0,
            "risk_level": "low",
            "recommendation": "observe_only",
            "warnings": [],
        }

    problem_rows = [row for row in recent_rows if _row_problem_fingerprint(row) == problem_fingerprint]
    repeated_problem_total = len(problem_rows)
    repeated_problem_failures = sum(
        1 for row in problem_rows if str(row.get("outcome") or "").strip().lower() in _FAILURE_OUTCOMES
    )
    repeated_problem_successes = sum(
        1 for row in problem_rows if str(row.get("outcome") or "").strip().lower() == "success"
    )
    recent_outcomes = [
        {
            "outcome": row.get("outcome"),
            "created_at": _coerce_datetime(row.get("created_at")).isoformat()
            if _coerce_datetime(row.get("created_at"))
            else None,
        }
        for row in problem_rows[:max_recent_outcomes]
    ]
    success_escape_skills = _success_skills(problem_rows)
    loop_risk = _clamp(
        (min(repeated_problem_total / max(1, repeat_problem_threshold + 1), 1.0) * 0.24)
        + (min(repeated_problem_failures / repeat_problem_threshold, 1.0) * 0.52)
        - (min(repeated_problem_successes / max(1, repeat_problem_threshold), 1.0) * 0.14)
    )
    recommendation = "observe_only"
    warnings: list[str] = []
    if repeated_problem_failures >= repeat_problem_threshold:
        recommendation = "increase_validation_and_diverge"
        warnings.append(
            "Anti-loop: repeated failure pattern detected for a similar task in this project."
        )
    elif repeated_problem_successes > 0:
        recommendation = "reuse_prior_success"

    return {
        "enabled": True,
        "problem_fingerprint": problem_fingerprint,
        "problem_signature": problem_signature,
        "repeated_problem_total": repeated_problem_total,
        "repeated_problem_failures": repeated_problem_failures,
        "repeated_problem_successes": repeated_problem_successes,
        "recent_outcomes": recent_outcomes,
        "success_escape_skills": success_escape_skills[:3],
        "lookback_limit": lookback_limit,
        "recent_days": recent_days,
        "loop_risk": round(loop_risk, 4),
        "risk_level": _risk_level(loop_risk, medium_threshold=medium_threshold, high_threshold=high_threshold),
        "recommendation": recommendation,
        "warnings": warnings,
        "_recent_rows": recent_rows,
    }


def enrich_loop_state_with_strategy(
    *,
    loop_state: dict[str, Any] | None,
    skill_name: str,
    dispatch_method: str,
    retrieval_mode: str,
    execution_mode: str | None = None,
    playbook_scope: str | None = None,
    orchestration: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = dict(loop_state or {})
    policy = dict(policy or {})
    if not state.get("enabled"):
        return sanitize_loop_state(state)

    repeat_strategy_failure_threshold = max(1, int(policy.get("repeat_strategy_failure_threshold", 2) or 2))
    medium_threshold = float(policy.get("warning_risk_threshold", 0.35) or 0.35)
    high_threshold = float(policy.get("high_risk_threshold", 0.65) or 0.65)
    strategy_fingerprint, _, strategy_signature = _fingerprint(
        "strategy",
        skill_name,
        dispatch_method,
        retrieval_mode,
        execution_mode,
        playbook_scope,
        (orchestration or {}).get("mode"),
    )
    state["strategy_fingerprint"] = strategy_fingerprint
    state["strategy_signature"] = strategy_signature

    recent_rows = list(state.get("_recent_rows") or [])
    problem_fingerprint = str(state.get("problem_fingerprint") or "").strip()
    problem_rows = [row for row in recent_rows if _row_problem_fingerprint(row) == problem_fingerprint]
    strategy_rows = [row for row in problem_rows if _row_strategy_fingerprint(row) == strategy_fingerprint]
    repeated_strategy_total = len(strategy_rows)
    repeated_strategy_failures = sum(
        1 for row in strategy_rows if str(row.get("outcome") or "").strip().lower() in _FAILURE_OUTCOMES
    )
    alternate_success_skills = [
        skill
        for skill in _success_skills(problem_rows)
        if skill and skill != skill_name
    ]
    loop_risk = _clamp(
        float(state.get("loop_risk") or 0.0)
        + (min(repeated_strategy_total / max(1, repeat_strategy_failure_threshold + 1), 1.0) * 0.12)
        + (min(repeated_strategy_failures / repeat_strategy_failure_threshold, 1.0) * 0.32)
    )
    warnings = list(state.get("warnings") or [])
    recommendation = str(state.get("recommendation") or "observe_only")
    if repeated_strategy_failures >= repeat_strategy_failure_threshold:
        recommendation = "switch_strategy_before_retry"
        warnings.append(
            "Anti-loop: this same strategy has failed repeatedly on a similar task."
        )
    elif recommendation == "observe_only" and alternate_success_skills:
        recommendation = "consider_alternate_successful_skill"

    state.update(
        {
            "repeated_strategy_total": repeated_strategy_total,
            "repeated_strategy_failures": repeated_strategy_failures,
            "alternate_success_skills": alternate_success_skills[:3],
            "loop_risk": round(loop_risk, 4),
            "risk_level": _risk_level(loop_risk, medium_threshold=medium_threshold, high_threshold=high_threshold),
            "recommendation": recommendation,
            "warnings": warnings,
        }
    )
    return sanitize_loop_state(state)


def finalize_loop_state(
    *,
    project_id: str | None,
    loop_state: dict[str, Any] | None,
    result_outcome: str,
    result_error_description: str | None = None,
    result_validation_summary: str | None = None,
    result_metadata: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = dict(loop_state or {})
    policy = dict(policy or {})
    if not state.get("enabled") or not project_id:
        return sanitize_loop_state(state)

    lookback_limit = max(1, int(policy.get("lookback_limit", 30) or 30))
    recent_days = max(1, int(policy.get("recent_days", 21) or 21))
    repeat_error_threshold = max(1, int(policy.get("repeat_error_threshold", 2) or 2))
    medium_threshold = float(policy.get("warning_risk_threshold", 0.35) or 0.35)
    high_threshold = float(policy.get("high_risk_threshold", 0.65) or 0.65)

    try:
        recent_rows = _fetch_recent_task_rows(
            project_id,
            lookback_limit=lookback_limit,
            recent_days=recent_days,
        )
    except Exception:
        return sanitize_loop_state(state)

    error_fingerprint = ""
    error_signature = ""
    if str(result_outcome or "").strip().lower() in _FAILURE_OUTCOMES:
        error_fingerprint, _, error_signature = _fingerprint(
            "error",
            result_outcome,
            result_error_description,
            result_validation_summary,
            result_metadata,
        )

    repeated_error_failures = 0
    if error_fingerprint:
        repeated_error_failures = sum(
            1
            for row in recent_rows
            if _row_error_fingerprint(row) == error_fingerprint
            and str(row.get("outcome") or "").strip().lower() in _FAILURE_OUTCOMES
        )

    warnings = list(state.get("warnings") or [])
    recommendation = str(state.get("recommendation") or "observe_only")
    loop_risk = float(state.get("loop_risk") or 0.0)
    if repeated_error_failures >= repeat_error_threshold:
        loop_risk = _clamp(loop_risk + 0.22)
        recommendation = "elevate_error_memory"
        warnings.append("Anti-loop: recurring error family detected; promote stronger safeguards.")

    state.update(
        {
            "result_outcome": result_outcome,
            "error_fingerprint": error_fingerprint,
            "error_signature": error_signature,
            "repeated_error_failures": repeated_error_failures,
            "loop_risk": round(loop_risk, 4),
            "risk_level": _risk_level(loop_risk, medium_threshold=medium_threshold, high_threshold=high_threshold),
            "recommendation": recommendation,
            "warnings": warnings,
        }
    )
    return sanitize_loop_state(state)
