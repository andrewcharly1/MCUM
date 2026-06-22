"""
Autonomous SISL loop for MCUM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..db.connection import get_db, get_cursor
from ..db.project_registry import log_entry
from ..db.skill_catalog import discover_local_skills
from ..policy import load_execution_policy
from .optimizer import run_sisl_cycle
from .test_generator import generate_and_save


@dataclass
class AutonomousLoopConfig:
    min_experiences: int = 3
    min_tests: int = 6
    max_tests: int = 12
    target_ckl: float = 0.85
    cooldown_minutes: int = 30


def _local_timezone():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _normalize_loop_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = value.replace(tzinfo=_local_timezone())
    return value.astimezone(timezone.utc)


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _normalize_skill_names(values) -> set[str]:
    normalized: set[str] = set()
    for value in list(values or []):
        cleaned = str(value or "").strip()
        if cleaned:
            normalized.add(cleaned)
    return normalized


def _priority_timestamp(value) -> datetime:
    timestamp = _normalize_loop_timestamp(value)
    if timestamp is not None:
        return timestamp
    return datetime.min.replace(tzinfo=timezone.utc)


def _build_signal_summary(
    *,
    skill_name: str,
    phase: str,
    skipped: bool,
    reason: str | None,
    before: dict,
    after: dict,
    actions: list[dict],
    cycle: dict | None = None,
) -> dict:
    cycle = cycle or {}
    final_ckl = float(cycle.get("ckl_score") or 0.0)
    baseline_ckl = float(cycle.get("baseline_ckl_score") or 0.0)
    delta_ckl = round(final_ckl - baseline_ckl, 4) if cycle else 0.0
    accepted = None
    if cycle.get("gate_result") is not None:
        accepted = bool(cycle["gate_result"].get("accepted"))

    action_types = [item.get("type") for item in actions if item.get("type")]
    if skipped:
        next_step = "re-run after cooldown or bootstrap new tests"
        if reason == "not_enough_experiences":
            next_step = "collect more experiences before SISL"
        elif reason == "no_tests_available":
            next_step = "bootstrap validation tests first"
        elif reason == "cooldown_active":
            next_step = "wait for cooldown and retry"
    elif accepted is False:
        next_step = "investigate rejected candidate and tighten proposals"
    elif accepted is True and final_ckl >= 0.85:
        next_step = "candidate accepted; move to next skill or prune stale candidates"
    else:
        next_step = "continue iterating with fresh tests"

    return {
        "skill_name": skill_name,
        "phase": phase,
        "skipped": skipped,
        "reason": reason,
        "next_step": next_step,
        "accepted": accepted,
        "delta_ckl": delta_ckl,
        "ckl_score": final_ckl if cycle else None,
        "baseline_ckl_score": baseline_ckl if cycle else None,
        "experience_count": before.get("experience_count"),
        "test_count": after.get("test_count"),
        "retrieval_run_count": after.get("retrieval_run_count"),
        "actions_n": len(actions),
        "action_types": action_types[:5],
        "proposals_n": cycle.get("proposals_n"),
        "high_conf_n": cycle.get("high_conf_n"),
        "applied_n": len(cycle.get("applied") or []),
        "writeback_mode": cycle.get("applied", [{}])[0].get("mode") if cycle.get("applied") else None,
    }


def resolve_writeback_mode(
    skill_name: str,
    execution_policy: dict,
    requested_mode: str | None = None,
) -> str:
    if requested_mode:
        return str(requested_mode)

    base_mode = str(execution_policy.get("autonomous_writeback", "disabled") or "disabled")
    if base_mode == "disabled":
        return "disabled"

    excluded = _normalize_skill_names(execution_policy.get("autonomous_writeback_exclude"))
    if skill_name in excluded:
        return "disabled"

    targets = _normalize_skill_names(execution_policy.get("autonomous_writeback_targets"))
    if targets and skill_name not in targets:
        return "disabled"

    return base_mode


def get_skill_loop_stats(skill_name: str) -> dict:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM core_brain.experiences WHERE skill_name = %s) AS experience_count,
                    (SELECT COUNT(*) FROM core_brain.test_suite WHERE skill_name = %s AND is_active = TRUE) AS test_count,
                    (SELECT COUNT(*) FROM core_brain.retrieval_runs WHERE skill_name = %s) AS retrieval_run_count,
                    (
                        SELECT created_at
                        FROM core_brain.skill_versions
                        WHERE skill_name = %s
                          AND improvement_source = 'sisl_loop'
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_sisl_at
                """,
                (skill_name, skill_name, skill_name, skill_name),
            )
            row = dict(cur.fetchone())
            return {
                "experience_count": int(row.get("experience_count") or 0),
                "test_count": int(row.get("test_count") or 0),
                "retrieval_run_count": int(row.get("retrieval_run_count") or 0),
                "last_sisl_at": row.get("last_sisl_at"),
            }


def get_current_skill_version(skill_name: str) -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT version_semver
                FROM core_brain.skill_versions
                WHERE skill_name = %s
                ORDER BY
                    CASE status WHEN 'active' THEN 0 WHEN 'testing' THEN 1 ELSE 2 END,
                    created_at DESC
                LIMIT 1
                """,
                (skill_name,),
            )
            row = cur.fetchone()
            return row["version_semver"] if row and row.get("version_semver") else "1.0.0"


def get_skills_for_evaluation() -> list[str]:
    experience_counts: dict[str, int] = {}
    test_counts: dict[str, int] = {}
    last_sisl_at: dict[str, datetime] = {}
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT skill_name, COUNT(*) AS n
                FROM core_brain.experiences
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                experience_counts[str(row["skill_name"])] = int(row.get("n") or 0)

            cur.execute(
                """
                SELECT skill_name, COUNT(*) AS n
                FROM core_brain.test_suite
                WHERE is_active = TRUE
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                test_counts[str(row["skill_name"])] = int(row.get("n") or 0)

            cur.execute(
                """
                SELECT skill_name, MAX(created_at) AS last_sisl_at
                FROM core_brain.skill_versions
                WHERE improvement_source = 'sisl_loop'
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                timestamp = _normalize_loop_timestamp(row.get("last_sisl_at"))
                if timestamp is not None:
                    last_sisl_at[str(row["skill_name"])] = timestamp

    discovered = discover_local_skills()
    skill_names = [entry["skill_name"] for entry in discovered]

    def _priority(skill_name: str) -> tuple[int, int, datetime, str]:
        return (
            test_counts.get(skill_name, 0),
            -experience_counts.get(skill_name, 0),
            _priority_timestamp(last_sisl_at.get(skill_name)),
            skill_name,
        )

    return sorted(skill_names, key=_priority)


def _cooldown_active(last_sisl_at, cooldown_minutes: int) -> bool:
    if not last_sisl_at or cooldown_minutes <= 0:
        return False
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=cooldown_minutes)
    last_dt = _normalize_loop_timestamp(last_sisl_at)
    if last_dt is None:
        return False
    return last_dt > threshold


def run_autonomous_improvement(
    skill_name: str,
    skill_version: str | None = None,
    project_id: str | None = None,
    trigger: str = "session_close",
    config: AutonomousLoopConfig | None = None,
    writeback_mode_override: str | None = None,
    verbose: bool = False,
) -> dict:
    cfg = config or AutonomousLoopConfig()
    execution_policy = load_execution_policy()
    effective_version = skill_version or get_current_skill_version(skill_name)
    before = get_skill_loop_stats(skill_name)
    actions: list[dict] = []

    if before["experience_count"] < cfg.min_experiences:
        summary = _build_signal_summary(
            skill_name=skill_name,
            phase="bootstrap_gate",
            skipped=True,
            reason="not_enough_experiences",
            before=before,
            after=before,
            actions=actions,
        )
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "not_enough_experiences",
            "phase": "bootstrap_gate",
            "stats_before": before,
            "stats_after": before,
            "actions": actions,
            "signal_summary": summary,
        }

    generated = None
    if before["test_count"] < cfg.min_tests:
        generated = generate_and_save(skill_name, max_tests=cfg.max_tests)
        actions.append(
            {
                "type": "bootstrap_tests",
                "generated": generated["total_generated"],
                "saved": len(generated["saved_ids"]),
                "breakdown": generated["breakdown"],
            }
        )

    current = get_skill_loop_stats(skill_name)
    if current["test_count"] == 0:
        summary = _build_signal_summary(
            skill_name=skill_name,
            phase="bootstrap_tests",
            skipped=True,
            reason="no_tests_available",
            before=before,
            after=current,
            actions=actions,
        )
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "no_tests_available",
            "phase": "bootstrap_tests",
            "stats_before": before,
            "stats_after": current,
            "actions": actions,
            "signal_summary": summary,
        }

    if not generated and _cooldown_active(current["last_sisl_at"], cfg.cooldown_minutes):
        summary = _build_signal_summary(
            skill_name=skill_name,
            phase="cooldown",
            skipped=True,
            reason="cooldown_active",
            before=before,
            after=current,
            actions=actions,
        )
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "cooldown_active",
            "phase": "cooldown",
            "stats_before": before,
            "stats_after": current,
            "actions": actions,
            "signal_summary": summary,
        }

    policy_writeback_mode = execution_policy.get("autonomous_writeback", "disabled")
    writeback_mode = resolve_writeback_mode(
        skill_name,
        execution_policy,
        requested_mode=writeback_mode_override,
    )
    cycle = run_sisl_cycle(
        skill_name=skill_name,
        skill_version=effective_version,
        target_ckl=cfg.target_ckl,
        verbose=verbose,
        dry_run=writeback_mode == "disabled",
        persist_eval=True,
        writeback_mode=writeback_mode,
    )
    actions.append(
        {
            "type": "sisl_cycle",
            "ckl_score": cycle["ckl_score"],
            "baseline_ckl_score": cycle.get("baseline_ckl_score"),
            "proposals_n": cycle["proposals_n"],
            "high_conf_n": cycle["high_conf_n"],
            "applied_n": len(cycle["applied"]),
            "writeback_mode": writeback_mode,
            "policy_writeback_mode": policy_writeback_mode,
            "sisl_mode": execution_policy.get("sisl_mode", "db_only"),
            "eval_record_id": cycle.get("eval_record_id"),
            "candidate_eval_record_id": cycle.get("candidate_eval_record_id"),
            "report_id": cycle.get("report_id"),
            "gate_result": cycle.get("gate_result"),
        }
    )

    after = get_skill_loop_stats(skill_name)

    if project_id:
        outcome = "success" if cycle["ckl_score"] >= cfg.target_ckl else "partial"
        log_entry(
            project_id=project_id,
            log_type="improvement",
            title=f"Autonomous SISL cycle: {skill_name}",
            description=(
                f"trigger={trigger}; ckl={cycle['ckl_score']:.3f}; "
                f"proposals={cycle['proposals_n']}; applied={len(cycle['applied'])}"
            ),
            skill_used="mcum-orchestrator",
            skills_orchestrated=[skill_name],
            outcome=outcome,
            confidence_score=cycle["ckl_score"],
            log_metadata={
                "trigger": trigger,
                "stats_before": _json_safe(before),
                "stats_after": _json_safe(after),
                "actions": _json_safe(actions),
            },
        )

    return {
        "skill_name": skill_name,
        "skill_version": effective_version,
        "skipped": False,
        "reason": None,
        "phase": "sisl_cycle",
        "stats_before": before,
        "stats_after": after,
        "actions": actions,
        "cycle": cycle,
        "signal_summary": _build_signal_summary(
            skill_name=skill_name,
            phase="sisl_cycle",
            skipped=False,
            reason=None,
            before=before,
            after=after,
            actions=actions,
            cycle=cycle,
        ),
    }


def run_workspace_improvement_cycle(
    *,
    max_skills: int | None = None,
    verbose: bool = False,
) -> list[dict]:
    skills = get_skills_for_evaluation()
    if max_skills is not None:
        skills = skills[:max_skills]

    results: list[dict] = []
    for skill_name in skills:
        results.append(
            run_autonomous_improvement(
                skill_name=skill_name,
                trigger="workspace_cycle",
                verbose=verbose,
            )
        )
    return results


if __name__ == "__main__":
    result = run_autonomous_improvement(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        trigger="cli",
        verbose=True,
    )
    print(result)
