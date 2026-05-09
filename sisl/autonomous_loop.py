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
    counts: dict[str, int] = {}
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
                counts[str(row["skill_name"])] = int(row.get("n") or 0)

    discovered = discover_local_skills()
    skill_names = [entry["skill_name"] for entry in discovered]
    return sorted(skill_names, key=lambda skill_name: counts.get(skill_name, 0), reverse=True)


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
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "not_enough_experiences",
            "stats_before": before,
            "stats_after": before,
            "actions": actions,
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
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "no_tests_available",
            "stats_before": before,
            "stats_after": current,
            "actions": actions,
        }

    if not generated and _cooldown_active(current["last_sisl_at"], cfg.cooldown_minutes):
        return {
            "skill_name": skill_name,
            "skipped": True,
            "reason": "cooldown_active",
            "stats_before": before,
            "stats_after": current,
            "actions": actions,
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
        "stats_before": before,
        "stats_after": after,
        "actions": actions,
        "cycle": cycle,
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
